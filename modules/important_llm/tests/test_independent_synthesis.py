"""验证独立候选由脚本回填成员身份，错误响应仍被来源和覆盖校验拒绝。"""

import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import Any

from synthesis_fixtures import independent_from_payload

from book_extractor.markdown import Unit
from book_extractor.models import Candidate, IndependentSynthesis
from book_extractor.synthesis import _synthesize_batch


class IndependentClient:
    """模拟新传输协议，可注入不同机械或来源错误。"""

    model = "independent-test"

    def __init__(self, fault: str = "") -> None:
        """fault指定错误类型；payloads保留输入以核对来源隔离。"""
        self.fault = fault
        self.payloads: list[dict] = []

    def call(self, model: type, messages: list[dict], **kwargs: Any) -> Any:
        """用当前请求生成响应，生产调用边界执行实际上下文校验。"""
        assert model is IndependentSynthesis
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        answer = independent_from_payload(payload)
        if self.fault == "unknown":
            answer.items[0].candidate_id = "c99"
        elif self.fault == "duplicate":
            answer.items[1].candidate_id = answer.items[0].candidate_id
        elif self.fault == "missing":
            answer.items.pop()
        elif self.fault == "foreign_source":
            answer.items[0].finding.evidence_ids = ["u000002"]
        elif self.fault == "foreign_alias":
            answer.items[0].finding.aliases = ["乙"]
        elif self.fault == "reject_with_finding":
            answer.items[0].decision = "reject"
        elif self.fault == "accept_without_finding":
            answer.items[0].finding = None
        elif self.fault == "copy_name":
            answer.items[0].finding.name = None
        return answer


