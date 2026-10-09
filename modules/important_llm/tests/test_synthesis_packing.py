"""验证多个完整小碰撞桶合批，模型分组不能越过原始比较边界。"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from instructor.core.exceptions import IncompleteOutputException
from synthesis_fixtures import accepted_review, independent_from_payload

from book_extractor.llm import ContextBudgetError
from book_extractor.markdown import Unit, token_count
from book_extractor.models import Candidate, Grouping, IndependentSynthesis
from book_extractor.synthesis import _json, _payload, synthesize


class PackingClient:
    """按请求桶边界返回完整分区，不调用网络。"""

    model = "packing-test"

    def __init__(self, cross: bool = False) -> None:
        """cross用于模拟模型错误合并不同词面桶。"""
        self.cross = cross
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, model: type, messages: list[dict], **kwargs: Any) -> Any:
        """只替代模型边界，生产缓存/上下文校验仍实际执行。"""
        payload = json.loads(messages[-1]["content"])
        self.calls.append((model.__name__, payload))
        if model is IndependentSynthesis:
            return independent_from_payload(payload)
        if model is Grouping:
            buckets = payload.get(
                "comparison_buckets",
                [[row["candidate_id"] for row in payload["candidates"]]],
            )
            if self.cross:
                buckets = [[key for bucket in buckets for key in bucket]]
            return Grouping(
                groups=[
                    {"candidate_ids": bucket, "status": "same"} for bucket in buckets
                ]
            )
        lookup = {row["candidate_id"]: row for row in payload["candidates"]}
        return accepted_review(
            evidence_by_candidate={
                key: row["evidence_ids"] for key, row in lookup.items()
            },
            findings=[
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": False,
                    "confidence": "high",
                    "name": lookup[group[0]]["name"],
                    "aliases": [],
                    "definition": None,
                    "category": "概念",
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": list(
                        dict.fromkeys(
                            key
                            for member in group
                            for key in lookup[member]["evidence_ids"]
                        )
                    ),
                    "draft_ids": group,
                }
                for group in payload["candidate_groups"]
            ],
        )


class PackingTests(unittest.TestCase):
    """三个双成员小桶只需分组+综合两请求，来源与成员仍完整。"""

    def test_truncated_output_splits_buckets_and_stops(
        self,
    ) -> None:
        """输出截断不原样重发；完整桶二分保留来源，单候选仍截断则明确失败。"""
        members = [
            Candidate(
                candidate_id=f"{name}{i}",
                chunk_id="c",
                name=name,
                scope=[],
                evidence_ids=[f"u{name}{i}"],
            )
            for name in "甲乙"
            for i in range(2)
        ]
        units = [
            Unit(c.evidence_ids[0], "原始证据", 1, 1, [], "paragraph") for c in members
        ]
        client = PackingClient()
        original = client.call
        attempts = []

        def truncated(model: type, messages: list[dict], **kwargs: Any) -> Any:
            """只截断四候选大响应，缩小后的真实综合校验仍执行。"""
            payload = json.loads(messages[-1]["content"])
            attempts.append((model.__name__, len(payload["candidates"])))
            if model.__name__ == "SynthesisReview" and len(payload["candidates"]) > 2:
                raise IncompleteOutputException()
            return original(model, messages, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(client, "call", side_effect=truncated):
                records, errors = synthesize(
                    members, units, client, "b", "r", Path(folder)
                )
        self.assertEqual(
            [count for model, count in attempts if model == "SynthesisReview"],
            [4, 2, 2],
        )
        self.assertEqual(len(records), 2)
        self.assertFalse(any(row["severity"] == "error" for row in errors))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(client, "call", side_effect=IncompleteOutputException()):
                records, errors = synthesize(
                    members[:1], units, client, "b", "r", Path(folder)
                )
        self.assertEqual(len(records), 1)
        self.assertIn("synthesis_failed:IncompleteOutputException", records[0].issues)

    def test_small_collision_recovers_after_actual_budget_rejection(self) -> None:
        """真实预算拒绝才拆同名组，不裁证据，并标记跨批比较未完成。"""
        members = [
            Candidate(
                candidate_id=f"c{i}",
                chunk_id="c",
                name="甲",
                scope=[],
                evidence_ids=[f"u{i}"],
            )
            for i in range(2)
        ]
        units = [Unit(f"u{i}", "甲的解释。", 1, 1, [], "paragraph") for i in range(2)]
        with tempfile.TemporaryDirectory() as folder:
            client = PackingClient()
            original = client.call

            def bounded(model: type, messages: list[dict], **kwargs: Any) -> Any:
                """模拟完整组实际请求超限，子批仍使用原有来源校验。"""
                if model is Grouping:
                    raise ContextBudgetError("full group too large")
                return original(model, messages, **kwargs)

            with patch.object(client, "call", side_effect=bounded):
                records, errors = synthesize(
                    members, units, client, "b", "r", Path(folder)
                )
        self.assertEqual(
            [name for name, _ in client.calls],
            ["IndependentSynthesis", "IndependentSynthesis"],
        )
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all(
                payload["candidates"][0]["sources"][0]["text"] == "甲的解释。"
                for _, payload in client.calls
            )
        )
        self.assertTrue(
            any(
                row["issue"] == "cross_batch_grouping_deferred"
                and row["severity"] == "error"
                for row in errors
            )
        )

    def test_storage_failure_stops_and_restores_completed_response(self) -> None:
        """缓存替换失败抛出I/O异常；恢复从完整ready文件读取，不重复分组请求。"""
        members = [
            Candidate(
                candidate_id=f"c{i}",
                chunk_id="c",
                name="甲",
                scope=[],
                evidence_ids=[f"u{i}"],
            )
            for i in range(2)
        ]
        units = [Unit(f"u{i}", "甲的解释。", 1, 1, [], "paragraph") for i in range(2)]
        with tempfile.TemporaryDirectory() as folder:
            client = PackingClient()
            cache = Path(folder)
            with patch(
                "book_extractor.synthesis.replace_checkpoint",
                side_effect=PermissionError("locked"),
            ):
                with self.assertRaises(PermissionError):
                    synthesize(members, units, client, "b", "r", cache)
            self.assertEqual(len(list(cache.glob("*.ready"))), 1)
            self.assertEqual(len(client.calls), 1)
            records, errors = synthesize(members, units, client, "b", "r", cache)
            self.assertEqual(
                [name for name, _ in client.calls], ["Grouping", "SynthesisReview"]
            )
            self.assertTrue(records)
            self.assertFalse(any(row["severity"] == "error" for row in errors))

    def test_packing_limits_do_not_split_an_original_pair(self) -> None:
        """人数或真实payload边界只能在完整桶之间断开，不能把双成员桶拆成单例。"""
        members = [
            Candidate(
                candidate_id=f"{name}{i}",
                chunk_id="c",
                name=name,
                scope=[],
                evidence_ids=[f"u{name}{i}"],
            )
            for name in "甲乙丙"
            for i in range(2)
        ]
        units = [
            Unit(c.evidence_ids[0], "原始证据", 1, 1, [], "paragraph") for c in members
        ]
        payload = _payload(members[:4], {u.id: u for u in units})
        payload["comparison_buckets"] = [
            [f"{name}{i}" for i in range(2)] for name in "甲乙"
        ]
        cap = token_count(_json(payload))
        for option, limit in [("GROUP_SIZE", 5), ("PAYLOAD_TOKENS", cap)]:
            with (
                self.subTest(option=option),
                tempfile.TemporaryDirectory() as folder,
                patch(f"book_extractor.synthesis.{option}", limit),
            ):
                client = PackingClient()
                _, errors = synthesize(members, units, client, "b", "r", Path(folder))
                grouping = [
                    payload for name, payload in client.calls if name == "Grouping"
                ]
                self.assertEqual(
                    [len(payload["candidates"]) for payload in grouping], [4, 2]
                )
                self.assertTrue(
                    all(
                        len(bucket) == 2
                        for payload in grouping
                        for bucket in payload["comparison_buckets"]
                    )
                )
                self.assertFalse(any(row["severity"] == "error" for row in errors))

    def test_three_pairs_share_requests_but_never_merge_across_buckets(self) -> None:
        """正常响应从六请求降至两请求；跨桶合并必须失败且候选全保留。"""
        members = [
            Candidate(
                candidate_id=f"{name}{index}",
                chunk_id="chunk",
                name=name,
                scope=[],
                evidence_ids=[f"u{name}{index}"],
            )
            for name in "甲乙丙"
            for index in range(2)
        ]
        units = [
            Unit(c.evidence_ids[0], f"{c.name}的原始证据", 1, 1, [], "paragraph")
            for c in members
        ]
        for cross in (False, True):
            with self.subTest(cross=cross), tempfile.TemporaryDirectory() as folder:
                client = PackingClient(cross)
                records, errors = synthesize(
                    members, units, client, "b", "r", Path(folder)
                )
                self.assertEqual(
                    {key for record in records for key in record.candidate_ids},
                    {c.candidate_id for c in members},
                )
                if cross:
                    self.assertEqual(len(client.calls), 1)
                    self.assertTrue(any(row["severity"] == "error" for row in errors))
                else:
                    self.assertEqual(
                        [name for name, _ in client.calls],
                        ["Grouping", "SynthesisReview"],
                    )
                    self.assertEqual(
                        [record.name for record in records], list("甲乙丙")
                    )
                    self.assertEqual(
                        {key for record in records for key in record.evidence_ids},
                        {u.id for u in units},
                    )
                    self.assertEqual(
                        client.calls[0][1]["comparison_buckets"],
                        [["c1", "c2"], ["c3", "c4"], ["c5", "c6"]],
                    )
                    self.assertFalse(any(row["severity"] == "error" for row in errors))
                    audits = [
                        row for row in errors if row.get("stage") == "name_decision"
                    ]
                    self.assertEqual(len(audits), 6)
                    self.assertEqual(
                        len({row["grouping_cache_key"] for row in audits}), 1
                    )
                    self.assertTrue(
                        all(len(row["group_candidate_ids"]) == 2 for row in audits)
                    )
                    restored, restored_errors = synthesize(
                        members, units, client, "b", "r", Path(folder)
                    )
                    self.assertEqual(len(client.calls), 2)
                    self.assertEqual((restored, restored_errors), (records, errors))
