"""验证最终导出前按模型置信度选择性执行的来源复核。"""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
from openai import BadRequestError

from book_extractor.markdown import Unit
from book_extractor.models import Finding, Record
from book_extractor.synthesis import _verify_records
from book_extractor.telemetry import TelemetryWriteError


class VerificationClient:
    """返回可控 Finding，同时记录统计作用域、事件和请求内容。"""

    model = "offline-verification"

    def __init__(self, response: Finding | Exception) -> None:
        """保存给定 response 或异常，初始化请求、事件及作用域记录。"""
        self.response = response
        self.payloads: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}

    @contextmanager
    def scope(self, **metadata: Any) -> Iterator[None]:
        """临时叠加 metadata 并让出执行，退出时恢复上一层作用域。"""
        previous = self.metadata
        self.metadata = {**previous, **metadata}
        try:
            yield
        finally:
            self.metadata = previous

    def event(self, event: str, **metadata: Any) -> None:
        """把 event 名称、当前作用域和 metadata 写入内存事件列表，不返回值。"""
        self.events.append({**self.metadata, "event": event, **metadata})

    def call(
        self,
        model: type[Finding],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any],
    ) -> Finding:
        """记录 messages 中的实际 JSON，

        返回预设 Finding 或抛出预设异常；其余参数兼容客户端。
        """
        self.payloads.append(json.loads(messages[-1]["content"]))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class VerificationTests(unittest.TestCase):
    """检查无风险线索时零调用、语气纠正、来源保留及安全失败。"""

    def setUp(self) -> None:
        """建立一个已生成释义的最终记录和明确否定其因果的源文。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.member = Record(
            support_reason="测试来源支持范围",
            definition_supported=True,
            confidence="medium",
            name="农民起义",
            definition="农民起义由领主逆转农业发展引起。",
            aliases=[],
            conditions=[],
            category="事件",
            issues=[],
            evidence_ids=["u1"],
            record_id="record-1",
            book_id="book",
            run_id="run",
            candidate_ids=["candidate-1"],
            scope=["农业史"],
        )
        self.unit = Unit(
            "u1",
            "没有证据证明农民起义由领主逆转农业发展引起。起义发生于1381年。",
            1,
            1,
            ["农业史"],
            "paragraph",
        )
        self.corrected = Finding(
            support_reason="测试来源支持范围",
            definition_supported=bool(self.unit.text),
            confidence="medium",
            name="农民起义",
            definition=self.unit.text,
            aliases=[],
            conditions=[],
            category="事件",
            issues=[],
            evidence_ids=["u1"],
        )

    def test_confidence_routes_once_independent_of_typography(self) -> None:
        """高置信度零复核，中低及旧记录未知值各复核一次，不依赖公式或否定词。"""
        for level, expected in (("high", 0), ("medium", 1), ("low", 1), (None, 1)):
            with self.subTest(level=level):
                self.member.confidence = level
                self.unit.text = "普通说明。"
                self.corrected.confidence = "low"
                client = VerificationClient(self.corrected)
                records, errors = _verify_records(
                    [self.member],
                    {self.unit.id: self.unit},
                    client,
                    self.directory / str(level),
                )
                self.assertEqual(len(client.payloads), expected)
                self.assertFalse(errors)
                if expected:
                    self.assertEqual(records[0].confidence, "low")
        self.member.definition = None
        client = VerificationClient(RuntimeError("不得凭空补定义"))
        _verify_records(
            [self.member], {self.unit.id: self.unit}, client, self.directory
        )
        self.assertFalse(client.payloads)

    def test_wrong_name_is_not_cached_and_can_recover(self) -> None:
        """错名响应不能进入成功缓存，下一次正确响应可完成并复用。"""
        client = VerificationClient(self.corrected.model_copy(update={"name": "错名"}))
        _, errors = _verify_records(
            [self.member], {self.unit.id: self.unit}, client, self.directory
        )
        self.assertTrue(errors)
        self.assertFalse(list(self.directory.iterdir()))
        client.response = self.corrected
        for _ in range(2):
            records, errors = _verify_records(
                [self.member], {self.unit.id: self.unit}, client, self.directory
            )
            self.assertFalse(errors)
            self.assertEqual(records[0].name, self.member.name)
        self.assertEqual(len(client.payloads), 2)

    def test_rule_limits_survive_successful_verification(self) -> None:
        """程序生成的证据截断限制不依赖模型复述，最终记录仍可见。"""
        issues = [
            "rule_hits_truncated:8",
            "rule_context_truncated:400_chars",
            "rule_heading_context_truncated:512_tokens",
            "rule_large_table_row_only_or_unsupported",
            "rule_evidence_excluded_by_preprocess",
        ]
        self.member.issues = issues
        records, errors = _verify_records(
            [self.member],
            {self.unit.id: self.unit},
            VerificationClient(self.corrected),
            self.directory,
        )
        self.assertFalse(errors)
        self.assertEqual(records[0].issues, issues)

    def test_high_confidence_zero_calls(self) -> None:
        """高置信度时，记录不变且不产生验证请求或事件。"""
        client = VerificationClient(RuntimeError("must not call"))
        self.member.confidence = "high"
        self.member.issues = ["仅标题，原文未定义"]
        unit = Unit("u1", "起义发生于1381年。", 1, 1, ["农业史"], "paragraph")
        records, errors = _verify_records(
            [self.member], {unit.id: unit}, client, self.directory
        )
        self.assertEqual(records[0].definition, self.member.definition)
        self.assertEqual(records[0].issues, self.member.issues)
        self.assertEqual((client.payloads, client.events, errors), ([], [], []))

    def test_medium_confidence_routes_source_only_verification(self) -> None:
        """中置信度触发原生重核，不把生成释义作为证据，也不额外增加重试层。"""
        self.member.issues = ["source_formula_uncertain: 符号疑义"]
        self.unit.text = "原文将ml称为质径矩。"
        self.member.name = "质径矩"
        self.corrected.name = "质径矩"
        self.corrected.definition = self.unit.text
        client = VerificationClient(self.corrected)
        records, errors = _verify_records(
            [self.member], {self.unit.id: self.unit}, client, self.directory
        )
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(set(client.payloads[0]), {"name", "scope", "sources"})
        self.assertEqual(records[0].definition, self.unit.text)
        self.assertFalse(errors)
        selected = next(
            e for e in client.events if e["event"] == "verification_selected"
        )
        self.assertEqual(selected["selection_reasons"], ["confidence_medium"])
        self.assertEqual(selected["confidence"], "medium")

    def test_correction_cache_and_provenance(self) -> None:
        """只提交原文和名称定位，纠正语气时保留身份与来源，并复用校验后的缓存。"""
        retained = [
            "context_incomplete",
            "context_extension_failed",
            "source_unit_split",
            "source_formula_uncertain: 符号疑义",
            "grouping_uncertain",
            "cross_batch_grouping_deferred",
            "synthesis_context_oversized",
            "grouping_failed:RuntimeError",
            "synthesis_failed:ValueError",
            "verification_failed:RuntimeError",
        ]
        self.member.issues = [*retained, "仅标题，原文未定义"]
        self.corrected.issues = ["原文保留不同观点"]
        client = VerificationClient(self.corrected)
        records, errors = _verify_records(
            [self.member], {self.unit.id: self.unit}, client, self.directory
        )
        again, _ = _verify_records(
            [self.member], {self.unit.id: self.unit}, client, self.directory
        )
        self.assertEqual(records, again)
        self.assertEqual(records[0].definition, self.corrected.definition)
        self.assertEqual(records[0].issues, ["原文保留不同观点", *retained])
        self.assertEqual(records[0].candidate_ids, [self.member.candidate_ids[0]])
        self.assertEqual(
            (records[0].book_id, records[0].run_id, records[0].scope),
            ("book", "run", ["农业史"]),
        )
        self.assertEqual(records[0].evidence_ids, ["u1"])
        self.assertFalse(errors)
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(set(client.payloads[0]), {"name", "scope", "sources"})
        self.assertEqual(client.payloads[0]["sources"][0]["text"], self.unit.text)
        self.assertTrue(
            any(
                event["event"] == "cache_hit" and event["stage"] == "verification"
                for event in client.events
            )
        )
        self.assertTrue(
            all(
                "text" not in event and "definition" not in event
                for event in client.events
            )
        )

    def test_unrelated_cue_does_not_force_negation(self) -> None:
        """同一单元其他命题的否定线索只能触发检查，不能强制否定当前概念。"""
        self.unit.text = "起义发生于1381年。没有证据证明市政厅的建造年份。"
        answer = self.corrected.model_copy(update={"definition": "起义发生于1381年。"})
        records, errors = _verify_records(
            [self.member],
            {self.unit.id: self.unit},
            VerificationClient(answer),
            self.directory,
        )
        self.assertEqual(records[0].definition, answer.definition)
        self.assertFalse(errors)

    def test_new_missing_definition_keeps_current_issues(self) -> None:
        """成功重核返回空释义时，保留新问题并由校验补充缺定义标记。"""
        self.member.issues = ["旧草稿仅有标题"]
        answer = self.corrected.model_copy(
            update={
                "definition": None,
                "definition_supported": False,
                "issues": ["当前证据不足"],
            }
        )
        records, errors = _verify_records(
            [self.member],
            {self.unit.id: self.unit},
            VerificationClient(answer),
            self.directory,
        )
        self.assertFalse(errors)
        self.assertIsNone(records[0].definition)
        self.assertEqual(records[0].issues, ["当前证据不足", "definition_missing"])

    def test_failure_preserves_draft_and_fatal_errors_escape(self) -> None:
        """普通复核失败保留候选并报告未完成，服务拒绝和统计故障必须传播。"""
        self.member.confidence = "medium"
        self.member.issues = ["仅标题，原文未定义"]
        invalid = self.corrected.model_copy(update={"evidence_ids": ["foreign"]})
        for index, outcome in enumerate([RuntimeError("offline"), invalid]):
            with self.subTest(outcome=type(outcome).__name__):
                records, errors = _verify_records(
                    [self.member],
                    {self.unit.id: self.unit},
                    VerificationClient(outcome),
                    self.directory / str(index),
                )
                self.assertEqual(records[0].definition, self.member.definition)
                self.assertIn("仅标题，原文未定义", records[0].issues)
                self.assertEqual(
                    records[0].candidate_ids, [self.member.candidate_ids[0]]
                )
                self.assertTrue(
                    any(
                        issue.startswith("verification_failed:")
                        for issue in records[0].issues
                    )
                )
                self.assertEqual(
                    (errors[0]["stage"], errors[0]["severity"]),
                    ("verification", "error"),
                )
        refusal = BadRequestError(
            "rejected",
            response=httpx.Response(
                400, request=httpx.Request("POST", "https://test.invalid")
            ),
            body=None,
        )
        for error in [refusal, TelemetryWriteError("unavailable")]:
            with (
                self.subTest(error=type(error).__name__),
                self.assertRaises(type(error)),
            ):
                _verify_records(
                    [self.member],
                    {self.unit.id: self.unit},
                    VerificationClient(error),
                    self.directory / "fatal",
                )


if __name__ == "__main__":
    unittest.main()
