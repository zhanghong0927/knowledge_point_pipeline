"""使用离线桩检查客户端与统计行为；测试不调用真实模型服务。"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any
from unittest.mock import patch

import httpx
from instructor.core.exceptions import (
    IncompleteOutputException,
    InstructorRetryException,
)
from openai import APIStatusError, OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator
from synthesis_fixtures import independent_from_payload
from tenacity import RetryCallState, Retrying

from book_extractor.llm import (
    ContextBudgetError,
    DeferredCallError,
    ExtractionCancelledError,
    LLMClient,
    _retry_wait,
    load_service,
)
from book_extractor.markdown import Unit
from book_extractor.models import (
    Candidate,
    Grouping,
    SynthesisReview,
    partition_error,
)
from book_extractor.synthesis import _synthesize_batch
from book_extractor.telemetry import EventLog, TelemetryWriteError, summarize_events


class Selection(BaseModel):
    """用于验证证据成员约束的最小响应模型。"""

    ids: list[str]

    @field_validator("ids")
    @classmethod
    def known_ids(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """检查引用属于调用上下文的允许集合；未知引用抛 ValueError，合法值原样返回。"""
        if not set(value) <= set((info.context or {}).get("allowed_ids", [])):
            raise ValueError("ids must belong to allowed_ids")
        return value


class LLMTests(unittest.TestCase):
    """使用本地 HTTP 桩检查请求预算、验证重试和并发统计，不访问真实服务。"""

    def test_independent_repair_uses_only_failed_member_over_http(self) -> None:
        """真实 Instructor 收到单项别名错误时不原生重生成整批，只请求失败成员。"""
        payload = {
            "candidates": [
                {
                    "candidate_id": f"c{index}",
                    "name": name,
                    "sources": [{"id": f"u00000{index}"}],
                }
                for index, name in enumerate(["甲", "乙"], 1)
            ]
        }
        initial = independent_from_payload(payload)
        initial.items[0].finding.aliases = ["无来源别名"]
        repaired = independent_from_payload({"candidates": payload["candidates"][:1]})
        replies = []
        for answer in (initial, repaired):
            status, body = self.reply()
            body["choices"][0]["message"]["content"] = answer.model_dump_json()
            replies.append((status, body))
        client, requests = self.make_client(replies)
        members = [
            Candidate(
                candidate_id=row["candidate_id"],
                chunk_id="chunk",
                scope=[],
                name=row["name"],
                evidence_ids=[row["sources"][0]["id"]],
            )
            for row in payload["candidates"]
        ]
        sources = {
            member.evidence_ids[0]: Unit(
                member.evidence_ids[0], f"{member.name}的原文。", 1, 1, [], "paragraph"
            )
            for member in members
        }
        with tempfile.TemporaryDirectory() as folder:
            records, issues = _synthesize_batch(
                members, False, sources, client, "book", "run", Path(folder), Event()
            )
        self.assertFalse(any(row.get("failed") for row in issues))
        self.assertEqual([record.name for record in records], ["甲", "乙"])
        self.assertEqual(len(requests), 2)
        second_payload = json.loads(
            next(
                row["content"]
                for row in requests[1]["messages"]
                if row["role"] == "user"
            )
        )
        self.assertEqual(
            [row["candidate_id"] for row in second_payload["candidates"]], ["c1"]
        )
        # 首轮外层错误消耗一次原生重试；随后局部修复只剩一次普通请求额度。
        unknown = initial.model_copy(deep=True)
        unknown.items[0].candidate_id = "c99"
        _, body = self.reply()
        body["choices"][0]["message"]["content"] = unknown.model_dump_json()
        client, requests = self.make_client([(200, body), replies[0], replies[0]])
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("book_extractor.llm._retry_wait", return_value=0),
        ):
            _, issues = _synthesize_batch(
                members, False, sources, client, "book", "run", Path(folder), Event()
            )
        self.assertEqual(len(requests), 3)
        self.assertEqual(
            [row["candidate_id"] for row in issues if row.get("failed")], ["c1"]
        )

    def test_retry_logs_do_not_expose_model_values(self) -> None:
        """真实Instructor校验失败不能把模型响应写到第三方日志。"""
        client, requests = self.make_client([self.reply("PRIVATE_OUTPUT")], attempts=1)
        output = io.StringIO()
        logger = logging.getLogger("instructor.v2.retry")
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        try:
            with self.assertRaises(InstructorRetryException):
                client.call(
                    Selection,
                    [{"role": "user", "content": "select"}],
                    context={"allowed_ids": ["p2"]},
                )
            logger.warning("UNRELATED_CALLER_VISIBLE")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("PRIVATE_OUTPUT", output.getvalue())
        self.assertIn("UNRELATED_CALLER_VISIBLE", output.getvalue())

    def test_cancelled_inflight_keeps_usage_and_blocks_reask(self) -> None:
        """取消中途请求仍记真实usage，但错误响应不再reask且新调用零HTTP。"""
        client, requests = self.make_client([self.reply("unknown")])
        entered, release = Event(), Event()
        create = client._create

        def blocked_create(*args: Any, **kwargs: Any) -> Any:
            """模拟已经进入SDK的在途请求，取消不抹掉其真实响应。"""
            entered.set()
            if not release.wait(5):
                raise AssertionError("未释放在途请求")
            return create(*args, **kwargs)

        client._create = blocked_create
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)

            def invoke() -> Selection:
                """绑定独立执行身份后调用实际Instructor与HTTP桩。"""
                with client.scope(log=log, book_id="book", execution_id="execution"):
                    return client.call(
                        Selection,
                        [{"role": "user", "content": "select"}],
                        context={"allowed_ids": ["p2"]},
                    )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(invoke)
                self.assertTrue(entered.wait(5))
                client.cancel()
                release.set()
                with self.assertRaises(ExtractionCancelledError):
                    future.result(timeout=5)
            with self.assertRaises(ExtractionCancelledError):
                invoke()
            log.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]
            finished = [e for e in events if e["event"] == "request_finished"]
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(finished), 1)
            self.assertTrue(finished[0]["http_success"])
            self.assertEqual(finished[0]["usage"]["total_tokens"], 15)
            self.assertEqual(finished[0]["book_id"], "book")
            self.assertEqual(finished[0]["execution_id"], "execution")

    def test_cancel_wakes_native_retry_backoff(self) -> None:
        """取消唤醒长退避等待；原生重试不能发送第二个SDK请求。"""
        client, requests = self.make_client([self.reply("unknown")])
        waiting = Event()

        def backoff(state: Any) -> float:
            """标记已进入退避计算并给出长等待，供取消信号立即唤醒。"""
            waiting.set()
            return 60.0

        with (
            patch("book_extractor.llm._retry_wait", side_effect=backoff),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(
                client.call,
                Selection,
                [{"role": "user", "content": "select"}],
                context={"allowed_ids": ["p2"]},
            )
            self.assertTrue(waiting.wait(5))
            client.cancel()
            with self.assertRaises(ExtractionCancelledError):
                future.result(timeout=2)
        self.assertEqual(len(requests), 1)

    def test_connection_pool_is_configurable_without_cache_identity_change(
        self,
    ) -> None:
        """连接数只影响传输容量，默认100且不改变语义缓存身份。"""
        default = LLMClient("https://example.test/v1", "test", "test")
        expanded = LLMClient(
            "https://example.test/v1", "test", "test", max_connections=192
        )
        try:
            self.assertEqual(
                default._sdk._client._transport._pool._max_connections, 100
            )
            self.assertEqual(
                expanded._sdk._client._transport._pool._max_connections, 192
            )
            self.assertEqual(default.identity, expanded.identity)
        finally:
            default.close()
            expanded.close()
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                LLMClient(
                    "https://example.test", "test", "test", max_connections=invalid
                )

    def test_service_url_uses_exact_path_segment(self) -> None:
        """读取临时配置验证 URL 边界；保留查询片段且不泄露或依赖真实服务配置。"""
        cases = {
            "https://example.test": "https://example.test/v1",
            "https://example.test///": "https://example.test/v1",
            "https://example.test/maas/": "https://example.test/maas/v1",
            "https://example.test/v1/": "https://example.test/v1",
            "https://example.test/v1/gateway/": "https://example.test/v1/gateway",
            "https://v1.example.test/v10/": "https://v1.example.test/v10/v1",
            "https://example.test/v1proxy": "https://example.test/v1proxy/v1",
            "https://example.test/api?next=/v1#part": (
                "https://example.test/api/v1?next=/v1#part"
            ),
            "https://example.test/v1/?token=test#fragment": (
                "https://example.test/v1?token=test#fragment"
            ),
            "http://[::1]:8000/": "http://[::1]:8000/v1",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "services.json"
            for original, expected in cases.items():
                with self.subTest(url=original):
                    path.write_text(
                        json.dumps(
                            {
                                "services": {
                                    "test": {
                                        "base_url": original,
                                        "api_key": "offline",
                                        "model": "test",
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(load_service(path, "test")["base_url"], expected)
            for invalid in ("example.test/v1", "file:///tmp/api", "https://"):
                path.write_text(
                    json.dumps(
                        {
                            "services": {
                                "test": {
                                    "base_url": invalid,
                                    "api_key": "offline",
                                    "model": "test",
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                with self.subTest(url=invalid), self.assertRaises(ValueError):
                    load_service(path, "test")

    def test_service_selection_requires_unambiguous_config(self) -> None:
        """验证显式选择、配置默认和唯一服务；歧义或缺模型时抛异常，不猜测供应商。"""
        first = {"base_url": "https://one.test", "api_key": "offline", "model": "one"}
        second = {**first, "base_url": "https://two.test", "model": "two"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "services.json"
            path.write_text(json.dumps({"services": {"only": first}}), encoding="utf-8")
            self.assertEqual(load_service(path)["model"], "one")
            config = {
                "default": "second",
                "services": {"first": first, "second": second},
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_service(path)["model"], "two")
            self.assertEqual(
                load_service(path, "first", "override")["model"], "override"
            )
            del config["default"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_service(path)
            with self.assertRaises(ValueError):
                load_service(path, "missing")
            first["model"] = None
            path.write_text(json.dumps({"services": {"only": first}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_service(path)
            self.assertEqual(load_service(path, model="explicit")["model"], "explicit")

    def make_client(
        self, replies: list[tuple[int, dict[str, Any]]], **kwargs: Any
    ) -> tuple[LLMClient, list[dict[str, Any]]]:
        """根据响应序列创建带模拟传输的真实客户端并返回请求列表；

        测试结束自动关闭连接。
        """
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """记录模拟请求并返回预设 HTTP 响应，不执行网络访问。"""
            requests.append(json.loads(request.content))
            status, body = replies[min(len(requests) - 1, len(replies) - 1)]
            return httpx.Response(status, json=body)

        def sdk_factory(**settings: Any) -> OpenAI:
            """为生产 SDK 构造参数注入模拟传输；保留响应钩子并关闭原空连接池。"""
            original_http = settings.pop("http_client")
            hooks = original_http.event_hooks
            original_http.close()
            return OpenAI(
                **settings,
                http_client=httpx.Client(
                    transport=httpx.MockTransport(handler), event_hooks=hooks
                ),
            )

        with patch("book_extractor.llm.OpenAI", side_effect=sdk_factory):
            client = LLMClient("http://stub.invalid/v1", "stub", "stub", **kwargs)
        self.addCleanup(client.close)
        return client, requests

    @staticmethod
    def reply(value: str = "p2", finish: str = "stop") -> tuple[int, dict[str, Any]]:
        """按给定引用与终止原因生成 OpenAI 兼容的桩响应，包含已知 usage。"""
        return 200, {
            "id": "stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"ids": [value]}),
                    },
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def test_validation_reask_and_usage(self) -> None:
        """验证未知证据触发一次原生反馈重试，两次请求的用量完整累计。"""
        client, requests = self.make_client([self.reply("unknown"), self.reply()])
        result = client.call(
            Selection,
            [{"role": "user", "content": "Select p2."}],
            context={"allowed_ids": ["p2"]},
        )
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(len(requests), 2)
        self.assertIn("allowed_ids", json.dumps(requests[1]["messages"]))
        self.assertEqual(client.stats["prompt_tokens"], 20)
        self.assertEqual(client.stats["calls"], 2)
        self.assertEqual(client.last_call_attempts, 2)

    def test_temperature_and_native_json_schema_request(self) -> None:
        """温度和原生 Schema 模式进入实际桩请求，缓存身份区分不同温度。"""
        client, requests = self.make_client(
            [self.reply()], temperature=0.7, mode="json_schema"
        )
        messages = [
            {"role": "system", "content": "Keep supplied IDs."},
            {"role": "user", "content": "Select p2."},
        ]
        original = json.loads(json.dumps(messages))
        result = client.call(
            Selection,
            messages,
            context={"allowed_ids": ["p2"]},
        )
        self.assertEqual(messages, original)
        system = requests[0]["messages"][0]["content"]
        self.assertIn("返回符合该Schema的JSON实例，不返回Schema本身", system)
        self.assertIn(
            json.dumps(Selection.model_json_schema(), ensure_ascii=False), system
        )
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(requests[0]["temperature"], 0.7)
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")
        self.assertIn("schema", requests[0]["response_format"]["json_schema"])
        default, _ = self.make_client([self.reply()], mode="json_schema")
        self.assertEqual(default.temperature, 0.0)
        self.assertNotEqual(default.identity, client.identity)
        self.assertEqual(client.identity["temperature"], 0.7)
        for temperature in (
            True,
            False,
            None,
            "0.7",
            -0.1,
            2.1,
            float("nan"),
            float("inf"),
        ):
            with self.subTest(temperature=temperature), self.assertRaises(ValueError):
                self.make_client([self.reply()], temperature=temperature)
        with self.assertRaises(ValueError):
            self.make_client([self.reply()], extra_body={"temperature": 0.7})

    def test_source_is_literal_during_validation_reask(self) -> None:
        """验证模板符号、缩进及 CRLF 在验证重试前后保持原样，不被当作模板执行。"""
        source = "  {{ missing }} {% broken {# comment #}\r\n  {% endraw %} {{7*7}}\r\n"
        client, requests = self.make_client([self.reply("unknown"), self.reply()])
        result = client.call(
            Selection,
            [{"role": "user", "content": source}],
            context={"allowed_ids": ["p2"]},
        )
        self.assertIs(type(result), Selection)
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(len(requests), 2)
        self.assertIn("allowed_ids", json.dumps(requests[1]["messages"]))
        for request in requests:
            original_user = next(m for m in request["messages"] if m["role"] == "user")
            self.assertEqual(original_user["content"], source)

    def test_member_evidence_reask_has_precise_location_and_trusted_ids(self) -> None:
        """错挂其他成员来源时原生纠错给出准确位置和可信ID，第二响应合法通过。"""
        context = {
            "draft_ids": ["d0", "d1"],
            "evidence_ids": ["u000001", "u000002"],
            "candidate_groups": [["d0"], ["d1"]],
            "candidate_evidence_ids": {"d0": ["u000001"], "d1": ["u000002"]},
            "candidate_body_ids": {"d0": ["u000001"], "d1": ["u000002"]},
        }
        valid = {
            "name_decisions": [
                {
                    "candidate_id": f"d{i}",
                    "decision": "accept",
                    "reason": "source",
                    "evidence_ids": [f"u{i + 1:06d}"],
                }
                for i in range(2)
            ],
            "findings": [
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": False,
                    "confidence": "high",
                    "draft_ids": [f"d{i}"],
                    "name": f"n{i}",
                    "definition": None,
                    "aliases": [],
                    "category": "concept",
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": [f"u{i + 1:06d}"],
                }
                for i in range(2)
            ],
        }
        cases = [
            (field, regenerated)
            for field in ("name_decisions", "findings")
            for regenerated in (False, True)
        ]
        for field, regenerated in cases:
            with self.subTest(field=field, regenerated=regenerated):
                invalid = json.loads(json.dumps(valid))
                invalid[field][0]["evidence_ids"] = ["u000002"]
                invalid[field][0]["reason" if field == "name_decisions" else "name"] = (
                    "PRIVATE_MODEL_TEXT " * (4000 if regenerated else 1)
                )
                with self.assertRaises(ValidationError) as failure:
                    SynthesisReview.model_validate(invalid, context=context)
                message = str(failure.exception)
                self.assertNotIn("PRIVATE_MODEL_TEXT", message)
                self.assertEqual(
                    LLMClient._error_fields(failure.exception)["validation_errors"][0][
                        "loc"
                    ],
                    [field, 0, "evidence_ids"],
                )
                replies = [self.reply(), self.reply()]
                for reply, value in zip(replies, [invalid, valid]):
                    reply[1]["choices"][0]["message"]["content"] = json.dumps(value)
                client, requests = self.make_client(
                    replies, context_limit=8000, max_output_tokens=1000
                )
                with patch("book_extractor.llm._retry_wait", return_value=0):
                    result = client.call(
                        SynthesisReview,
                        [
                            {
                                "role": "user",
                                "content": "Use supplied members and sources.",
                            }
                        ],
                        context=context,
                    )
                self.assertEqual(len(result.findings), 2)
                self.assertEqual(len(requests), 2)
                correction = requests[1]["messages"][-1]["content"]
                if regenerated:
                    self.assertIn(f'"loc": ["{field}", 0, "evidence_ids"]', correction)
                    self.assertIn('"candidate_ids": ["d0"]', correction)
                    self.assertIn('"allowed_evidence_ids": ["u000001"]', correction)
                    self.assertEqual(
                        requests[1]["messages"][:-1], requests[0]["messages"]
                    )
                else:
                    self.assertIn(f"{field}.0.evidence_ids", correction)
                    self.assertIn("candidate_ids=['d0']", correction)
                    self.assertIn("allowed_evidence_ids=['u000001']", correction)
                self.assertNotIn("PRIVATE_MODEL_TEXT", correction)

    def test_partition_reask_names_only_trusted_missing_ids(self) -> None:
        """验证缺失 ID 进入原生反馈且分区规则仍拒绝漏项和重复；未知值只计数。"""
        expected = ["d0000", "d0001"]
        invalid = {
            "findings": [],
            "name_decisions": [
                {
                    "candidate_id": key,
                    "decision": "reject",
                    "reason": "noise",
                    "evidence_ids": ["u000001"],
                }
                for key in ["d0000", "d0000", "PRIVATE_UNEXPECTED_ID"]
            ],
        }
        valid = {
            **invalid,
            "name_decisions": [
                {
                    "candidate_id": key,
                    "decision": "reject",
                    "reason": "noise",
                    "evidence_ids": ["u000001"],
                }
                for key in expected
            ],
        }
        with self.assertRaises(ValidationError) as failure:
            SynthesisReview.model_validate(invalid, context={"draft_ids": expected})
        self.assertEqual(
            failure.exception.errors(include_input=False)[0]["type"],
            "name_decision_partition",
        )
        self.assertEqual(
            LLMClient._error_fields(failure.exception)["validation_errors"],
            [{"type": "name_decision_partition", "loc": []}],
        )
        message = failure.exception.errors(include_input=False)[0]["msg"]
        self.assertIn("missing_expected_ids=['d0001']", message)
        self.assertIn("duplicate_expected_ids=['d0000']", message)
        self.assertIn("unexpected_count=1", message)
        self.assertNotIn("PRIVATE_UNEXPECTED_ID", message)
        for ids in (["d0000"], ["d0000", "d0000"]):
            with self.assertRaises(ValidationError):
                Grouping.model_validate(
                    {"groups": [{"candidate_ids": ids, "status": "same"}]},
                    context={"candidate_ids": expected},
                )
            finding = {
                "support_reason": "测试来源支持范围",
                "definition_supported": False,
                "confidence": "high",
                "name": "n",
                "aliases": [],
                "category": "concept",
                "definition": None,
                "conditions": [],
                "issues": [],
                "evidence_ids": ["u000001"],
                "draft_ids": ids,
            }
            with self.assertRaises(ValidationError):
                SynthesisReview.model_validate(
                    {"findings": [finding]}, context={"draft_ids": expected}
                )
        replies = [self.reply(), self.reply()]
        for reply, payload in zip(replies, [invalid, valid]):
            reply[1]["choices"][0]["message"]["content"] = json.dumps(payload)
        client, requests = self.make_client(replies)
        result = client.call(
            SynthesisReview,
            [{"role": "user", "content": "Account for d0000 and d0001."}],
            context={"draft_ids": expected},
        )
        self.assertEqual(
            [draft.candidate_id for draft in result.name_decisions], expected
        )
        self.assertEqual(len(requests), 2)
        self.assertIn(
            "missing_expected_ids=['d0001']", json.dumps(requests[1]["messages"])
        )
        self.assertIn("name_decision_partition", json.dumps(requests[1]["messages"]))
        bounded = partition_error(
            "prefix", [], [f"d{index:04d}" for index in range(25)]
        )
        self.assertIn("d0019", bounded)
        self.assertNotIn("d0020", bounded)
        self.assertTrue(bounded.startswith("prefix"))

    def test_nonretryable_and_truncated(self) -> None:
        """验证鉴权、错误参数及截断响应不重复请求，每种场景仅调用一次。"""
        for status in [400, 401, 402, 403, 404]:
            with self.subTest(status=status):
                client, requests = self.make_client(
                    [
                        (
                            status,
                            {
                                "error": {
                                    "message": "invalid request",
                                    "type": "invalid_request_error",
                                }
                            },
                        )
                    ]
                )
                with self.assertRaises(APIStatusError):
                    client.call(Selection, [{"role": "user", "content": "Select p2."}])
                self.assertEqual(len(requests), 1)
        client, requests = self.make_client([self.reply(finish="length")])
        with self.assertRaises(IncompleteOutputException):
            client.call(Selection, [{"role": "user", "content": "Select p2."}])
        self.assertEqual(len(requests), 1)

    def test_budget_includes_generated_schema(self) -> None:
        """验证预算包含 Instructor 生成的 Schema，超限在 HTTP 前明确拒绝。"""
        client, requests = self.make_client(
            [self.reply()], context_limit=120, max_output_tokens=100
        )
        with self.assertRaises(ContextBudgetError):
            client.call(Selection, [{"role": "user", "content": "p2"}])
        self.assertEqual(requests, [])
        self.assertEqual(client.stats["budget_rejections"], 1)
        self.assertEqual(client.last_call_attempts, 0)

    def test_input_is_preserved_when_output_reservation_uses_remaining_budget(
        self,
    ) -> None:
        """输入完整保留，只缩输出上限；余量不足4096时仍零HTTP拒绝，账本记实际预算。"""
        messages = [{"role": "user", "content": "Original source. " * 100}]
        measured, first_requests = self.make_client([self.reply()])
        measured.call(Selection, messages, context={"allowed_ids": ["p2"]})
        input_tokens = measured._count_request_tokens(first_requests[0])
        for headroom in (6000, 3000):
            with (
                self.subTest(headroom=headroom),
                tempfile.TemporaryDirectory() as folder,
            ):
                client, requests = self.make_client(
                    [self.reply()],
                    context_limit=input_tokens + headroom,
                )
                log = EventLog(Path(folder) / "events.jsonl")
                try:
                    with client.scope(log=log):
                        if headroom == 3000:
                            with self.assertRaises(ContextBudgetError):
                                client.call(Selection, messages)
                        else:
                            result = client.call(
                                Selection, messages, context={"allowed_ids": ["p2"]}
                            )
                            self.assertEqual(result.ids, ["p2"])
                finally:
                    log.close()
                if headroom == 3000:
                    self.assertEqual(requests, [])
                    continue
                self.assertEqual(requests[0]["messages"], first_requests[0]["messages"])
                self.assertLess(requests[0]["max_tokens"], client.max_output_tokens)
                self.assertGreaterEqual(requests[0]["max_tokens"], 4096)
                events = [
                    json.loads(line) for line in log.path.read_text().splitlines()
                ]
                started = next(
                    row for row in events if row["event"] == "request_started"
                )
                self.assertEqual(started["requested_output_tokens"], 8192)
                self.assertEqual(
                    started["reserved_output_tokens"], requests[0]["max_tokens"]
                )
                self.assertLessEqual(
                    started["input_tokens"] + started["reserved_output_tokens"],
                    client.context_limit,
                )
                self.assertEqual(
                    summarize_events(log.path)["totals"]["output_budget_reductions"], 1
                )

    def test_parse_diagnostics_survive_later_budget_rejection(self) -> None:
        """两次 HTTP 成功但验证失败后，最终预算错误不能掩盖中间字段诊断。"""
        client, requests = self.make_client(
            [self.reply("PRIVATE_VALIDATION_INPUT")],
            context_limit=2000,
            max_output_tokens=100,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path, execution_id="offline-parse")
            try:
                with (
                    client.scope(log=log, stage="review", execution_id="offline-parse"),
                    patch.object(
                        client,
                        "_count_request_tokens",
                        side_effect=[0, 1, 2000, 2000],
                    ),
                    patch("book_extractor.llm._retry_wait", return_value=0),
                    self.assertRaises(ContextBudgetError),
                ):
                    client.call(
                        Selection,
                        [{"role": "user", "content": "PRIVATE_SOURCE_TEXT"}],
                        context={"allowed_ids": ["p2"]},
                    )
            finally:
                log.close()
            serialized = path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in serialized.splitlines()]
        self.assertEqual(len(requests), 2)
        failures = [event for event in events if event["event"] == "parse_failed"]
        self.assertEqual([event["attempt"] for event in failures], [1, 2])
        responses = {
            event["request_id"]: event
            for event in events
            if event["event"] == "request_finished"
        }
        for event in failures:
            self.assertEqual(event["exception_type"], "ValidationError")
            self.assertEqual(
                event["validation_errors"], [{"type": "value_error", "loc": ["ids"]}]
            )
            self.assertEqual(event["execution_id"], "offline-parse")
            self.assertEqual(event["http_status"], 200)
            self.assertEqual(
                event["call_id"], responses[event["request_id"]]["call_id"]
            )
        final = next(event for event in events if event["event"] == "call_finished")
        self.assertEqual(final["exception_type"], "ContextBudgetError")
        self.assertNotIn("PRIVATE_VALIDATION_INPUT", serialized)
        self.assertNotIn("PRIVATE_SOURCE_TEXT", serialized)

    def test_budget_regenerates_within_attempt_limit_without_source_loss(
        self,
    ) -> None:
        """仅超预算的JSON纠错删除失败生成文本，原Schema来源不变且不增加尝试数。"""
        for mode in ("json", "json_schema"):
            for succeeds in (True, False):
                with self.subTest(mode=mode, succeeds=succeeds):
                    invalid = "PRIVATE_GENERATED " * 4000
                    client, requests = self.make_client(
                        [
                            self.reply(invalid),
                            self.reply(invalid),
                            self.reply() if succeeds else self.reply(invalid),
                        ],
                        mode=mode,
                        context_limit=2000,
                        max_output_tokens=100,
                    )
                    messages = [{"role": "user", "content": "ORIGINAL {{source}}"}]
                    original = json.loads(json.dumps(messages))
                    with tempfile.TemporaryDirectory() as folder:
                        log = EventLog(Path(folder) / "events.jsonl")
                        try:
                            with (
                                client.scope(log=log),
                                patch("book_extractor.llm._retry_wait", return_value=0),
                            ):
                                if succeeds:
                                    self.assertEqual(
                                        client.call(
                                            Selection,
                                            messages,
                                            context={"allowed_ids": ["p2"]},
                                        ).ids,
                                        ["p2"],
                                    )
                                else:
                                    with self.assertRaises(InstructorRetryException):
                                        client.call(
                                            Selection,
                                            messages,
                                            context={"allowed_ids": ["p2"]},
                                        )
                        finally:
                            log.close()
                        events = [
                            json.loads(line)
                            for line in log.path.read_text().splitlines()
                        ]
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(messages, original)
                    self.assertEqual(
                        requests[1]["messages"][:-1], requests[0]["messages"]
                    )
                    self.assertNotIn("PRIVATE_GENERATED", json.dumps(requests[1]))
                    self.assertEqual(
                        requests[2]["messages"][:-1], requests[0]["messages"]
                    )
                    self.assertNotIn("PRIVATE_GENERATED", json.dumps(requests[2]))
                    self.assertIn("value_error", requests[1]["messages"][-1]["content"])
                    repairs = [
                        event
                        for event in events
                        if event["event"] == "reask_regenerated"
                    ]
                    self.assertEqual(len(repairs), 2)
                    self.assertEqual([event["attempt"] for event in repairs], [2, 3])
                    self.assertIsNotNone(repairs[0]["call_id"])
                    self.assertLess(
                        repairs[0]["input_tokens"],
                        repairs[0]["previous_input_tokens"],
                    )

    def test_json_reask_keeps_only_latest_pair_and_exact_budget_payload(self) -> None:
        """两种JSON模式保留原请求与最新完整纠错，计量和HTTP使用相同消息。"""
        for mode in ["json", "json_schema"]:
            with self.subTest(mode=mode):
                client, requests = self.make_client(
                    [
                        self.reply("first_unknown"),
                        self.reply("latest_unknown"),
                        self.reply(),
                    ],
                    mode=mode,
                )
                messages = [
                    {"role": "system", "content": "Preserve source."},
                    {"role": "user", "content": "SOURCE {{literal}}"},
                ]
                original = json.loads(json.dumps(messages))
                with tempfile.TemporaryDirectory() as folder:
                    log = EventLog(Path(folder) / "events.jsonl")
                    try:
                        with (
                            client.scope(log=log),
                            patch("book_extractor.llm._retry_wait", return_value=0),
                            patch.object(
                                client,
                                "_count_request_tokens",
                                wraps=client._count_request_tokens,
                            ) as encode,
                        ):
                            result = client.call(
                                Selection, messages, context={"allowed_ids": ["p2"]}
                            )
                        events = [
                            json.loads(line)
                            for line in log.path.read_text().splitlines()
                        ]
                    finally:
                        log.close()
                self.assertEqual(result.ids, ["p2"])
                self.assertEqual(messages, original)
                base = requests[0]["messages"]
                self.assertEqual(
                    [len(row["messages"]) for row in requests],
                    [len(base), len(base) + 2, len(base) + 2],
                )
                self.assertEqual(requests[2]["messages"][: len(base)], base)
                self.assertIn("latest_unknown", requests[2]["messages"][-2]["content"])
                self.assertIn("latest_unknown", requests[2]["messages"][-1]["content"])
                self.assertNotIn("first_unknown", json.dumps(requests[2]["messages"]))
                measured = encode.call_args_list[-1].args[0]
                self.assertEqual(measured["messages"], requests[2]["messages"])
                self.assertEqual(
                    requests[0]["response_format"], requests[2]["response_format"]
                )
                starts = [row for row in events if row["event"] == "request_started"]
                self.assertEqual(
                    [row["removed_reask_messages"] for row in starts], [0, 0, 2]
                )
                self.assertEqual(starts[-1]["removed_reask_rounds"], 1)
                self.assertIsNone(client._thread.initial_messages)

    def test_reask_compaction_is_independent_between_threads(self) -> None:
        """同一客户端交错重试时，每线程只保留自己的原文和最近失败响应。"""
        client, _ = self.make_client([self.reply()])
        barrier = Barrier(2)
        lock = Lock()
        requests: dict[str, list[list[dict[str, Any]]]] = {"A": [], "B": []}

        def reply_for_thread(**kwargs: Any) -> ChatCompletion:
            """同步两线程的每轮桩响应，让历史状态泄漏能在离线测试中显现。"""
            messages = json.loads(json.dumps(kwargs["messages"]))
            tag = (
                "A"
                if any(message.get("content") == "thread-A" for message in messages)
                else "B"
            )
            barrier.wait(timeout=5)
            with lock:
                requests[tag].append(messages)
                attempt = len(requests[tag])
            _, body = self.reply(f"bad-{tag}-{attempt}" if attempt < 3 else "p2")
            return ChatCompletion.model_validate(body)

        def invoke(tag: str) -> Selection:
            """用线程特有原文调用同一客户端，不共享可变消息列表。"""
            return client.call(
                Selection,
                [{"role": "user", "content": f"thread-{tag}"}],
                context={"allowed_ids": ["p2"]},
            )

        with (
            patch.object(client, "_create", side_effect=reply_for_thread),
            patch("book_extractor.llm._retry_wait", return_value=0),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(invoke, ["A", "B"]))
        self.assertTrue(all(result.ids == ["p2"] for result in results))
        for tag, attempts in requests.items():
            self.assertEqual(len(attempts), 3)
            self.assertEqual(attempts[-1][:-2], attempts[0])
            text = json.dumps(attempts[-1])
            self.assertIn(f"bad-{tag}-2", text)
            self.assertNotIn(f"bad-{tag}-1", text)
            other = "B" if tag == "A" else "A"
            self.assertNotIn(f"thread-{other}", text)
            self.assertNotIn(f"bad-{other}", text)

    def test_tool_reask_history_is_not_compacted(self) -> None:
        """工具调用保持完整assistant/tool配对，不套用JSON消息裁剪。"""
        replies = []
        for index, identity in enumerate(["wrong1", "wrong2", "p2"]):
            status, body = self.reply(identity)
            body["choices"][0]["message"] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "Selection",
                            "arguments": json.dumps({"ids": [identity]}),
                        },
                    }
                ],
            }
            replies.append((status, body))
        client, requests = self.make_client(replies, mode="tools")
        messages = [{"role": "user", "content": "Choose an ID."}]
        original = json.loads(json.dumps(messages))
        with patch("book_extractor.llm._retry_wait", return_value=0):
            result = client.call(Selection, messages, context={"allowed_ids": ["p2"]})
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(messages, original)
        self.assertEqual([len(row["messages"]) for row in requests], [1, 3, 5])
        self.assertEqual(
            [row["role"] for row in requests[-1]["messages"]],
            ["user", "assistant", "tool", "assistant", "tool"],
        )

    def test_retry_limit_is_total_attempts(self) -> None:
        """验证尝试上限包含首次调用，持续无效的响应不会额外请求一次。"""
        client, requests = self.make_client([self.reply("unknown")], attempts=2)
        with self.assertRaises(InstructorRetryException):
            client.call(
                Selection,
                [{"role": "user", "content": "Select p2."}],
                context={"allowed_ids": ["p2"]},
            )
        self.assertEqual(len(requests), 2)

    def test_transient_status_recovers(self) -> None:
        """验证临时服务错误在统一预算内重试并成功，不启用第二层 SDK 重试。"""
        client, requests = self.make_client(
            [(503, {"error": {"message": "temporarily unavailable"}}), self.reply()]
        )
        result = client.call(
            Selection,
            [{"role": "user", "content": "Select p2."}],
            context={"allowed_ids": ["p2"]},
        )
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(len(requests), 2)

    def test_gateway_budget_does_not_consume_validation_attempts(self) -> None:
        """真实Instructor链路中504与纠错交错，仍保留三次普通尝试并完整记账。"""
        gateway = (504, {"error": {"message": "gateway timeout"}})
        client, requests = self.make_client(
            [gateway, self.reply("bad"), gateway, self.reply("bad"), self.reply()],
            gateway_retries=2,
        )
        with tempfile.TemporaryDirectory() as folder:
            log = EventLog(Path(folder) / "events.jsonl")
            with (
                client.scope(log=log),
                patch("book_extractor.llm._retry_wait", return_value=0),
            ):
                result = client.call(
                    Selection,
                    [{"role": "user", "content": "select"}],
                    context={"allowed_ids": ["p2"]},
                )
            log.close()
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
        self.assertEqual(result.ids, ["p2"])
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0]["messages"], requests[1]["messages"])
        self.assertEqual(requests[2]["messages"], requests[3]["messages"])
        retries = [row for row in events if row["event"] == "retry_scheduled"]
        self.assertEqual(
            [row["retry_kind"] for row in retries],
            ["gateway_504", "ordinary", "gateway_504", "ordinary"],
        )
        self.assertEqual(events[-1]["actual_attempts"], 5)
        self.assertEqual(client.last_call_ordinary_attempts, 3)

    def test_gateway_retries_are_bounded_and_cancellable(self) -> None:
        """连续504额度耗尽即停止；额外退避能被取消唤醒，不再发送请求。"""
        gateway = (504, {"error": {"message": "gateway timeout"}})
        for limit in (0, 2):
            client, requests = self.make_client([gateway], gateway_retries=limit)
            with patch("book_extractor.llm._retry_wait", return_value=0):
                with self.assertRaises(APIStatusError):
                    client.call(Selection, [{"role": "user", "content": "select"}])
            self.assertEqual(len(requests), limit + 1)
        client, requests = self.make_client(
            [gateway, self.reply("unknown"), gateway, gateway], gateway_retries=2
        )
        with patch("book_extractor.llm._retry_wait", return_value=0):
            with self.assertRaises(APIStatusError):
                client.call(
                    Selection,
                    [{"role": "user", "content": "select"}],
                    context={"allowed_ids": ["p2"]},
                )
        self.assertEqual(len(requests), 4)
        client, requests = self.make_client([gateway])
        waiting = Event()

        def backoff(state: RetryCallState) -> float:
            """标记已收到504并进入退避，返回长等待用于验证取消。"""
            waiting.set()
            return 120.0

        with (
            patch("book_extractor.llm._retry_wait", side_effect=backoff),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(
                client.call, Selection, [{"role": "user", "content": "select"}]
            )
            self.assertTrue(waiting.wait(5))
            client.cancel()
            with self.assertRaises(ExtractionCancelledError):
                future.result(timeout=2)
        self.assertEqual(len(requests), 1)

    def test_transport_failures_defer_without_consuming_full_gateway_budget(
        self,
    ) -> None:
        """两次传输故障即释放工作，下一次独立调用仍可成功，不能无限拖住书籍。"""
        gateway = (504, {"error": {"message": "gateway timeout"}})
        client, requests = self.make_client(
            [gateway, gateway, self.reply()],
            gateway_retries=10,
            transport_failure_limit=2,
        )
        try:
            with patch("book_extractor.llm._retry_wait", return_value=0):
                with self.assertRaises(DeferredCallError):
                    client.call(
                        Selection,
                        [{"role": "user", "content": "select"}],
                    )
                self.assertEqual(len(requests), 2)
                self.assertEqual(client.request_queue_state["active"], 0)
                result = client.call(
                    Selection,
                    [{"role": "user", "content": "select"}],
                    context={"allowed_ids": ["p2"]},
                )
                self.assertEqual(result.ids, ["p2"])
        finally:
            client.close()

    def test_gateway_backoff_and_config_validation(self) -> None:
        """504使用较长有界退避，配置拒绝负数、布尔和非整数。"""
        response = httpx.Response(
            504, request=httpx.Request("GET", "http://stub.invalid")
        )
        error = APIStatusError("timeout", response=response, body=None)
        state = RetryCallState(Retrying(), None, (), {})
        state.set_exception((type(error), error, None))
        with patch("book_extractor.llm.random.uniform", return_value=5):
            for attempt, expected in ((1, 35), (2, 65), (3, 125), (10, 125)):
                state.attempt_number = attempt
                self.assertEqual(_retry_wait(state), expected)
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.make_client([self.reply()], gateway_retries=value)

    def test_retry_after_numeric_date_and_fallback(self) -> None:
        """验证数字与日期等待头、上限和无效值回退，不依赖真实时钟或休眠。"""
        cases = [
            ("12", 12),
            ("120", 60),
            ("1.5", 1.5),
            ("0", 0),
            ("bad", 2),
            ("-1", 2),
            ("nan", 2),
            ("inf", 2),
            ("Thu, 01 Jan 1970 00:00:20 GMT", 10),
            ("Thu, 01 Jan 1970 00:00:05 GMT", 0),
        ]
        for header, expected in cases:
            with self.subTest(header=header):
                response = httpx.Response(
                    429,
                    headers={"Retry-After": header},
                    request=httpx.Request("GET", "http://stub.invalid"),
                )
                error = APIStatusError("temporary", response=response, body=None)
                state = RetryCallState(Retrying(), None, (), {})
                state.set_exception((type(error), error, None))
                with patch("book_extractor.llm.time.time", return_value=10):
                    self.assertEqual(_retry_wait(state), expected)
        state = RetryCallState(Retrying(), None, (), {})
        state.attempt_number = 2
        self.assertEqual(_retry_wait(state), 8)

    def test_caller_can_reduce_remaining_budget(self) -> None:
        """验证上层可收紧剩余尝试预算，验证反馈也服从该限制。"""
        client, requests = self.make_client([self.reply("unknown")])
        with self.assertRaises(InstructorRetryException):
            client.call(
                Selection, [{"role": "user", "content": "Select p2."}], attempts=1
            )
        self.assertEqual(len(requests), 1)
        self.assertEqual(client.last_call_attempts, 1)
        with self.assertRaises(ValueError):
            client.call(Selection, [], attempts=0)
        self.assertEqual(client.last_call_attempts, 0)
        self.assertNotIn("api_key", client.identity)
        self.assertNotIn("base_url", client.identity)

    def test_events_separate_http_reask_and_parse_success(self) -> None:
        """验证 HTTP 成功与结构校验分开记账，缺少 usage 的字段保留 None。"""
        first = self.reply("unknown")
        first[1]["usage"].update(
            prompt_tokens_details={"cached_tokens": 4},
            completion_tokens_details={"reasoning_tokens": 2},
        )
        second = self.reply()
        second[1].pop("usage")
        client, requests = self.make_client([first, second])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path, execution_id="exec", run_id="run", book_id="book")
            with client.scope(log=log, stage="discovery", work_id="chunk"):
                result = client.call(
                    Selection,
                    [{"role": "user", "content": "PRIVATE BOOK TEXT"}],
                    context={"allowed_ids": ["p2"]},
                )
            log.close()
            self.assertEqual(result.ids, ["p2"])
            events = [json.loads(line) for line in path.read_text().splitlines()]
            finished = [
                event for event in events if event["event"] == "request_finished"
            ]
            self.assertEqual([event["http_status"] for event in finished], [200, 200])
            queued = [event for event in events if event["event"] == "request_queued"]
            self.assertEqual(len(queued), 2)
            self.assertTrue(all(event["call_id"] for event in queued))
            self.assertEqual(
                [event["call_id"] for event in queued],
                [event["call_id"] for event in finished],
            )
            self.assertIsNone(finished[1]["usage"]["prompt_tokens"])
            self.assertNotIn("PRIVATE BOOK TEXT", path.read_text())
            self.assertTrue(all(event["execution_id"] == "exec" for event in events))
            summary = summarize_events(path)
            self.assertEqual(summary["totals"]["retries"], 1)
            self.assertEqual(summary["totals"]["parse_success"], 1)
            self.assertEqual(summary["usage"]["prompt_tokens"], 10)
            self.assertEqual(summary["usage"]["cached_tokens"], 4)
            self.assertEqual(summary["usage_missing"]["prompt_tokens"], 1)
            self.assertFalse(summary["usage_is_complete"])
            self.assertEqual(len(requests), 2)

    def test_accounting_failure_never_repeats_completed_http(self) -> None:
        """验证完成响应后的日志同步失败会终止执行，不会重发已完成请求。"""
        client, requests = self.make_client([self.reply()])
        with tempfile.TemporaryDirectory() as folder:
            log = EventLog(Path(folder) / "events.jsonl")
            try:
                with (
                    client.scope(log=log),
                    patch(
                        "book_extractor.telemetry.os.fsync",
                        side_effect=[None, None, None, OSError("disk full")],
                    ),
                ):
                    with self.assertRaises(TelemetryWriteError):
                        client.call(
                            Selection,
                            [{"role": "user", "content": "p2"}],
                            context={"allowed_ids": ["p2"]},
                        )
                self.assertEqual(len(requests), 1)
                with client.scope(log=log), self.assertRaises(TelemetryWriteError):
                    client.call(
                        Selection,
                        [{"role": "user", "content": "p2"}],
                        context={"allowed_ids": ["p2"]},
                    )
                self.assertEqual(len(requests), 1)
            finally:
                log.close()

    def test_budget_rejection_has_no_http_event(self) -> None:
        """验证预算拒绝单独记账，不能计为已经发送的 HTTP 请求。"""
        client, requests = self.make_client(
            [self.reply()], context_limit=120, max_output_tokens=100
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            try:
                with client.scope(log=log), self.assertRaises(ContextBudgetError):
                    client.call(Selection, [{"role": "user", "content": "p2"}])
            finally:
                log.close()
            summary = summarize_events(path)
            self.assertEqual(summary["totals"]["requests_started"], 0)
            self.assertEqual(summary["totals"]["budget_rejections"], 1)
            self.assertEqual(summary["totals"]["parse_failed"], 1)
            self.assertEqual(requests, [])

    def test_parallel_scopes_do_not_mix_books(self) -> None:
        """验证共享客户端的不同线程分别绑定书籍与阶段，嵌套作用域不串账。"""
        client, _ = self.make_client([self.reply()])
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"book-{index}.jsonl" for index in range(2)]
            logs = [EventLog(path) for path in paths]

            def work(index: int) -> None:
                """在线程内绑定书籍与嵌套阶段并调用模拟客户端，供隔离性断言检查。"""
                with client.scope(
                    log=logs[index],
                    execution_id=f"e{index}",
                    run_id=f"r{index}",
                    book_id=f"b{index}",
                ):
                    with client.scope(stage="discovery", work_id=f"c{index}"):
                        client.call(
                            Selection,
                            [{"role": "user", "content": "p2"}],
                            context={"allowed_ids": ["p2"]},
                        )

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(work, range(2)))
            finally:
                for log in logs:
                    log.close()
            for index, path in enumerate(paths):
                events = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertTrue(
                    all(event["book_id"] == f"b{index}" for event in events)
                )
                self.assertTrue(all(event["stage"] == "discovery" for event in events))
                self.assertEqual(
                    summarize_events(path)["totals"]["requests_started"], 1
                )

    def test_http_error_metadata_has_code_without_message(self) -> None:
        """验证错误事件仅包含真实状态和符号代码，不保存服务错误正文。"""
        client, requests = self.make_client(
            [
                (
                    401,
                    {
                        "error": {
                            "message": "PRIVATE ERROR",
                            "code": "invalid_api_key",
                            "type": "authentication_error",
                        }
                    },
                )
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            try:
                with client.scope(log=log), self.assertRaises(APIStatusError):
                    client.call(Selection, [{"role": "user", "content": "p2"}])
            finally:
                log.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]
            response = next(
                event for event in events if event["event"] == "request_finished"
            )
            self.assertEqual(response["http_status"], 401)
            self.assertEqual(response["provider_code"], "invalid_api_key")
            self.assertFalse(response["http_success"])
            self.assertNotIn("PRIVATE ERROR", path.read_text())
            self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
