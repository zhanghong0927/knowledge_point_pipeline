"""用确定性离线响应验证提取编排和源文追溯。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Lock
from typing import Any
from unittest.mock import patch

import httpx
from instructor.core.exceptions import IncompleteOutputException
from openai import APIStatusError
from synthesis_fixtures import accepted_work

from book_extractor.llm import ExtractionCancelledError
from book_extractor.markdown import Chunk, Unit
from book_extractor.models import Candidate, NameDiscovery, Regions, SynthesisReview
from book_extractor.pipeline import (
    extract_book,
    make_candidates,
    neighbor_payloads,
    preprocess,
    region_candidates,
    split_payload,
)
from book_extractor.telemetry import TelemetryWriteError


class FakeClient:
    """按预设动作模拟发现结果、上下文需求或异常，不访问推理服务。"""

    model = "fake"
    max_output_tokens = 100
    extra_body: dict[str, Any] = {}
    identity = {"service": "fake"}
    stats = {"calls": 0}
    last_call_attempts = 1

    def __init__(self, actions: list[str]) -> None:
        """保存 actions 响应序列，初始化请求片段和重试预算记录。"""
        self.actions = actions
        self.payloads: list[dict[str, Any]] = []
        self.budgets: list[int] = []

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """接收统计作用域参数 kwargs，返回不产生虚假 HTTP 统计的空上下文。"""
        return nullcontext()

    def call(
        self,
        model: type[NameDiscovery],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any],
        attempts: int = 3,
    ) -> NameDiscovery:
        """记录 messages 和 attempts，

        按动作返回仅引用核心源文的草稿，并用 context 校验。
        """
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        self.budgets.append(attempts)
        action = self.actions.pop(0) if self.actions else "ok"
        if action == "truncate":
            raise IncompleteOutputException()
        if action == "auth":
            raise APIStatusError(
                "fake auth",
                response=httpx.Response(
                    401, request=httpx.Request("POST", "https://fake.invalid")
                ),
                body=None,
            )
        if action == "fail":
            raise RuntimeError("fake")
        data = {
            "findings": [
                {
                    "name": action[5:] if action.startswith("name:") else "原理",
                    "evidence_ids": [payload["core"][0]["id"]],
                }
            ],
            "needs_context": action == "context",
            "context_reason": "邻接说明" if action == "context" else "",
        }
        return model.model_validate(data, context=context)


class PipelineTests(unittest.TestCase):
    """覆盖检查点恢复、来源范围、完成状态及有界重试。"""

    def test_cancellation_is_interrupted_not_partial_candidate(self) -> None:
        """取消不得吞成发现失败或空候选完成，manifest明确中断且释放运行锁。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "book.md"
            source.write_text("# 力学\n\n力是物体间的相互作用。", encoding="utf-8")
            client = FakeClient([])
            with (
                patch.object(
                    client, "call", side_effect=ExtractionCancelledError("cancelled")
                ) as call,
                self.assertRaises(ExtractionCancelledError),
            ):
                extract_book(source, root / "runs", client, workers=2)
            self.assertEqual(call.call_count, 1)
            manifest_path = next((root / "runs").glob("*/manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(manifest["fatal_error"], "ExtractionCancelledError")
            self.assertFalse((manifest_path.parent / ".running").exists())

    def setUp(self) -> None:
        """准备临时工作区，替换复核和综合以隔离发现阶段编排。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.md"
        self.source.write_text("惯性描述物体运动状态。" * 12, encoding="utf-8")
        self.stub = patch(
            "book_extractor.pipeline.synthesize", side_effect=accepted_work
        )
        self.stub.start()
        self.addCleanup(self.stub.stop)

    def run_book(self, client: FakeClient, chunk_tokens: int = 4000) -> dict[str, Any]:
        """用 client 和 chunk_tokens 串行提取临时书籍，返回执行摘要。"""
        return extract_book(
            self.source, self.root / "out", client, workers=1, chunk_tokens=chunk_tokens
        )

    def test_name_candidate_preserves_exact_read_slice(self) -> None:
        """名称直接成为候选，重复原文依赖显式坐标且不会扩成整单元。"""
        names = NameDiscovery(
            findings=[{"name": "惯性", "evidence_ids": ["u1"]}],
            needs_context=False,
            context_reason="",
        )
        unit = Unit("u1", "惯性。惯性。", 1, 1, ["章节"], "paragraph")
        part = {"id": "u1", "text": "惯性。", "start": 3, "end": 6}
        result = make_candidates(names, "c1", {"u1": unit}, [part])[0]
        self.assertEqual(
            result.evidence_spans[0].model_dump(),
            {
                "unit_id": "u1",
                "start": 3,
                "end": 6,
            },
        )
        self.assertEqual(result.name_evidence_ids, ["u1"])
        self.assertEqual(result.origins, ["body"])
        self.assertNotIn("definition", result.model_dump())
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            make_candidates(names, "c1", {"u1": unit}, [{"id": "u1", "text": "惯性。"}])
        children = split_payload([part])
        self.assertEqual(children[0][0]["start"], 3)
        self.assertEqual(children[1][0]["end"], 6)
        self.assertEqual(children[0][0]["end"], children[1][0]["start"])

    def test_index_rule_coverage_routes_without_losing_unparsed_text(self) -> None:
        """已完整规则提名的索引行不重复发现；未解析行和词汇表仍交模型。"""
        text = (
            "# 正文\n\n惯性描述运动状态。\n\n"
            "# Index\n\n惯性 12\n\n未带页码的父项\n\n"
            "# Glossary\n\n转矩：使物体转动的作用。\n"
        )
        self.source.write_text(text, encoding="utf-8")
        client = FakeClient([])
        result = self.run_book(client)
        sent = "".join(
            part["text"] for payload in client.payloads for part in payload["core"]
        )
        self.assertNotIn("惯性 12", sent)
        self.assertIn("未带页码的父项", sent)
        self.assertIn("转矩：使物体转动的作用", sent)
        directory = Path(result["directory"])
        self.assertEqual((directory / "source.md").read_text(encoding="utf-8"), text)
        rules = [
            json.loads(line)
            for line in (directory / "rule_candidates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("惯性", [item["name"] for item in rules])

    def test_split_preserves_slices(self) -> None:
        """二分现有片段时不把已省略的源文字符重新引入请求。"""
        core = [{"id": "a", "text": "AB"}, {"id": "b", "text": "CDEFG"}]
        halves = split_payload(core)
        self.assertEqual("".join(p["text"] for half in halves for p in half), "ABCDEFG")
        self.assertEqual(halves[0][-1], {"id": "b", "text": "C"})
        self.assertEqual(
            neighbor_payloads([[{"id": "a", "text": "中"}], [], []], 1, 1), []
        )

    def test_formula_evidence_expands_only_read_bounded_explanations(self) -> None:
        """公式及其明确解释只在实际读取的同节范围补齐，不跨标题或截取长段。"""
        from book_extractor.markdown import parse_markdown

        for introduction, explanation in [
            ("组成基本杆组的条件是:", "其中n必须是整数。"),
            ("The condition is given by:", "where n must be an integer."),
        ]:
            for boundary in ("ok", "heading", "unrelated", "oversized", "excluded"):
                with self.subTest(language=introduction, boundary=boundary):
                    tail = explanation
                    if boundary == "heading":
                        tail = "## Next\n\n" + tail
                    elif boundary == "unrelated":
                        tail = "这里讨论另一种方法。"
                    elif boundary == "oversized":
                        tail += "很长的解释" * 1000
                    units = parse_markdown(
                        "## Section\n\n" + introduction + "\n\n$$F=3n-2p=0$$\n\n" + tail
                    )
                    source = {unit.id: unit for unit in units}
                    intro = next(
                        unit for unit in units if unit.text.startswith(introduction)
                    )
                    math = next(unit for unit in units if unit.kind == "math")
                    parts = [
                        {
                            "id": unit.id,
                            "text": unit.text,
                            "start": 0,
                            "end": len(unit.text),
                        }
                        for unit in units
                        if not (
                            boundary == "excluded" and unit.text.startswith(explanation)
                        )
                    ]
                    for reference in (intro.id, math.id):
                        draft = NameDiscovery(
                            findings=[{"name": "Concept", "evidence_ids": [reference]}],
                            needs_context=False,
                            context_reason="",
                        )
                        item = make_candidates(draft, "c1", source, parts)[0]
                        self.assertEqual(item.name_evidence_ids, [reference])
                        if boundary == "ok":
                            self.assertIn(math.id, item.evidence_ids)
                            self.assertTrue(
                                any(
                                    source[s.unit_id].text[s.start : s.end]
                                    == explanation
                                    for s in item.evidence_spans
                                )
                            )
                            self.assertTrue(
                                any(
                                    i.startswith("formula_context_added:")
                                    for i in item.issues
                                )
                            )
                        else:
                            self.assertEqual(item.evidence_ids, [reference])

    def test_success_resumes_without_calls(self) -> None:
        """成功发现恢复不重复调用；缓存中被改动的已读跨度必须拒绝。"""
        first = self.run_book(FakeClient([]))
        client = FakeClient([])
        second = self.run_book(client)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["status"], "complete")
        self.assertEqual(client.payloads, [])
        path = next((Path(first["directory"]) / "discovery").glob("*.json"))
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        checkpoint["candidates"][0]["evidence_spans"][0]["end"] -= 1
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differ from read evidence"):
            self.run_book(client)
        self.assertEqual(client.payloads, [])

    def test_frozen_source_hash_checked_before_paid_work(self) -> None:
        """清单哈希吻合时正常执行；原文变化后拒绝创建运行或发送模型请求。"""
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = extract_book(
            self.source,
            self.root / "matched",
            FakeClient([]),
            expected_sha256=expected,
            workers=1,
        )
        self.assertEqual(result["status"], "complete")
        self.source.write_text("已被其他流程更新的正文。", encoding="utf-8")
        client = FakeClient([])
        with self.assertRaisesRegex(ValueError, "SHA256"):
            extract_book(
                self.source,
                self.root / "changed",
                client,
                expected_sha256=expected,
            )
        self.assertEqual(client.payloads, [])
        self.assertFalse((self.root / "changed").exists())

    def test_region_failure_differs_from_model_uncertainty(self) -> None:
        """区域请求失败使整书 partial；模型正常保守判断只记录限制，均保留原文。"""
        self.source.write_text(
            "ISBN 978-7-1234-5678-9\n出版社：示例出版社\n\n惯性描述物体运动状态。",
            encoding="utf-8",
        )
        for fail, expected in ((True, "partial"), (False, "complete")):
            with self.subTest(fail=fail):
                client = FakeClient([])
                original = client.call

                def respond(
                    model: Any,
                    messages: list[dict[str, Any]],
                    *,
                    context: dict[str, Any],
                    **kwargs: Any,
                ) -> Any:
                    """仅替换区域响应；其他阶段复用现有离线客户端。"""
                    if model is not Regions:
                        return original(model, messages, context=context, **kwargs)
                    if fail:
                        raise RuntimeError("offline region failure")
                    return Regions.model_validate(
                        {
                            "decisions": [
                                {
                                    "region_id": context["region_ids"][0],
                                    "action": "uncertain",
                                    "reason": "出版信息与正文混合，保守保留",
                                }
                            ]
                        },
                        context=context,
                    )

                with patch.object(client, "call", side_effect=respond):
                    result = extract_book(
                        self.source,
                        self.root / str(fail),
                        client,
                        workers=1,
                    )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["excluded_units"], 0)
                self.assertEqual(
                    result["preprocess_statistics"]["failed_decisions"], int(fail)
                )
                self.assertEqual(result["errors"][0]["stage"], "preprocess")

    def test_truncated_child_failure_resumes_only_failure(self) -> None:
        """拆分父块只有在两个子块都成功后才完成，恢复仅重试失败子块。"""
        first_client = FakeClient(["truncate", "ok", "fail"])
        first = self.run_book(first_client)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["candidates"], 1)
        original = "".join(p["text"] for p in first_client.payloads[0]["core"])
        children = "".join(
            p["text"] for payload in first_client.payloads[1:] for p in payload["core"]
        )
        self.assertEqual(original, children)
        client = FakeClient([])
        second = self.run_book(client)
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["candidates"], 2)

    def test_context_once_shares_attempt_budget(self) -> None:
        """上下文补充调用只能使用首次发现剩余的重试次数。"""
        client = FakeClient(["context", "ok"])
        manifest = self.run_book(client, chunk_tokens=40)
        self.assertEqual(client.budgets[:2], [3, 2])
        self.assertIn("neighbors", client.payloads[1])
        self.assertEqual(manifest["status"], "complete")

    def test_service_identity_changes_run(self) -> None:
        """部署身份不同必须生成新运行，不能复用旧发现检查点。"""
        first = self.run_book(FakeClient([]))
        client = FakeClient([])
        client.identity = {"service": "other"}
        second = self.run_book(client)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_fatal_error_writes_failed_manifest_and_releases_lock(self) -> None:
        """整书异常必须写失败清单并释放运行锁，不能残留 running 状态。"""
        with patch(
            "book_extractor.pipeline.chunk_units", side_effect=RuntimeError("failure")
        ):
            with self.assertRaises(RuntimeError):
                self.run_book(FakeClient([]))
        manifest_path = next((self.root / "out").glob("*/manifest.json"))
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"], "failed"
        )
        self.assertFalse((manifest_path.parent / ".running").exists())

    def test_limitation_does_not_mark_unfinished(self) -> None:
        """已知能力局限保持可见，但不误报为执行失败。"""

        def limited(*args: Any, **kwargs: Any) -> Any:
            """保持测试中的合法审计覆盖，同时附加非失败限制。"""
            records, audit = accepted_work(*args, **kwargs)
            return records, [*audit, {"severity": "limitation", "error": "known"}]

        with patch("book_extractor.pipeline.synthesize", side_effect=limited):
            self.assertEqual(self.run_book(FakeClient([]))["status"], "complete")

    def test_synthesis_failure_keeps_evidence_candidates_for_resume(self) -> None:
        """综合失败仍已落盘证据候选，恢复时不重复名称发现或调用局部释义。"""
        client = FakeClient([])
        with patch(
            "book_extractor.pipeline.synthesize", side_effect=RuntimeError("failed")
        ):
            with self.assertRaises(RuntimeError):
                self.run_book(client)
        directory = next((self.root / "out").iterdir())
        row = json.loads((directory / "candidates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["name"], "原理")
        self.assertNotIn("definition", row)
        self.assertTrue(row["evidence_spans"])
        self.assertNotIn("definition_missing", row["issues"])
        resumed = FakeClient([])
        final = self.run_book(resumed)
        self.assertEqual(final["status"], "complete")
        self.assertEqual(resumed.payloads, [])
        self.assertEqual(final["output_statistics"]["body_candidates"], 1)
        self.assertNotIn("pending_definition_names", final["output_statistics"])

    def test_rule_candidates_are_collected_before_region_exclusion(self) -> None:
        """规则读取完整来源，后续排除不删除规则提名，也不强并正文同名候选。"""
        rule = Candidate(
            name="目录名",
            candidate_id="rule-1",
            chunk_id="rule:u1",
            scope=[],
            evidence_ids=[],
            name_evidence_ids=["u000001"],
            origins=["toc"],
        )
        order = []

        def collect(units: list[Unit]) -> list[Candidate]:
            """记录规则调用并确认原文单元仍在。"""
            self.assertTrue(units)
            order.append("rule")
            return [rule]

        from book_extractor.pipeline import preprocess as original_preprocess

        def prepare(*args: Any, **kwargs: Any) -> Any:
            """记录预处理调用，委托真实逻辑保留统计和产物。"""
            order.append("preprocess")
            return original_preprocess(*args, **kwargs)

        from book_extractor.synthesis import synthesize
        from scripts.validate_runs import validate

        client = FakeClient([])
        discover = client.call

        def answer(model: type, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            """仅替换模型边界，实际综合负责覆盖、来源约束及记录身份。"""
            if model is not SynthesisReview:
                return discover(model, messages, **kwargs)
            payload = json.loads(messages[-1]["content"])
            return model.model_validate(
                {
                    "name_decisions": [
                        {
                            "candidate_id": c["candidate_id"],
                            "decision": "accept",
                            "reason": "源文命名",
                            "evidence_ids": c["evidence_ids"] or c["name_evidence_ids"],
                        }
                        for c in payload["candidates"]
                    ],
                    "findings": [
                        {
                            "support_reason": "测试来源支持范围",
                            "definition_supported": bool(
                                "源文解释" if item["evidence_ids"] else None
                            ),
                            "confidence": "high",
                            "draft_ids": [item["candidate_id"]],
                            "name": item["name"],
                            "definition": "源文解释" if item["evidence_ids"] else None,
                            "evidence_ids": item["evidence_ids"]
                            or item["name_evidence_ids"],
                            "aliases": [],
                            "conditions": [],
                            "category": "概念",
                            "issues": [],
                        }
                        for item in payload["candidates"]
                    ],
                },
                context=kwargs["context"],
            )

        with (
            patch(
                "book_extractor.pipeline.extract_rule_candidates", side_effect=collect
            ),
            patch("book_extractor.pipeline.preprocess", side_effect=prepare),
            patch("book_extractor.pipeline.synthesize", side_effect=synthesize),
            patch.object(client, "call", side_effect=answer),
        ):
            result = self.run_book(client)
        self.assertEqual(order, ["rule", "preprocess"])
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["output_statistics"]["rule_candidates"], 1)
        self.assertEqual(result["output_statistics"]["name_only_candidates"], 1)
        audit = validate(Path(result["directory"]))
        self.assertTrue(audit["references_valid"])
        self.assertEqual((audit["candidates"], audit["records"]), (2, 2))

    def test_excluded_region_cannot_return_as_rule_definition_evidence(self) -> None:
        """预处理排除后保留规则名称依据，但从交付候选移除被排除的释义切片。"""
        rule = Candidate(
            name="惯性",
            candidate_id="rule-filter",
            chunk_id="rule:u000001",
            scope=[],
            evidence_ids=["u000001"],
            name_evidence_ids=["u000001"],
            evidence_spans=[{"unit_id": "u000001", "start": 0, "end": 2}],
            origins=["index"],
        )

        def exclude(*args: Any, **kwargs: Any) -> Any:
            """保留真实预处理审计文件，模拟其明确排除当前唯一单元。"""
            _, decisions = preprocess(*args, **kwargs)
            return [], decisions

        with (
            patch(
                "book_extractor.pipeline.extract_rule_candidates", return_value=[rule]
            ),
            patch("book_extractor.pipeline.preprocess", side_effect=exclude),
        ):
            result = self.run_book(FakeClient([]))
        directory = Path(result["directory"])
        saved = json.loads((directory / "rule_candidates.jsonl").read_text("utf-8"))
        self.assertEqual(saved["name_evidence_ids"], ["u000001"])
        self.assertEqual(saved["evidence_ids"], [])
        self.assertEqual(saved["evidence_spans"], [])
        self.assertIn("rule_evidence_excluded_by_preprocess", saved["issues"])
        self.assertEqual(
            result["output_statistics"]["rule_candidates_with_excluded_evidence"], 1
        )

    def test_code_and_blank_regions_are_protected(self) -> None:
        """代码单元和纯空白不能被区域预处理列为排除候选。"""
        units = [
            Unit(
                "a",
                "ISBN 978-7-1234-5678-9\n出版社：示例出版社\n版次：第一版\n",
                1,
                3,
                [],
                "code",
            ),
            Unit("b", "\n", 2, 2, [], "whitespace"),
        ]
        self.assertEqual(region_candidates(units), [])
        huge = Unit(
            "c",
            "ISBN 978-7-1234-5678-9\n出版社：示例出版社\n版次：第一版\n"
            + "文字 " * 6000,
            1,
            4,
            [],
            "paragraph",
        )
        client = FakeClient([])
        retained, rows = preprocess([huge], client, self.root)
        self.assertEqual(retained, [huge])
        self.assertEqual(rows[0]["reason"], "region_context_oversized")
        self.assertEqual(client.payloads, [])

    def test_fatal_auth_stops_new_chunk_submission(self) -> None:
        """认证失败阻止补入新块；仅已在途块可结束，单线程仍只请求一次。"""
        for workers in (1, 2):
            with self.subTest(workers=workers):
                client = FakeClient(["auth"])
                with self.assertRaises(APIStatusError):
                    extract_book(
                        self.source,
                        self.root / f"out-{workers}",
                        client,
                        workers=workers,
                        chunk_tokens=40,
                    )
                self.assertGreaterEqual(len(client.payloads), 1)
                self.assertLessEqual(len(client.payloads), workers)

    def test_chunk_completion_refills_before_slow_first_chunk(self) -> None:
        """首块等待时第二块完成即补第三块，保持并发上界和候选原顺序。"""
        units = [
            Unit(f"u{index}", name, index, index, [], "paragraph")
            for index, name in enumerate(["甲", "乙", "丙"], 1)
        ]
        chunks = [
            Chunk(f"c{index}", [unit.id], unit.text, [])
            for index, unit in enumerate(units)
        ]
        third_started = Event()
        lock = Lock()
        active = 0
        maximum = 0
        first_saw_third = False
        client = FakeClient([])

        def call(
            model: type[NameDiscovery],
            messages: list[dict[str, Any]],
            *,
            context: dict[str, Any],
            attempts: int = 3,
        ) -> NameDiscovery:
            """首块等待第三块进入模型边界，不用睡眠猜测线程执行顺序。"""
            nonlocal active, maximum, first_saw_third
            part = json.loads(messages[-1]["content"])["core"][0]
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if part["text"] == "甲":
                    first_saw_third = third_started.wait(5)
                    if not first_saw_third:
                        raise AssertionError("第三块未在首块完成前启动")
                if part["text"] == "丙":
                    third_started.set()
                return model.model_validate(
                    {
                        "findings": [
                            {"name": part["text"], "evidence_ids": [part["id"]]}
                        ],
                        "needs_context": False,
                        "context_reason": "",
                    },
                    context=context,
                )
            finally:
                with lock:
                    active -= 1

        with (
            patch("book_extractor.pipeline.parse_markdown", return_value=units),
            patch("book_extractor.pipeline.chunk_units", return_value=chunks),
            patch.object(client, "call", side_effect=call),
        ):
            result = extract_book(self.source, self.root / "out", client, workers=2)
        rows = [
            json.loads(line)
            for line in (Path(result["directory"]) / "candidates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(first_saw_third)
        self.assertEqual(maximum, 2)
        self.assertEqual([row["name"] for row in rows], ["甲", "乙", "丙"])
        self.assertEqual(result["status"], "complete")

    def test_preprocessing_auth_propagates(self) -> None:
        """区域预处理同样传播部署认证失败，不吞掉异常。"""
        unit = Unit(
            "a",
            "ISBN 978-7-1234-5678-9\n出版社：示例出版社\n版次：第一版\n",
            1,
            3,
            [],
            "paragraph",
        )
        with self.assertRaises(APIStatusError):
            preprocess([unit], FakeClient(["auth"]), self.root)

    def test_split_context_uses_sibling_and_marks_resolution(self) -> None:
        """只有一个父块时，子块仍能借用兄弟块作为邻文并记录补全状态。"""
        client = FakeClient(["truncate", "context", "ok", "ok"])
        manifest = self.run_book(client)
        self.assertEqual(manifest["status"], "complete")
        self.assertIn("neighbors", client.payloads[2])
        rows = [
            json.loads(line)
            for line in (Path(manifest["directory"]) / "candidates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertFalse(any("context_incomplete" in row["issues"] for row in rows))

    def test_scope_uses_referenced_unit(self) -> None:
        """候选章节范围来自所引用单元，不误用其他章节标题。"""
        self.source.write_text(
            "# 第一节\n惯性说明。\n\n# 第二节\n热力学说明。", encoding="utf-8"
        )
        manifest = self.run_book(FakeClient([]))
        row = json.loads(
            (Path(manifest["directory"]) / "candidates.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertEqual(row["scope"], ["第一节"])

    def test_discovery_policy_rejection_remains_fatal(self) -> None:
        """名称发现遭服务拒绝必须中止，不能静默接受空候选。"""
        error = APIStatusError(
            "policy",
            response=httpx.Response(
                400, request=httpx.Request("POST", "https://fake.invalid")
            ),
            body=None,
        )
        client = FakeClient([])
        with patch.object(client, "call", side_effect=error):
            with self.assertRaises(APIStatusError):
                self.run_book(client)
        manifest = next((self.root / "out").glob("*/manifest.json"))
        self.assertEqual(
            json.loads(manifest.read_text(encoding="utf-8"))["status"], "failed"
        )

    def test_telemetry_failure_escapes_discovery_fallback(self) -> None:
        """统计写入失败必须传播，不能回退成未验证的成功结果。"""
        client = FakeClient([])
        with patch.object(client, "call", side_effect=TelemetryWriteError("disk full")):
            with self.assertRaises(TelemetryWriteError):
                self.run_book(client)
        self.assertEqual(list((self.root / "out").glob("*/.running")), [])

    def test_log_open_failure_releases_book_lock(self) -> None:
        """执行日志初始化失败也必须释放书籍运行锁。"""
        with patch(
            "book_extractor.pipeline.EventLog",
            side_effect=TelemetryWriteError("open failed"),
        ):
            with self.assertRaises(TelemetryWriteError):
                self.run_book(FakeClient([]))
        self.assertEqual(list((self.root / "out").glob("*/.running")), [])

    def test_english_and_metadata_regions_require_model_decisions(self) -> None:
        """英文出版页和首尾对象只提名；排除、保留及失败均由实际判断决定。"""
        for text in (
            "Copyright 2026. All rights reserved. Published by Example Press.",
            '{"filename":"book.md","checksum":"abc"}',
        ):
            unit = Unit("u1", text, 1, 1, [], "paragraph")
            self.assertEqual(region_candidates([unit]), [unit])
            self.assertEqual(
                region_candidates([Unit("u1", text, 1, 1, [], "code")]), []
            )
            for action in ("retain", "exclude", "uncertain", "failure"):
                with (
                    self.subTest(text=text, action=action),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    client = FakeClient([])
                    result = Regions.model_validate(
                        {
                            "decisions": [
                                {
                                    "region_id": "u1",
                                    "action": action
                                    if action != "failure"
                                    else "uncertain",
                                    "reason": "按上下文判断",
                                }
                            ]
                        },
                        context={"region_ids": ["u1"]},
                    )
                    with patch.object(
                        client,
                        "call",
                        return_value=result,
                        side_effect=RuntimeError("offline")
                        if action == "failure"
                        else None,
                    ) as call:
                        kept, decisions = preprocess([unit], client, Path(folder))
                    self.assertEqual(call.call_count, 1)
                    self.assertEqual(kept, [] if action == "exclude" else [unit])
                    self.assertNotEqual(decisions[0]["decision_origin"], "rule")


if __name__ == "__main__":
    unittest.main()
