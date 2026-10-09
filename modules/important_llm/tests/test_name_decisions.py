"""验证名称判定与综合同次响应、拒绝覆盖、失败身份及独立审计检查。"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError
from synthesis_fixtures import independent_review

from book_extractor.markdown import Unit
from book_extractor.models import (
    Candidate,
    Finding,
    Grouping,
    IndependentSynthesis,
    NameDiscovery,
    SynthesisReview,
)
from book_extractor.pipeline import extract_book
from book_extractor.synthesis import synthesize
from scripts.validate_runs import audit_group, validate
from scripts.validate_runs import main as validate_main


class NameClient:
    """确定性返回名称及其判定，用于无网络的完整导出回归。"""

    model = "name-audit-test"
    max_output_tokens = 8192
    extra_body: dict[str, Any] = {}
    stats: dict[str, int] = {}
    last_call_attempts = 1

    def __init__(self, mode: str = "mixed") -> None:
        """mode控制全拒或失败，调用次数用于核验恢复。"""
        self.mode = mode
        self.calls = 0

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """接受生产线程归属参数，测试不虚构HTTP事件。"""
        return nullcontext()

    def call(
        self, model: type, messages: list[dict], *, context: dict, attempts: int = 1
    ) -> Any:
        """用生产Schema校验返回，未列出的调用直接失败，避免隐藏额外轮次。"""
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        if model is NameDiscovery:
            return model.model_validate(
                {
                    "findings": [
                        {"name": name, "evidence_ids": [payload["core"][0]["id"]]}
                        for name in ["几个基本概念", "动量", "原词"]
                    ],
                    "needs_context": False,
                    "context_reason": "",
                },
                context=context,
            )
        if model not in {SynthesisReview, IndependentSynthesis}:
            raise AssertionError("Unexpected extra model phase")
        if self.mode == "fail":
            raise RuntimeError("offline failure")
        decisions = []
        findings = []
        for candidate in payload["candidates"]:
            name = candidate["name"]
            decision = (
                "reject"
                if self.mode == "reject_all" or name == "几个基本概念"
                else "uncertain"
                if name == "原词"
                else "accept"
            )
            ids = (
                [p["id"] for p in candidate["sources"] or candidate["name_sources"]]
                if model is IndependentSynthesis
                else candidate["evidence_ids"] or candidate["name_evidence_ids"]
            )
            decisions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "decision": decision,
                    "reason": "栏目包装"
                    if decision == "reject"
                    else "已点名但缺定义"
                    if decision == "accept"
                    else "名称身份尚不明确",
                    "evidence_ids": ids,
                }
            )
            if decision != "reject":
                findings.append(
                    {
                        "support_reason": "测试来源支持范围",
                        "definition_supported": False,
                        "confidence": "high",
                        "name": name,
                        "definition": None,
                        "aliases": [],
                        "category": "概念",
                        "conditions": [],
                        "issues": [],
                        "evidence_ids": ids,
                        "draft_ids": [candidate["candidate_id"]],
                    }
                )
        if model is IndependentSynthesis:
            lookup = {row["draft_ids"][0]: row for row in findings}
            return model.model_validate(
                {
                    "items": [
                        {
                            **row,
                            "finding": {
                                key: value
                                for key, value in lookup[row["candidate_id"]].items()
                                if key != "draft_ids"
                            }
                            if row["candidate_id"] in lookup
                            else None,
                        }
                        for row in decisions
                    ]
                },
                context=context,
            )
        return model.model_validate(
            {"name_decisions": decisions, "findings": findings}, context=context
        )


class NameDecisionTests(unittest.TestCase):
    """名称身份决定与覆盖校验必须保持严格，不能把执行失败伪装成拒绝。"""

    def test_group_audit_requires_real_matching_cache_and_singleton_uncertainty(
        self,
    ) -> None:
        """审计组由真实分组缓存确认，伪造key/组或放大uncertain单例均拒绝。"""
        lookup = {
            key: Candidate(
                candidate_id=key,
                chunk_id="chunk",
                scope=[],
                name="甲",
                evidence_ids=["u" + key],
            )
            for key in ["a", "b", "c"]
        }
        key = "1" * 64
        row = {
            "candidate_id": "a",
            "decision": "reject",
            "group_candidate_ids": ["a", "b"],
            "grouping_cache_key": key,
        }
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            cache = directory / "synthesis" / f"grouping-{key}.json"
            cache.parent.mkdir()
            cache.write_text(
                Grouping(
                    groups=[
                        {"candidate_ids": ["a", "b"], "status": "same"},
                        {"candidate_ids": ["c"], "status": "uncertain"},
                    ]
                ).model_dump_json(),
                encoding="utf-8",
            )
            self.assertEqual(audit_group(row, directory, lookup, {}), ["a", "b"])
            for change in [
                {"grouping_cache_key": "../bad"},
                {"group_candidate_ids": ["a", "c"]},
                {"grouping_cache_key": None},
                {"decision": "unreviewed"},
                {"candidate_id": "c", "group_candidate_ids": ["a", "b", "c"]},
            ]:
                with self.subTest(change=change), self.assertRaises(AssertionError):
                    audit_group({**row, **change}, directory, lookup, {})
            with self.assertRaises(FileNotFoundError):
                audit_group(
                    {**row, "grouping_cache_key": "2" * 64}, directory, lookup, {}
                )
            self.assertEqual(
                audit_group(
                    {**row, "candidate_id": "c", "group_candidate_ids": ["c"]},
                    directory,
                    lookup,
                    {},
                ),
                ["c"],
            )

    def test_validation_does_not_skip_run_without_records(self) -> None:
        """只有中断清单、没有交付文件的运行不能被当作空批次验证通过。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "interrupted"
            run.mkdir()
            (run / "manifest.json").write_text('{"status":"interrupted"}')
            with patch("sys.argv", ["validate_runs", str(root)]):
                with self.assertRaises(FileNotFoundError):
                    validate_main()
            self.assertFalse((root / "validation.json").exists())

    def run_book(self, root: Path, mode: str = "mixed") -> tuple[dict, NameClient]:
        """禁用规则支路以固定三候选，其他综合、缓存和导出均使用生产实现。"""
        source = root / "book.md"
        source.write_text(
            "几个基本概念。这里点名动量和原词，未说明定义。\n\n另一段仅供定位验证。",
            encoding="utf-8",
        )
        client = NameClient(mode)
        with patch("book_extractor.pipeline.extract_rule_candidates", return_value=[]):
            result = extract_book(source, root / "runs", client, workers=1)
        return result, client

    def test_mixed_decisions_resume_and_tampering(self) -> None:
        """包装被拒、有名无定义被接受、不确定被保留；恢复审计不重复，篡改可检出。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result, client = self.run_book(root)
            # 生产只执行发现与综合，不额外调用实验主体识别。
            self.assertEqual(client.calls, 2)
            directory = Path(result["directory"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(
                result["name_decision_statistics"],
                {
                    "accepted": 1,
                    "rejected": 1,
                    "uncertain": 1,
                    "unreviewed": 0,
                    "failed": 0,
                },
            )
            self.assertEqual(validate(directory)["records"], 2)
            audit_path = directory / "name_decisions.jsonl"
            original = audit_path.read_text(encoding="utf-8")
            again, resumed = self.run_book(root)
            self.assertEqual(resumed.calls, 0)
            self.assertEqual(audit_path.read_text(encoding="utf-8"), original)
            rows = [json.loads(line) for line in original.splitlines()]
            for change in ["duplicate", "source", "reason", "rejection"]:
                edited = [dict(row) for row in rows]
                if change == "duplicate":
                    edited.append(edited[0])
                elif change == "source":
                    edited[0]["evidence_ids"] = ["foreign"]
                elif change == "reason":
                    edited[0]["reason"] = "  "
                else:
                    next(r for r in edited if r["decision"] == "reject")["decision"] = (
                        "accept"
                    )
                audit_path.write_text(
                    "\n".join(json.dumps(row) for row in edited), encoding="utf-8"
                )
                with (
                    self.subTest(change=change),
                    self.assertRaises((AssertionError, ValidationError)),
                ):
                    validate(directory)
            audit_path.write_text(original, encoding="utf-8")
            records_path = directory / "records.jsonl"
            records = [
                json.loads(line)
                for line in records_path.read_text(encoding="utf-8").splitlines()
            ]
            records[0]["aliases"] = ["原文没有这个别名"]
            records_path.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                validate(directory)
            records[0]["aliases"] = []
            units = json.loads((directory / "units.json").read_text(encoding="utf-8"))
            records[0]["evidence_ids"] = [units[-1]["id"]]
            records_path.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            with self.assertRaisesRegex(AssertionError, "borrows another candidate"):
                validate(directory)

    def test_all_rejected_and_failed_are_different(self) -> None:
        """全拒可以合法完成且零记录；失败必须保留三条null并完整声明未审计。"""
        for mode in ["reject_all", "fail"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                result, client = self.run_book(Path(folder), mode)
                self.assertEqual(client.calls, 2)
                report = validate(Path(result["directory"]))
                counts = report["name_decisions"]
                if mode == "reject_all":
                    self.assertEqual(
                        (result["status"], report["records"], counts["rejected"]),
                        ("complete", 0, 3),
                    )
                else:
                    self.assertEqual(
                        (result["status"], report["records"], counts["failed"]),
                        ("partial", 3, 3),
                    )
                    self.assertEqual(counts["unreviewed"], 3)
                    self.assertEqual(counts["accepted"] + counts["rejected"], 0)

    def test_verification_preserves_uncertain_marker_and_audit(self) -> None:
        """同次响应的uncertain由程序标记；原文重核不得擦除该身份或改写审计。"""
        candidate = Candidate(
            candidate_id="c",
            chunk_id="chunk",
            scope=[],
            name="原词",
            evidence_ids=["u1"],
        )
        source = Unit("u1", "原词记作$x$。", 1, 1, [], "paragraph")
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=bool(source.text),
            confidence="medium",
            name="原词",
            definition=source.text,
            category="概念",
            aliases=[],
            conditions=[],
            issues=[],
            evidence_ids=["u1"],
        )

        class Client:
            """先返回不含重复问题码的判定，后返回清空issues的源文复核。"""

            model = "uncertain-test"
            calls = 0

            def call(self, model: type, messages: list[dict], *, context: dict) -> Any:
                """只允许现有综合与定向重核两次，无其他判定轮次。"""
                self.calls += 1
                if model in {SynthesisReview, IndependentSynthesis}:
                    review = SynthesisReview.model_validate(
                        {
                            "name_decisions": [
                                {
                                    "candidate_id": "c",
                                    "decision": "uncertain",
                                    "reason": "名称身份不明确",
                                    "evidence_ids": ["u1"],
                                }
                            ],
                            "findings": [{**finding.model_dump(), "draft_ids": ["c"]}],
                        },
                        context=context,
                    )
                    return (
                        independent_review(review, context)
                        if model is IndependentSynthesis
                        else review
                    )
                return finding

        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            records, rows = synthesize(
                [candidate], [source], client, "b", "r", Path(folder)
            )
            self.assertEqual(client.calls, 2)
            self.assertIn("name_uncertain", records[0].issues)
            audit = [row for row in rows if row.get("stage") == "name_decision"]
            self.assertEqual(audit[0]["decision"], "uncertain")
            resumed, rows2 = synthesize(
                [candidate], [source], client, "b", "r", Path(folder)
            )
            self.assertEqual(client.calls, 2)
            self.assertEqual(resumed, records)
            self.assertEqual(
                [r for r in rows2 if r.get("stage") == "name_decision"], audit
            )

    def test_decisions_are_required_complete_and_source_bound(self) -> None:
        """缺决定、重复ID、错挂来源以及拒绝项混入成品均不能通过Schema校验。"""
        context = {
            "draft_ids": ["c1"],
            "evidence_ids": ["u1", "u2"],
            "candidate_evidence_ids": {"c1": ["u1"]},
        }
        decision = {
            "candidate_id": "c1",
            "decision": "reject",
            "reason": "栏目包装",
            "evidence_ids": ["u1"],
        }
        cases = [
            {"findings": []},
            {"name_decisions": [], "findings": []},
            {"name_decisions": [decision, decision], "findings": []},
            {"name_decisions": [{**decision, "evidence_ids": ["u2"]}], "findings": []},
            {"name_decisions": [{**decision, "reason": " "}], "findings": []},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                SynthesisReview.model_validate(data, context=context)
        SynthesisReview.model_validate(
            {"name_decisions": [decision], "findings": []}, context=context
        )
        finding = {
            "support_reason": "测试来源支持范围",
            "definition_supported": False,
            "confidence": "high",
            "name": "概念",
            "category": "概念",
            "definition": None,
            "aliases": [],
            "conditions": [],
            "issues": [],
            "evidence_ids": ["u1"],
            "draft_ids": ["c1"],
        }
        with self.assertRaises(ValidationError):
            SynthesisReview.model_validate(
                {"name_decisions": [decision], "findings": [finding]}, context=context
            )
        with self.assertRaises(ValidationError):
            SynthesisReview.model_validate(
                {
                    "name_decisions": [{**decision, "decision": "uncertain"}],
                    "findings": [],
                },
                context=context,
            )


if __name__ == "__main__":
    unittest.main()
