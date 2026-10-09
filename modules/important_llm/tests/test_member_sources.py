"""离线验证批次成员的字符区间与语言保护不能相互借证。"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from synthesis_fixtures import independent_from_payload

from book_extractor.markdown import Unit
from book_extractor.models import Candidate, Finding, Grouping, IndependentSynthesis
from book_extractor.synthesis import _payload, synthesize


class MemberClient:
    """保存生产请求，按给定Finding返回并执行真实上下文校验。"""

    model = "member-source-test"

    def __init__(self, definitions: dict[str, tuple[str, list[str]]]) -> None:
        """指定各成员释义及别名，不改变生产校验器。"""
        self.confidence = "high"
        self.definitions = definitions
        self.payloads: list[dict] = []
        self.failures: list[dict] = []

    def call(self, model: type, messages: list[dict], *, context: dict) -> Any:
        """支持现有分组、综合和定向复核阶段，不访问网络。"""
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if model is Grouping:
            return model.model_validate(
                {
                    "groups": [
                        {
                            "candidate_ids": [
                                c["candidate_id"] for c in payload["candidates"]
                            ],
                            "status": "same",
                        }
                    ]
                },
                context=context,
            )
        if model is Finding:
            return model.model_validate(
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": bool(
                        "\n".join(p["text"] for p in payload["sources"])
                    ),
                    "confidence": self.confidence,
                    "name": payload["name"],
                    "definition": "\n".join(p["text"] for p in payload["sources"]),
                    "aliases": [],
                    "conditions": [],
                    "issues": [],
                    "category": "概念",
                    "evidence_ids": [p["id"] for p in payload["sources"]],
                },
                context=context,
            )
        if model is IndependentSynthesis:
            answer = independent_from_payload(payload)
            for item in answer.items:
                definition, aliases = self.definitions[item.finding.name]
                item.finding.confidence = self.confidence
                item.finding.definition = definition
                item.finding.definition_supported = bool(definition)
                item.finding.aliases = aliases
            try:
                return model.model_validate(answer.model_dump(), context=context)
            except ValidationError as error:
                self.failures = error.errors(include_input=False, include_context=False)
                raise
        decisions = [
            {
                "candidate_id": c["candidate_id"],
                "decision": "accept",
                "reason": "源文命名",
                "evidence_ids": c["evidence_ids"],
            }
            for c in payload["candidates"]
        ]
        lookup = {c["candidate_id"]: c for c in payload["candidates"]}
        findings = []
        for group in payload["candidate_groups"]:
            c = lookup[group[0]]
            definition, aliases = self.definitions[c["name"]]
            findings.append(
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": bool(definition),
                    "confidence": self.confidence,
                    "name": c["name"],
                    "definition": definition,
                    "aliases": aliases,
                    "category": "概念",
                    "conditions": [],
                    "issues": [],
                    "draft_ids": group,
                    "evidence_ids": list(
                        dict.fromkeys(
                            i for key in group for i in lookup[key]["evidence_ids"]
                        )
                    ),
                }
            )
        try:
            return model.model_validate(
                {"name_decisions": decisions, "findings": findings}, context=context
            )
        except ValidationError as error:
            self.failures = error.errors(include_input=False, include_context=False)
            raise


class MemberSourcesTests(unittest.TestCase):
    """错误借证必须拒绝，合法混批和合并后的精确多片段必须继续工作。"""

    def test_same_unit_disjoint_alias_cannot_cross_members(self) -> None:
        """同Unit不同span不因引用ID相同而共享别名证据。"""
        left, right = "甲是概念。", "乙又称乙别名。"
        members = [
            Candidate(
                candidate_id=key,
                chunk_id="chunk",
                scope=[],
                name=name,
                evidence_ids=["u"],
                evidence_spans=[{"unit_id": "u", "start": start, "end": end}],
            )
            for key, name, start, end in [
                ("a", "甲", 0, len(left)),
                ("b", "乙", len(left), len(left + right)),
            ]
        ]
        source = [Unit("u", left + right, 1, 1, [], "paragraph")]
        client = MemberClient(
            {"甲": ("甲是概念。", ["乙别名"]), "乙": ("乙是概念。", [])}
        )
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                members, source, client, "b", "r", Path(folder)
            )
        self.assertIsNone(records[0].definition)
        self.assertEqual(records[1].definition, "乙是概念。")
        self.assertTrue(any(e.get("severity") == "error" for e in errors))
        self.assertEqual(
            client.payloads[1]["validation_errors"]["c1"][0],
            {"type": "source_alias_not_found", "loc": ["items", 0, "finding"]},
        )
        candidates = client.payloads[0]["candidates"]
        self.assertEqual([c["candidate_id"] for c in candidates], ["c1", "c2"])
        parts = [part for candidate in candidates for part in candidate["sources"]]
        self.assertTrue(all("candidate_ids" not in part for part in parts))
        self.assertEqual([p["text"] for p in parts], [left, right])

    def test_chinese_neighbor_cannot_disable_english_source_guard(self) -> None:
        """混合语言合批仍逐成员校验；正确语言输出不增加调用或被误拒。"""
        members = [
            Candidate(
                candidate_id=k, chunk_id="chunk", scope=[], name=n, evidence_ids=[u]
            )
            for k, n, u in [("a", "Force", "u1"), ("b", "速度", "u2")]
        ]
        english = "Force is the cause of a change in the motion of an object."
        source = [
            Unit("u1", english, 1, 1, [], "paragraph"),
            Unit("u2", "速度是运动概念。", 2, 2, [], "paragraph"),
        ]
        for definition, accepted in [
            ("力是改变运动状态的作用。", False),
            (english, True),
        ]:
            with (
                self.subTest(accepted=accepted),
                tempfile.TemporaryDirectory() as folder,
            ):
                client = MemberClient(
                    {"Force": (definition, []), "速度": ("速度是运动概念。", [])}
                )
                records, errors = synthesize(
                    members, source, client, "b", "r", Path(folder)
                )
                self.assertEqual(len(client.payloads), 1 if accepted else 3)
                if not accepted:
                    self.assertTrue(
                        all(
                            [item["name"] for item in payload["candidates"]]
                            == ["Force"]
                            for payload in client.payloads[1:]
                        )
                    )
                self.assertEqual(
                    bool([e for e in errors if e.get("severity") == "error"]),
                    not accepted,
                )
                self.assertEqual(
                    records[0].definition, definition if accepted else None
                )

    def test_verification_preserves_all_owned_spans_without_outside_text(self) -> None:
        """同义合并后的复核回取所有成员span，不能覆盖掉同ID前片段或展开间隙。"""
        first, gap, second = "甲记为$x$。", "不应发送的间隙", "甲还有另一个性质。"
        members = [
            Candidate(
                candidate_id=k,
                chunk_id="chunk",
                scope=[],
                name="甲",
                evidence_ids=["u"],
                evidence_spans=[{"unit_id": "u", "start": a, "end": b}],
            )
            for k, a, b in [
                ("a", 0, len(first)),
                ("b", len(first + gap), len(first + gap + second)),
            ]
        ]
        source = [Unit("u", first + gap + second, 1, 1, [], "paragraph")]
        client = MemberClient({"甲": (first + second, [])})
        client.confidence = "medium"
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                members, source, client, "b", "r", Path(folder)
            )
        self.assertFalse(any(e.get("severity") == "error" for e in errors))
        self.assertEqual(len(client.payloads), 3)
        verification = client.payloads[-1]
        self.assertEqual(verification["sources"][0]["text"], first + "\n" + second)
        self.assertNotIn(gap, json.dumps(client.payloads, ensure_ascii=False))
        self.assertEqual(records[0].evidence_ids, ["u"])

    def test_name_evidence_deduplicates_only_for_the_same_owner(self) -> None:
        """同owner同区间只发正文，另owner的名称来源仍保留且不串证。"""
        text = "甲与乙列名。"
        body = Candidate(
            candidate_id="a",
            chunk_id="c",
            scope=[],
            name="甲",
            evidence_ids=["u"],
            name_evidence_ids=["u"],
        )
        name = Candidate(
            candidate_id="b",
            chunk_id="r",
            scope=[],
            name="乙",
            evidence_ids=[],
            name_evidence_ids=["u"],
            origins=["index"],
        )
        payload = _payload([body, name], {"u": Unit("u", text, 1, 1, [], "paragraph")})
        self.assertEqual(payload["sources"][0]["candidate_ids"], ["a"])
        self.assertEqual(payload["name_sources"][0]["candidate_ids"], ["b"])
        self.assertEqual(payload["name_sources"][0]["text"], text)
        self.assertEqual(payload["candidates"][0]["name_evidence_ids"], ["u"])
        self.assertNotIn("chunk_id", payload["candidates"][0])
        self.assertNotIn("evidence_spans", payload["candidates"][0])
        self.assertEqual(body.chunk_id, "c")
        self.assertIn("evidence_spans", body.model_dump())

    def test_name_only_member_does_not_lose_its_own_name_source(self) -> None:
        """同Unit另一候选的body片段不能替换纯名称候选的实际名称出处。"""
        text = "甲的正文。乙仅列名。"
        body = Candidate(
            candidate_id="a",
            chunk_id="c",
            scope=[],
            name="甲",
            evidence_ids=["u"],
            evidence_spans=[{"unit_id": "u", "start": 0, "end": 5}],
        )
        name = Candidate(
            candidate_id="b",
            chunk_id="r",
            scope=[],
            name="乙",
            evidence_ids=[],
            name_evidence_ids=["u"],
            origins=["index"],
        )
        payload = _payload([body, name], {"u": Unit("u", text, 1, 1, [], "paragraph")})
        self.assertEqual(payload["name_sources"][0]["text"], text)
        self.assertEqual(payload["name_sources"][0]["candidate_ids"], ["b"])


if __name__ == "__main__":
    unittest.main()
