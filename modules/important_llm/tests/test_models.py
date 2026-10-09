"""验证源文引用、语言约束及公式解码损坏的响应校验。"""

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from book_extractor.llm import LLMClient
from book_extractor.models import (
    Candidate,
    EvidenceSpan,
    Finding,
    Grouping,
    NameAnchor,
    NameDecision,
    NameDiscovery,
    SynthesisReview,
)
from book_extractor.synthesis import _cache_key
from book_extractor.telemetry import EventLog


class ModelTests(unittest.TestCase):
    """拒绝序列化损坏和无依据引用，同时允许释义改写。"""

    def test_name_reason_precedes_decision_without_changing_validation(self) -> None:
        """理由先于结论进入Schema和序列化；旧键顺序仍可校验，综合缓存身份改变。"""
        decision = NameDecision.model_validate(
            {
                "candidate_id": "c1",
                "decision": "reject",
                "reason": "栏目包装",
                "evidence_ids": ["u1"],
            }
        )
        fields = list(decision.model_dump())
        self.assertLess(fields.index("reason"), fields.index("decision"))
        schema = SynthesisReview.model_json_schema()
        nested = schema["$defs"]["NameDecision"]
        for fields in (list(nested["properties"]), nested["required"]):
            self.assertLess(fields.index("reason"), fields.index("decision"))
        with self.assertRaises(ValidationError):
            NameDecision.model_validate({**decision.model_dump(), "reason": " "})
        old_schema = json.loads(json.dumps(schema))
        old = old_schema["$defs"]["NameDecision"]
        order = ["evidence_ids", "candidate_id", "decision", "reason"]
        old["properties"] = {key: old["properties"][key] for key in order}
        old["required"] = order
        client = SimpleNamespace(model="offline")
        current_key = _cache_key(client, SynthesisReview, "prompt", {}, {})
        with patch.object(
            SynthesisReview, "model_json_schema", return_value=old_schema
        ):
            old_key = _cache_key(client, SynthesisReview, "prompt", {}, {})
        self.assertNotEqual(current_key, old_key)

    def test_alias_accepts_explicit_inline_subscript_spelling(self) -> None:
        """真实T2别名只改变数学排版；其他下标或未引用来源仍不能供证。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "spin–spin relaxation",
            "definition": "A relaxation process.",
            "aliases": ["T2 relaxation"],
            "category": "process",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u1"],
        }
        context = {
            "evidence_ids": ["u1", "u2"],
            "evidence_texts": {
                "u1": "This process is known as spin–spin, or $ T_{2} $ , relaxation.",
                "u2": "Another process is described here.",
            },
        }
        result = Finding.model_validate(value, context=context)
        self.assertEqual(result.aliases, ["T2 relaxation"])
        for spelling in ("$T_2$ relaxation", "$T_{2}$ relaxation"):
            with self.subTest(spelling=spelling):
                updated = {**context, "evidence_texts": {"u1": spelling}}
                self.assertEqual(
                    Finding.model_validate(value, context=updated).aliases,
                    ["T2 relaxation"],
                )
        for confidence in (None, "certain", 0.9):
            with (
                self.subTest(confidence=confidence),
                self.assertRaises(ValidationError),
            ):
                Finding.model_validate({**value, "confidence": confidence})
        with self.assertRaises(ValidationError):
            Finding.model_validate(
                {k: v for k, v in value.items() if k != "confidence"}
            )
        for change in [
            {"aliases": ["T3 relaxation"]},
            {"aliases": ["transverse relaxation"]},
            {"evidence_ids": ["u2"]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Finding.model_validate({**value, **change}, context=context)

    def test_prose_error_types_are_safely_countable_in_existing_telemetry(self) -> None:
        """三类正文错误经既有安全提取和账本可分型，不记录输入或反馈正文。"""
        context = {
            "evidence_ids": ["u000001"],
            "evidence_texts": {
                "u000001": "The force is the interaction of bodies and is a vector."
            },
        }
        finding = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "Force",
            "definition": "Force is a vector.",
            "aliases": [],
            "category": "concept",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u000001"],
        }
        cases = [
            ("source_language_changed", "PRIVATE 原文之外的中文解释"),
            ("internal_id_in_prose", "PRIVATE described in u000001"),
            ("invalid_control_character", "PRIVATE broken \x08eta"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            try:
                for expected, definition in cases:
                    with self.assertRaises(ValidationError) as failure:
                        Finding.model_validate(
                            {**finding, "definition": definition}, context=context
                        )
                    fields = LLMClient._error_fields(failure.exception)
                    self.assertEqual(fields["validation_errors"][0]["type"], expected)
                    log.event("parse_failed", **fields)
            finally:
                log.close()
            text = path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in text.splitlines()]
            counts = Counter(
                item["type"] for event in events for item in event["validation_errors"]
            )
            self.assertEqual(counts, {code: 1 for code, _ in cases})
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("u000001", text)
            self.assertTrue(
                all(
                    set(item) == {"type", "loc"}
                    for event in events
                    for item in event["validation_errors"]
                )
            )

    def test_member_evidence_feedback_bounds_trusted_context(self) -> None:
        """引用错挂反馈只含有界可信ID和未知数量，不复述错误引用或理由。"""
        allowed = [f"u{i:06d}" for i in range(1, 31)]
        value = {
            "name_decisions": [
                {
                    "candidate_id": "d0",
                    "decision": "reject",
                    "reason": "PRIVATE_REASON",
                    "evidence_ids": ["u999999"],
                }
            ],
            "findings": [],
        }
        with self.assertRaises(ValidationError) as failure:
            SynthesisReview.model_validate(
                value,
                context={
                    "draft_ids": ["d0"],
                    "evidence_ids": [*allowed, "u999999"],
                    "candidate_evidence_ids": {"d0": allowed},
                },
            )
        detail = failure.exception.errors()[0]
        self.assertEqual(detail["loc"], ("name_decisions", 0, "evidence_ids"))
        self.assertEqual(detail["ctx"]["candidate_ids"], ["d0"])
        self.assertEqual(detail["ctx"]["allowed_evidence_ids"], allowed[:20])
        self.assertEqual(detail["ctx"]["allowed_evidence_count"], 30)
        self.assertEqual(detail["ctx"]["unknown_count"], 1)
        self.assertIsNone(detail["input"])
        self.assertNotIn("u999999", str(failure.exception))
        self.assertNotIn("PRIVATE_REASON", str(failure.exception))

    def test_text_guards_use_final_references_but_internal_ids_use_all_sources(
        self,
    ) -> None:
        """未引用单元不能支持别名或绕过语言检查，内部ID仍按全部已知源检测。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "fast Fourier transform",
            "aliases": ["FFT"],
            "definition": "A transform of the signal.",
            "category": "concept",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u000002"],
        }
        context = {
            "evidence_ids": ["u000001", "u000002"],
            "evidence_texts": {
                "u000001": "FFT 是此处的简称。",
                "u000002": "The transform is a representation of the signal.",
            },
        }
        with self.assertRaises(ValidationError) as failure:
            Finding.model_validate(value, context=context)
        self.assertEqual(
            failure.exception.errors()[0]["type"], "source_alias_not_found"
        )
        accepted = Finding.model_validate(
            {**value, "evidence_ids": ["u000001", "u000002"]}, context=context
        )
        self.assertEqual(accepted.aliases, ["FFT"])
        for changes in [{"name": "傅里叶变换"}, {"conditions": ["仅用于信号"]}]:
            with (
                self.subTest(changes=changes),
                self.assertRaises(ValidationError) as failure,
            ):
                Finding.model_validate(
                    {**value, "aliases": [], **changes}, context=context
                )
            self.assertEqual(
                failure.exception.errors()[0]["type"], "source_language_changed"
            )
        with self.assertRaises(ValidationError) as failure:
            NameAnchor.model_validate(
                {"name": "傅里叶变换", "evidence_ids": ["u000002"]}, context=context
            )
        self.assertEqual(
            failure.exception.errors()[0]["type"], "source_language_changed"
        )
        chinese = {
            **context,
            "evidence_texts": {
                "u000001": "傅里叶变换（FFT）",
                "u000002": "变换名称在此列出。",
            },
        }
        with self.assertRaises(ValidationError) as failure:
            NameAnchor.model_validate(
                {"name": "傅里叶变换（FFT）", "evidence_ids": ["u000002"]},
                context=chinese,
            )
        self.assertEqual(
            failure.exception.errors()[0]["type"],
            "source_parenthesized_translation_not_found",
        )
        with self.assertRaises(ValidationError) as failure:
            Finding.model_validate(
                {**value, "aliases": [], "definition": "See u000001."}, context=context
            )
        self.assertEqual(failure.exception.errors()[0]["type"], "internal_id_in_prose")
        self.assertEqual(context["evidence_texts"]["u000001"], "FFT 是此处的简称。")

    def test_name_decisions_can_share_confirmed_group_sources_only(self) -> None:
        """名称身份可用确认同组证据；跨组和不确定单例仍拒绝，释义来源不随之扩大。"""
        context = {
            "draft_ids": ["a", "b", "c"],
            "evidence_ids": ["u1", "u2", "u3"],
            "candidate_groups": [["a", "b"], ["c"]],
            "candidate_evidence_ids": {"a": ["u1"], "b": ["u2"], "c": ["u3"]},
            "candidate_body_ids": {"a": ["u1"], "b": ["u2"], "c": ["u3"]},
        }
        value = {
            "name_decisions": [
                {
                    "candidate_id": key,
                    "decision": "reject",
                    "reason": "源内身份依据",
                    "evidence_ids": ["u2" if key in {"a", "b"} else "u3"],
                }
                for key in ["a", "b", "c"]
            ],
            "findings": [],
        }
        accepted = SynthesisReview.model_validate(value, context=context)
        self.assertEqual(accepted.name_decisions[0].evidence_ids, ["u2"])
        for groups in [[["a"], ["b"], ["c"]], [["a", "c"], ["b"]]]:
            with (
                self.subTest(groups=groups),
                self.assertRaises(ValidationError) as failure,
            ):
                SynthesisReview.model_validate(
                    value, context={**context, "candidate_groups": groups}
                )
            self.assertEqual(
                failure.exception.errors()[0]["loc"],
                ("name_decisions", 0, "evidence_ids"),
            )
        changed = json.loads(json.dumps(value))
        changed["name_decisions"][0]["decision"] = "accept"
        changed["findings"] = [
            {
                "support_reason": "测试来源支持范围",
                "definition_supported": True,
                "confidence": "high",
                "draft_ids": ["a"],
                "name": "甲",
                "aliases": [],
                "category": "概念",
                "definition": "不能借乙的正文",
                "conditions": [],
                "issues": [],
                "evidence_ids": ["u2"],
            }
        ]
        with self.assertRaises(ValidationError) as failure:
            SynthesisReview.model_validate(changed, context=context)
        self.assertEqual(
            failure.exception.errors()[0]["loc"], ("findings", 0, "evidence_ids")
        )

    def test_real_parallel_title_rejections_use_confirmed_group_evidence(self) -> None:
        """真实六项拒绝原响应可引用同组第四成员证据；不改变名称判定内容。"""
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "parallel_name_group.json").read_text(
                encoding="utf-8"
            )
        )
        result = SynthesisReview.model_validate(
            fixture["response"], context=fixture["context"]
        )
        self.assertEqual(len(result.name_decisions), 6)
        self.assertTrue(
            all(
                row.decision == "reject" and row.evidence_ids == ["u001109"]
                for row in result.name_decisions
            )
        )
        self.assertEqual(result.findings, [])
        singleton = {
            **fixture["context"],
            "candidate_groups": [[key] for key in fixture["context"]["draft_ids"]],
        }
        with self.assertRaises(ValidationError):
            SynthesisReview.model_validate(fixture["response"], context=singleton)

    def test_candidate_has_no_definition_and_validates_source_spans(self) -> None:
        """中间类型拒绝生成释义；正文、名称引用与半开区间共享来源验证。"""
        value = {
            "candidate_id": "c",
            "chunk_id": "c1",
            "scope": [],
            "name": "力",
            "evidence_ids": ["u1"],
            "name_evidence_ids": ["u2"],
            "evidence_spans": [{"unit_id": "u1", "start": 1, "end": 3}],
        }
        context = {
            "evidence_ids": {"u1", "u2"},
            "evidence_texts": {"u1": "甲力乙丁", "u2": "力"},
        }
        Candidate.model_validate(value, context=context)
        for change in [
            {"definition": "伪造解释"},
            {"category": "概念"},
            {"name_evidence_ids": ["foreign"]},
            {"evidence_spans": [{"unit_id": "u1", "start": 1, "end": 99}]},
            {"evidence_spans": [{"unit_id": "u2", "start": 0, "end": 1}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Candidate.model_validate({**value, **change}, context=context)
        for start, end in [(2, 2), (-1, 1), (3, 2), (False, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                EvidenceSpan(unit_id="u1", start=start, end=end)

    def test_source_and_partition_errors_have_stable_types(self) -> None:
        """来源及名单错误保留拒绝规则，并提供不包含原文的稳定类型。"""
        finding = {
            "support_reason": "测试来源支持范围",
            "definition_supported": False,
            "confidence": "high",
            "name": "树穴",
            "aliases": [],
            "category": "概念",
            "definition": None,
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u000001"],
        }
        context = {
            "evidence_ids": ["u000001"],
            "evidence_texts": {"u000001": "树穴"},
            "draft_ids": ["d0000"],
            "expected_names": {"d0000": "树穴"},
            "candidate_ids": ["d0000"],
        }
        cases = [
            (
                Finding,
                {**finding, "aliases": ["private alias"]},
                "source_alias_not_found",
            ),
            (
                Finding,
                {**finding, "name": "树穴 (Tree pit)"},
                "source_parenthesized_translation_not_found",
            ),
            (
                Finding,
                {**finding, "evidence_ids": ["private id"]},
                "evidence_id_not_allowed",
            ),
            (
                SynthesisReview,
                {
                    "name_decisions": [
                        {
                            "candidate_id": "d0000",
                            "decision": "accept",
                            "reason": "源文命名",
                            "evidence_ids": ["u000001"],
                        }
                    ],
                    "findings": [{**finding, "draft_ids": ["d0001"]}],
                },
                "synthesis_partition",
            ),
            (Grouping, {"groups": []}, "candidate_partition"),
        ]
        for model, value, code in cases:
            with self.subTest(code=code), self.assertRaises(ValidationError) as failure:
                model.model_validate(value, context=context)
            details = failure.exception.errors(
                include_input=False, include_context=False
            )
            self.assertEqual(details[0]["type"], code)
            self.assertNotIn("private", details[0]["msg"])

    def test_formula_controls_and_nested_evidence(self) -> None:
        """公式转义产生的控制字符和伪造引用 ID 必须触发校验失败。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "效率",
            "aliases": [],
            "category": "概念",
            "definition": "效率为η。",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u1"],
        }
        for damaged in ["\beta", "\theta", "\rho", "\frac"]:
            with (
                self.subTest(damaged=repr(damaged)),
                self.assertRaises(ValidationError),
            ):
                Finding.model_validate({**value, "definition": damaged})
        value["definition"] = r"效率为 $\eta$，允许有根据的总结。"
        Finding.model_validate_json(json.dumps(value), context={"evidence_ids": ["u1"]})
        with self.assertRaises(ValidationError):
            Finding.model_validate(
                value,
                context={"evidence_ids": ["u2"]},
            )

    def test_english_evidence_does_not_become_chinese(self) -> None:
        """拒绝将英文证据主动译成中文，同时保留中文和数学来源场景。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "Inertia",
            "aliases": [],
            "category": "概念",
            "definition": "惯性是物体维持运动状态的性质。",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u1"],
        }
        context = {
            "evidence_texts": {
                "u1": (
                    "Inertia is the tendency of an object to maintain its s"
                    "tate of motion."
                )
            }
        }
        with self.assertRaises(ValidationError):
            Finding.model_validate(value, context=context)
        Finding.model_validate(
            {**value, "definition": "The tendency to maintain motion."}, context=context
        )
        with self.assertRaises(ValidationError) as failure:
            Finding.model_validate(
                {
                    **value,
                    "definition": "The tendency to maintain motion.",
                    "conditions": ["仅在无外力条件下"],
                },
                context=context,
            )
        self.assertEqual(
            failure.exception.errors()[0]["type"], "source_language_changed"
        )
        Finding.model_validate(
            value, context={"evidence_texts": {"u1": r"$a+b=\frac{c}{d}$"}}
        )
        for source in [
            "transport – including road, rail, air and water",
            "<table><tr><td>Type</td><td>Label colour</td></tr>"
            "<tr><td>Carbon dioxide</td><td>Black</td></tr></table>",
            "La force est une interaction entre les corps.",
        ]:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                Finding.model_validate(
                    value, context={"evidence_texts": {"u1": source}}
                )

    def test_source_id_padding_is_not_semantic_guessing(self) -> None:
        """只在规范 ID 确实属于允许集合时修正数字补零，不猜测引用。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "惯性",
            "aliases": [],
            "category": "概念",
            "definition": "保持运动状态的性质。",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u1", "u000001"],
        }
        result = Finding.model_validate(value, context={"evidence_ids": ["u000001"]})
        self.assertEqual(result.evidence_ids, ["u000001"])
        for unknown in ["u2", "u-1", "u0000001", "u１２"]:
            with self.subTest(unknown=unknown), self.assertRaises(ValidationError):
                Finding.model_validate(
                    {**value, "evidence_ids": [unknown]},
                    context={"evidence_ids": ["u000001"]},
                )
        self.assertEqual(
            Finding.model_validate(
                {**value, "evidence_ids": ["u1"]}, context={"evidence_ids": ["u1"]}
            ).evidence_ids,
            ["u1"],
        )

    def test_internal_id_is_not_display_prose(self) -> None:
        """拒绝把内部定位 ID 写入展示文本，但保留原文已有的同形标识。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "概念",
            "aliases": [],
            "category": "概念",
            "definition": "说明（来源: u000001）",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u000001"],
        }
        with self.assertRaises(ValidationError):
            Finding.model_validate(
                value, context={"evidence_texts": {"u000001": "原文说明。"}}
            )
        Finding.model_validate(
            value, context={"evidence_texts": {"u000001": "原文中的变量是u000001。"}}
        )

    def test_unsolicited_translation_and_alias_are_rejected(self) -> None:
        """上下文补全名称不要求连续逐字出现，仍拒绝无来源别名和翻译。"""
        value = {
            "support_reason": "测试来源支持范围",
            "definition_supported": True,
            "confidence": "high",
            "name": "II级基本杆组",
            "aliases": [],
            "category": "概念",
            "definition": "允许总结。",
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u000001"],
        }
        context = {"evidence_texts": {"u000001": "表头：基本杆组。表项：II级组。"}}
        result = Finding.model_validate(
            {**value, "aliases": ["II级组"]}, context=context
        )
        self.assertEqual(result.name, "II级基本杆组")
        self.assertNotIn(result.name, context["evidence_texts"]["u000001"])
        with self.assertRaises(ValidationError):
            Finding.model_validate(
                {**value, "aliases": ["Basic group"]}, context=context
            )
        with self.assertRaises(ValidationError):
            Finding.model_validate(
                {**value, "name": "II级基本杆组（Basic group）"},
                context=context,
            )

    def test_name_stage_has_no_definition_status_and_keeps_guards(self) -> None:
        """名称阶段不产生缺释义状态，仍拒绝错误引用、翻译和内部ID。"""
        context = {
            "evidence_ids": ["u000001"],
            "evidence_texts": {"u000001": "The law is a rule of motion and of force."},
        }
        value = {"name": "law", "evidence_ids": ["u000001"]}
        anchor = NameAnchor.model_validate(value, context=context)
        result = NameDiscovery(
            findings=[anchor], needs_context=False, context_reason=""
        )
        self.assertEqual(set(result.findings[0].model_dump()), {"name", "evidence_ids"})
        for bad in [
            {**value, "name": "法则"},
            {**value, "name": "law u000001"},
            {**value, "evidence_ids": ["u000002"]},
            {**value, "name": " "},
            {**value, "definition": None},
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                NameAnchor.model_validate(bad, context=context)
        with self.assertRaises(ValidationError):
            NameAnchor.model_validate(
                {"name": "果树（Tree Planting）", "evidence_ids": ["u000001"]},
                context={"evidence_texts": {"u000001": "果树。"}},
            )


if __name__ == "__main__":
    unittest.main()