class IndependentSynthesisTests(unittest.TestCase):
    """验证脚本组装的结构安全性及可恢复性。"""

    def test_resume_partial_cache_retries_unresolved_members(self) -> None:
        """恢复保留两轮成功项；空失败响应不冻结，剩余成员仍拥有三次实际请求额度。"""
        members = [
            Candidate(
                candidate_id=f"original-{index}",
                chunk_id="chunk",
                name=name,
                scope=[],
                evidence_ids=[f"u00000{index}"],
            )
            for index, name in enumerate(["甲", "乙", "丙"], 1)
        ]
        source = {
            member.evidence_ids[0]: Unit(
                member.evidence_ids[0], f"{member.name}的原文。", 1, 1, [], "paragraph"
            )
            for member in members
        }
        client = IndependentClient()
        original_call = client.call

        def delayed_repair(model: type, messages: list[dict], **kwargs: Any) -> Any:
            """逐轮释放丙和乙；甲直到第六次真实调用才通过校验。"""
            answer = original_call(model, messages, **kwargs)
            for item in answer.items:
                if (len(client.payloads) == 1 and item.candidate_id == "c2") or (
                    len(client.payloads) < 6 and item.candidate_id == "c1"
                ):
                    item.finding.aliases = ["无来源别名"]
            return answer

        client.call = delayed_repair
        with tempfile.TemporaryDirectory() as folder:
            args = (members, False, source, client, "b", "r", Path(folder), Event())
            _, issues = _synthesize_batch(*args)
            self.assertEqual(len(client.payloads), 3)
            self.assertEqual(
                [row["candidate_id"] for row in issues if row.get("failed")],
                [members[0].candidate_id],
            )
            records, issues = _synthesize_batch(*args)
            self.assertEqual(len(client.payloads), 6)
            self.assertFalse(any(row.get("failed") for row in issues))
            self.assertEqual([record.name for record in records], ["甲", "乙", "丙"])
            self.assertTrue(
                all(
                    [row["candidate_id"] for row in payload["candidates"]] == ["c1"]
                    for payload in client.payloads[2:]
                )
            )
            self.assertEqual(_synthesize_batch(*args), (records, issues))
            self.assertEqual(len(client.payloads), 6)

    def test_retry_only_invalid_member_and_reuse_partial_cache(self) -> None:
        """单项别名失败不重生成成功项，恢复时复用全部已校验响应。"""
        members = [
            Candidate(
                candidate_id=f"original-{index}",
                chunk_id="chunk",
                name=name,
                scope=[],
                evidence_ids=[f"u00000{index}"],
            )
            for index, name in enumerate(["甲", "乙"], 1)
        ]
        source = {
            member.evidence_ids[0]: Unit(
                member.evidence_ids[0], f"{member.name}的原文。", 1, 1, [], "paragraph"
            )
            for member in members
        }
        client = IndependentClient("foreign_alias")
        original_call = client.call

        def repair(model: type, messages: list[dict], **kwargs: Any) -> Any:
            """首次伪造别名，下一次只根据实际收到的剩余候选正确返回。"""
            result = original_call(model, messages, **kwargs)
            client.fault = ""
            return result

        client.call = repair
        with tempfile.TemporaryDirectory() as folder:
            args = (members, False, source, client, "b", "r", Path(folder), Event())
            records, issues = _synthesize_batch(*args)
            self.assertFalse(any(row.get("failed") for row in issues))
            self.assertEqual([record.name for record in records], ["甲", "乙"])
            self.assertIn(
                "source_alias_not_found", client.payloads[1]["repair_instructions"]
            )
            self.assertEqual(
                [
                    [row["candidate_id"] for row in payload["candidates"]]
                    for payload in client.payloads
                ],
                [["c1", "c2"], ["c1"]],
            )
            self.assertEqual(_synthesize_batch(*args), (records, issues))
            self.assertEqual(len(client.payloads), 2)

    def test_assembly_cache_and_invalid_responses(self) -> None:
        """合法响应恢复原ID并零请求恢复；部分缓存只包含逐项严格校验成功的成员。"""
        members = [
            Candidate(
                candidate_id=f"c00000{i}-123456789abcdef12345",
                chunk_id="chunk",
                name=name,
                scope=[],
                evidence_ids=[f"u00000{i}"],
            )
            for i, name in enumerate(["甲", "乙"], 1)
        ]
        source = {
            member.evidence_ids[0]: Unit(
                member.evidence_ids[0], f"{member.name}的原文。", 1, 1, [], "paragraph"
            )
            for member in members
        }
        for fault in (
            "",
            "unknown",
            "duplicate",
            "missing",
            "foreign_source",
            "foreign_alias",
            "reject_with_finding",
            "accept_without_finding",
            "copy_name",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                client = IndependentClient(fault)
                args = (members, False, source, client, "b", "r", directory, Event())
                records, issues = _synthesize_batch(*args)
                payload = client.payloads[0]
                self.assertNotIn("candidate_groups", payload)
                self.assertNotIn(
                    "candidate_ids", payload["candidates"][0]["sources"][0]
                )
                self.assertEqual(
                    payload["candidates"][0]["sources"][0]["text"], "甲的原文。"
                )
                self.assertEqual(
                    [record.candidate_ids for record in records],
                    [[member.candidate_id] for member in members],
                )
                if fault and fault != "copy_name":
                    self.assertTrue(any(row.get("failed") for row in issues))
                    if fault in {"unknown", "duplicate"}:
                        self.assertFalse(list(directory.glob("*.json")))
                        continue
                    failed_short = "c2" if fault == "missing" else "c1"
                    cached_items = [
                        item
                        for path in directory.glob("*.json")
                        for item in json.loads(path.read_text(encoding="utf-8"))[
                            "response"
                        ]["items"]
                    ]
                    self.assertTrue(cached_items)
                    self.assertTrue(
                        all(
                            item["candidate_id"] != failed_short
                            for item in cached_items
                        )
                    )
                    self.assertEqual(len(client.payloads), 3)
                    self.assertEqual(
                        [len(row["candidates"]) for row in client.payloads], [2, 1, 1]
                    )
                else:
                    self.assertEqual(records[0].name, members[0].name)
                    self.assertFalse(any(row.get("failed") for row in issues))
                    self.assertEqual(_synthesize_batch(*args), (records, issues))
                    self.assertEqual(len(client.payloads), 1)
