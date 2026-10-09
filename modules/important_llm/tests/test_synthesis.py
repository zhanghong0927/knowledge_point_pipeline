"""离线验证书内分组、证据追溯和综合结果缓存恢复。"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel
from synthesis_fixtures import (
    accepted_review,
    independent_from_payload,
    independent_review,
)

from book_extractor.llm import LLMClient
from book_extractor.markdown import Unit
from book_extractor.markdown import token_count as real_token_count
from book_extractor.models import (
    Candidate,
    EvidenceSpan,
    Finding,
    Grouping,
    IndependentSynthesis,
    SynthesisReview,
)
from book_extractor.prompts import SOURCE_ENGLISH_SUFFIX
from book_extractor.synthesis import (
    PAYLOAD_TOKENS,
    _buckets,
    _cached_call,
    _json,
    _payload,
    synthesize,
)


class FakeClient:
    """按序返回预设响应，记录逻辑调用次数和实际消息。"""

    model = "offline-test"

    def __init__(self, outputs: list[BaseModel | Exception]) -> None:
        """保存 outputs 响应或异常序列，初始化调用计数和消息记录。"""
        self.outputs = iter(outputs)
        self.calls = 0
        self.messages: list[list[dict[str, Any]]] = []

    def call(
        self,
        response_model: type[BaseModel],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> BaseModel:
        """记录 messages，按序返回模型结果或抛出异常；

        response_model 和 context 由调用方校验。
        """
        self.calls += 1
        self.messages.append(messages)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        if response_model is IndependentSynthesis:
            return independent_review(output, context)
        if response_model in {Grouping, SynthesisReview}:
            # 这些旧夹具用证据单元ID作为原候选ID，模型响应改用实际请求中的短编号。
            payload = json.loads(messages[-1]["content"])
            mapping = {
                row["evidence_ids"][0]: row["candidate_id"]
                for row in payload["candidates"]
            }
            data = output.model_dump()
            if response_model is Grouping:
                for group in data["groups"]:
                    group["candidate_ids"] = [
                        mapping.get(k, k) for k in group["candidate_ids"]
                    ]
            else:
                for decision in data["name_decisions"]:
                    key = decision["candidate_id"]
                    decision["candidate_id"] = mapping.get(key, key)
                for finding in data["findings"]:
                    finding["draft_ids"] = [
                        mapping.get(k, k) for k in finding["draft_ids"]
                    ]
            return response_model.model_validate(data)
        return output


def candidate(
    key: str, name: str = "力", conditions: list[str] | None = None
) -> Candidate:
    """由 key、name 和 conditions 构造带对应证据 ID 的局部候选并返回。"""
    return Candidate(
        name=name,
        aliases=[],
        issues=[],
        evidence_ids=[key],
        candidate_id=key,
        chunk_id=f"chunk-{key}",
        scope=["章节"],
    )


def units(candidates: list[Candidate]) -> list[Unit]:
    """把 candidates 的定义转换为一一对应的源单元列表，使用候选 ID 作为单元 ID。"""
    return [
        Unit(c.candidate_id, f"{c.name}的局部解释", i + 1, i + 1, ["章节"], "paragraph")
        for i, c in enumerate(candidates)
    ]


class SynthesisTests(unittest.TestCase):
    """验证失败回退不丢候选、条件保留及请求内容绑定缓存。"""

    def test_english_suffix_is_source_selected_and_cached(self) -> None:
        """英文证据触发末尾约束且缓存恢复不重发；中文/法语不被要求译成英语。"""
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=False,
            confidence="high",
            name="Force",
            definition=None,
            aliases=[],
            conditions=[],
            category="concept",
            issues=[],
            evidence_ids=["u1"],
        )
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient([finding, finding, finding])
            for index, source in enumerate(
                [
                    "The force is an interaction of bodies and is a vector.",
                    "力是物体之间的相互作用。",
                    "La force est une interaction entre les corps.",
                ]
            ):
                context = {"evidence_texts": {"u1": source}}
                for _ in range(2):
                    _cached_call(client, Finding, "base", {}, context, Path(folder))
                expected = "base" + (SOURCE_ENGLISH_SUFFIX if index == 0 else "")
                self.assertEqual(client.messages[-1][0]["content"], expected)
            self.assertEqual(client.calls, 3)

    def setUp(self) -> None:
        """用字符长度替代 token 计数以隔离编排测试，分词器另有覆盖。"""
        counter = patch("book_extractor.synthesis.token_count", side_effect=len)
        counter.start()
        self.addCleanup(counter.stop)

    def test_casefold_only_proposes_comparison_without_forced_merge(self) -> None:
        """大小写变体进入Grouping，独立分组响应仍保留CO与Co原名和各自来源。"""
        members = [candidate("u1", "CO"), candidate("u2", "Co")]
        client = FakeClient(
            [
                Grouping(
                    groups=[
                        {"candidate_ids": [c.candidate_id], "status": "same"}
                        for c in members
                    ]
                ),
                accepted_review(
                    evidence_by_candidate={
                        c.candidate_id: c.evidence_ids for c in members
                    },
                    findings=[
                        {
                            "support_reason": "测试来源支持范围",
                            "definition_supported": False,
                            "confidence": "high",
                            "name": c.name,
                            "aliases": [],
                            "category": "概念",
                            "definition": None,
                            "conditions": [],
                            "issues": [],
                            "evidence_ids": c.evidence_ids,
                            "draft_ids": [c.candidate_id],
                        }
                        for c in members
                    ],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                members, units(members), client, "b", "r", Path(folder)
            )
        self.assertEqual(client.calls, 2)
        grouping_payload = json.loads(client.messages[0][-1]["content"])
        self.assertEqual(
            [c["name"] for c in grouping_payload["candidates"]], ["CO", "Co"]
        )
        self.assertEqual([record.name for record in records], ["CO", "Co"])
        self.assertEqual([record.evidence_ids for record in records], [["u1"], ["u2"]])
        self.assertFalse(any(row["severity"] == "error" for row in errors))
        # 忽略大小写仍只扩展首成员的直接别名，不把后来成员的别名递归传递。
        chain = [
            candidate("a", "Force"),
            candidate("b", "force"),
            candidate("c", "Third"),
        ]
        chain[1].aliases = ["Third"]
        self.assertEqual(
            [[c.candidate_id for c in bucket] for bucket in _buckets(chain)],
            [["a", "b"], ["c"]],
        )
        self.assertEqual(chain[1].aliases, ["Third"])

    def test_group_resume_and_conditions(self) -> None:
        """分组后保留受支持的条件及来源，相同输入恢复不新增调用。"""
        members = [candidate("u1", conditions=["仅限静态"]), candidate("u2")]
        grouping = Grouping(groups=[{"candidate_ids": ["u1", "u2"], "status": "same"}])
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=True,
            confidence="high",
            name="力",
            aliases=[],
            category="概念",
            definition="仅限静态的综合解释",
            conditions=["仅限静态"],
            issues=[],
            evidence_ids=["u1"],
        )
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    grouping,
                    accepted_review(
                        evidence_by_candidate={
                            c.candidate_id: c.evidence_ids or c.name_evidence_ids
                            for c in members
                        },
                        findings=[{**finding.model_dump(), "draft_ids": ["u1", "u2"]}],
                    ),
                ]
            )
            records, issues = synthesize(
                members, units(members), client, "book", "run", Path(directory)
            )
            self.assertEqual(client.calls, 2)
            synthesis_input = json.loads(client.messages[1][1]["content"])
            self.assertTrue(
                all(
                    "definition" not in item and "conditions" not in item
                    for item in synthesis_input["candidates"]
                )
            )
            self.assertEqual(
                [item["text"] for item in synthesis_input["sources"]],
                [unit.text for unit in units(members)],
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].candidate_ids, ["u1", "u2"])
            self.assertEqual(records[0].evidence_ids, ["u1"])
            self.assertIn("仅限静态", records[0].definition)
            second = FakeClient([])
            resumed, resumed_issues = synthesize(
                members, units(members), second, "book", "run", Path(directory)
            )
            audit = [row for row in issues if row.get("stage") == "name_decision"]
            self.assertEqual(len(audit), 2)
            self.assertTrue(
                all(row["group_candidate_ids"] == ["u1", "u2"] for row in audit)
            )
            self.assertTrue(
                all(
                    (
                        Path(directory) / f"grouping-{row['grouping_cache_key']}.json"
                    ).exists()
                    for row in audit
                )
            )
            self.assertEqual(resumed_issues, issues)
            self.assertEqual(second.calls, 0)
            self.assertEqual(resumed, records)
            changed = units(members)
            changed[0].text += "改变证据"
            fresh = FakeClient(
                [
                    grouping,
                    accepted_review(
                        evidence_by_candidate={
                            c.candidate_id: c.evidence_ids or c.name_evidence_ids
                            for c in members
                        },
                        findings=[{**finding.model_dump(), "draft_ids": ["u1", "u2"]}],
                    ),
                ]
            )
            synthesize(members, changed, fresh, "book", "run", Path(directory))
            self.assertEqual(fresh.calls, 2)

    def test_rejected_draft_claims_are_not_restored(self) -> None:
        """综合结果不能重新追加草稿中已拒绝的条件、别名或失效问题标记。"""
        members = [candidate("u1", conditions=["无来源的条件"]), candidate("u2")]
        members[0].aliases = ["无来源的别名"]
        members[0].issues = [
            "definition_missing",
            "definition_missing: 局部未解释",
            "source_formula_uncertain",
            "grouping_uncertain",
            "仅标题，原文未定义",
        ]
        grouping = Grouping(groups=[{"candidate_ids": ["u1", "u2"], "status": "same"}])
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=True,
            confidence="high",
            name="力",
            aliases=[],
            category="概念",
            definition="有来源的综合释义",
            conditions=[],
            issues=["unsupported_candidate_claim:u1"],
            evidence_ids=["u2"],
        )
        with tempfile.TemporaryDirectory() as directory:
            records, _ = synthesize(
                members,
                units(members),
                FakeClient(
                    [
                        grouping,
                        accepted_review(
                            evidence_by_candidate={
                                c.candidate_id: c.evidence_ids or c.name_evidence_ids
                                for c in members
                            },
                            findings=[
                                {**finding.model_dump(), "draft_ids": ["u1", "u2"]}
                            ],
                        ),
                    ]
                ),
                "book",
                "run",
                Path(directory),
            )
        record = records[0]
        self.assertEqual(record.conditions, [])
        self.assertEqual(record.aliases, [])
        self.assertNotIn("无来源", record.definition)
        self.assertFalse(
            any(issue.startswith("definition_missing") for issue in record.issues)
        )
        self.assertIn("source_formula_uncertain", record.issues)
        self.assertIn("grouping_uncertain", record.issues)
        self.assertNotIn("仅标题，原文未定义", record.issues)
        self.assertIn("unsupported_candidate_claim:u1", record.issues)
        self.assertEqual(set(record.evidence_ids), {"u2"})
        self.assertEqual(record.candidate_ids, ["u1", "u2"])

    def test_partition_failure_retains_candidates(self) -> None:
        """即使自定义客户端绕过校验，重复或遗漏候选 ID 也必须拒绝。"""
        members = [candidate("u1"), candidate("u2")]
        invalid = Grouping.model_construct(groups=[])
        with tempfile.TemporaryDirectory() as directory:
            records, issues = synthesize(
                members,
                units(members),
                FakeClient([invalid]),
                "book",
                "run",
                Path(directory),
            )
        self.assertEqual(len(records), 2)
        self.assertTrue(any("grouping_failed" in item["issue"] for item in issues))

    def test_uncertain_and_failure_stay_local(self) -> None:
        """分组不确定属于有效结果；综合失败仍保留局部候选及证据。"""
        members = [candidate("u1"), candidate("u2")]
        members[0].issues = ["仅标题，原文未定义"]
        grouping = Grouping(groups=[{"candidate_ids": ["u1", "u2"], "status": "same"}])
        with tempfile.TemporaryDirectory() as directory:
            records, issues = synthesize(
                members,
                units(members),
                FakeClient([grouping, RuntimeError()]),
                "book",
                "run",
                Path(directory),
            )
        self.assertEqual([r.candidate_ids for r in records], [["u1"], ["u2"]])
        self.assertIn("仅标题，原文未定义", records[0].issues)
        self.assertTrue(any("synthesis_failed" in item["issue"] for item in issues))

    def test_twenty_five_collisions_use_real_payload_budget_not_headcount(self) -> None:
        """25短同名可完整比较；长同名保留限制，25独立单例按12/12/1打包。"""
        for kind in ("short", "long", "singletons"):
            with self.subTest(kind=kind):
                members = [
                    candidate(f"u{i}", f"对象{i}" if kind == "singletons" else "力")
                    for i in range(25)
                ]
                source = [
                    Unit(
                        c.candidate_id,
                        "word " * 450 if kind == "long" else "力的含义",
                        1,
                        1,
                        [],
                        "paragraph",
                    )
                    for c in members
                ]
                complete_tokens = real_token_count(
                    _json(_payload(members, {u.id: u for u in source}))
                )
                self.assertEqual(complete_tokens > PAYLOAD_TOKENS, kind == "long")
                calls: list[tuple[str, dict[str, Any]]] = []
                client = FakeClient([])

                def respond(model: type, messages: list[dict], **kwargs: Any) -> Any:
                    """按本批完整分区返回有效结果，不把服务失败混入调度反例。"""
                    payload = json.loads(messages[-1]["content"])
                    calls.append((model.__name__, payload))
                    if model is IndependentSynthesis:
                        return independent_from_payload(payload)
                    if model is Grouping:
                        return Grouping(
                            groups=[
                                {
                                    "candidate_ids": [
                                        row["candidate_id"]
                                        for row in payload["candidates"]
                                    ],
                                    "status": "same",
                                }
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
                                "category": "概念",
                                "definition": None,
                                "conditions": [],
                                "issues": [],
                                "evidence_ids": lookup[group[0]]["evidence_ids"],
                                "draft_ids": group,
                            }
                            for group in payload["candidate_groups"]
                        ],
                    )

                with (
                    tempfile.TemporaryDirectory() as folder,
                    patch(
                        "book_extractor.synthesis.token_count",
                        side_effect=real_token_count,
                    ),
                    patch.object(client, "call", side_effect=respond),
                ):
                    records, issues = synthesize(
                        members, source, client, "b", "r", Path(folder)
                    )
                    self.assertEqual(
                        {key for record in records for key in record.candidate_ids},
                        {c.candidate_id for c in members},
                    )
                    deferred = [
                        row
                        for row in issues
                        if row.get("issue") == "cross_batch_grouping_deferred"
                        and row["severity"] == "error"
                    ]
                    self.assertEqual(bool(deferred), kind == "long")
                    if kind == "short":
                        self.assertEqual(
                            [name for name, _ in calls], ["Grouping", "SynthesisReview"]
                        )
                        self.assertEqual(len(records), 1)
                        self.assertEqual(len(calls[0][1]["candidates"]), 25)
                        before = len(calls)
                        resumed, resumed_issues = synthesize(
                            members, source, client, "b", "r", Path(folder)
                        )
                        self.assertEqual(len(calls), before)
                        self.assertEqual((resumed, resumed_issues), (records, issues))
                    elif kind == "singletons":
                        self.assertEqual(
                            [len(payload["candidates"]) for _, payload in calls],
                            [12, 12, 1],
                        )
                    else:
                        self.assertGreater(len(records), 1)
                        for _, payload in calls:
                            self.assertTrue(
                                all(
                                    part["text"] == "word " * 450
                                    for part in (
                                        payload["sources"]
                                        if "sources" in payload
                                        else [
                                            p
                                            for c in payload["candidates"]
                                            for p in c["sources"]
                                        ]
                                    )
                                )
                            )

    def test_bounded_batches_and_singletons(self) -> None:
        """达到批次上限时保留单例，并明确披露尚未跨批次合并。"""
        members = [candidate("u1", conditions=["条件"]), candidate("u2")]
        client = FakeClient([])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("book_extractor.synthesis.GROUP_SIZE", 1),
            patch("book_extractor.synthesis.PAYLOAD_TOKENS", 1),
        ):
            records, issues = synthesize(
                members, units(members), client, "book", "run", Path(directory)
            )
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(records), 2)
        self.assertIsNone(records[0].definition)
        self.assertTrue(
            any(
                item["issue"] == "cross_batch_grouping_deferred"
                and item["severity"] == "error"
                for item in issues
            )
        )

    def test_uncertain_does_not_force_merge(self) -> None:
        """不确定分组不强制合并，也不追加调用编造共同释义。"""
        members = [candidate("u1"), candidate("u2")]
        grouping = Grouping(
            groups=[
                {"candidate_ids": [key], "status": "uncertain"} for key in ["u1", "u2"]
            ]
        )
        client = FakeClient(
            [
                grouping,
                accepted_review(
                    evidence_by_candidate={
                        c.candidate_id: c.evidence_ids or c.name_evidence_ids
                        for c in members
                    },
                    findings=[
                        {
                            "support_reason": "测试来源支持范围",
                            "definition_supported": True,
                            "confidence": "high",
                            "name": member.name,
                            "aliases": [],
                            "category": "概念",
                            "definition": "源文解释",
                            "conditions": [],
                            "issues": [],
                            "evidence_ids": member.evidence_ids,
                            "draft_ids": [member.candidate_id],
                        }
                        for member in members
                    ],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            records, issues = synthesize(
                members, units(members), client, "book", "run", Path(directory)
            )
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all("grouping_uncertain" in record.issues for record in records)
        )
        self.assertTrue(all(item["severity"] == "limitation" for item in issues))
        self.assertEqual(
            sum(item["issue"] == "grouping_uncertain" for item in issues), 2
        )

    def test_synthesis_can_reverse_a_wrong_group(self) -> None:
        """综合可撤销错误分组并拆回多个结果，候选不得遗漏或重复。"""
        members = [candidate("u1"), candidate("u2")]
        grouping = Grouping(groups=[{"candidate_ids": ["u1", "u2"], "status": "same"}])
        reviewed = accepted_review(
            evidence_by_candidate={
                c.candidate_id: c.evidence_ids or c.name_evidence_ids for c in members
            },
            findings=[
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": True,
                    "confidence": "high",
                    "name": member.name,
                    "aliases": [],
                    "category": "概念",
                    "definition": "源文解释",
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": member.evidence_ids,
                    "draft_ids": [member.candidate_id],
                }
                for member in members
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            records, _ = synthesize(
                members,
                units(members),
                FakeClient([grouping, reviewed]),
                "book",
                "run",
                Path(directory),
            )
        self.assertEqual([record.candidate_ids for record in records], [["u1"], ["u2"]])
        self.assertEqual([record.evidence_ids for record in records], [["u1"], ["u2"]])

    def test_independent_singletons_generate_in_one_cached_request(self) -> None:
        """不同名称单例合批生成，各自保留来源，恢复不增加请求。"""
        members = [candidate("u1", "力"), candidate("u2", "速度")]
        response = accepted_review(
            evidence_by_candidate={
                c.candidate_id: c.evidence_ids or c.name_evidence_ids for c in members
            },
            findings=[
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": bool(f"{c.name}的来源解释"),
                    "confidence": "high",
                    "name": c.name,
                    "definition": f"{c.name}的来源解释",
                    "category": "概念",
                    "aliases": [],
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": c.evidence_ids,
                    "draft_ids": [c.candidate_id],
                }
                for c in members
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient([response])
            records, errors = synthesize(
                members, units(members), client, "b", "r", Path(folder)
            )
            self.assertEqual(client.calls, 1)
            self.assertTrue(all(r.definition for r in records))
            self.assertFalse(any(e["severity"] == "error" for e in errors))
            payload = json.loads(client.messages[0][-1]["content"])
            self.assertEqual(
                [row["candidate_id"] for row in payload["candidates"]], ["c1", "c2"]
            )
            self.assertNotIn("candidate_groups", payload)
            resumed = FakeClient([])
            self.assertEqual(
                synthesize(members, units(members), resumed, "b", "r", Path(folder))[0],
                records,
            )
            self.assertEqual(resumed.calls, 0)

    def test_body_and_name_spans_remain_bounded_through_verification(self) -> None:
        """名称出处同ID及后置公式重核均不能展开区间外的原文。"""
        prefix, body, suffix = "禁止暴露前缀", "力的符号为$F$。", "禁止暴露后缀"
        member = candidate("u1")
        member.name_evidence_ids = ["u1"]
        member.evidence_spans = [
            EvidenceSpan(unit_id="u1", start=len(prefix), end=len(prefix + body))
        ]
        source = [Unit("u1", prefix + body + suffix, 1, 1, [], "paragraph")]
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=bool(body),
            confidence="medium",
            name="力",
            definition=body,
            category="概念",
            aliases=[],
            conditions=[],
            issues=[],
            evidence_ids=["u1"],
        )
        client = FakeClient(
            [
                accepted_review(
                    evidence_by_candidate={
                        c.candidate_id: c.evidence_ids or c.name_evidence_ids
                        for c in [member]
                    },
                    findings=[{**finding.model_dump(), "draft_ids": ["u1"]}],
                ),
                finding,
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            records, _ = synthesize([member], source, client, "b", "r", Path(folder))
        self.assertEqual(client.calls, 2)
        self.assertEqual(records[0].definition, body)
        for messages in client.messages:
            payload = json.loads(messages[-1]["content"])
            self.assertNotIn(prefix, json.dumps(payload, ensure_ascii=False))
            self.assertNotIn(suffix, json.dumps(payload, ensure_ascii=False))
            parts = payload.get("sources")
            if parts is None:
                parts = payload["candidates"][0]["sources"]
            self.assertEqual(parts[0]["text"], body)
        self.assertEqual(
            json.loads(client.messages[0][-1]["content"])["candidates"][0][
                "name_sources"
            ],
            [],
        )

    def test_name_only_does_not_become_definition(self) -> None:
        """目录名称有身份来源但无正文；非空定义被拒绝并留下可恢复null失败记录。"""
        member = Candidate(
            candidate_id="c",
            chunk_id="toc",
            scope=[],
            name="力",
            evidence_ids=[],
            name_evidence_ids=["u1"],
            origins=["toc"],
        )
        source = [Unit("u1", "力……12", 1, 1, [], "paragraph")]
        invalid = accepted_review(
            evidence_by_candidate={
                c.candidate_id: c.evidence_ids or c.name_evidence_ids for c in [member]
            },
            findings=[
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": True,
                    "confidence": "high",
                    "name": "力",
                    "definition": "力是目录中的一个概念",
                    "category": "概念",
                    "aliases": [],
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": ["u1"],
                    "draft_ids": ["c"],
                }
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                [member], source, FakeClient([invalid]), "b", "r", Path(folder)
            )
        self.assertIsNone(records[0].definition)
        self.assertTrue(any(e["severity"] == "error" for e in errors))
        self.assertEqual(records[0].evidence_ids, ["u1"])

    def test_oversized_evidence_is_not_truncated_or_completed(self) -> None:
        """真实完整请求预算拒绝不发HTTP，保留全部成员与未审核身份，不裁来源。"""
        members = [candidate(f"u{i}") for i in range(8)]
        source = [
            Unit(c.candidate_id, "长正文" * 50, 1, 1, [], "paragraph") for c in members
        ]
        client = LLMClient(
            "https://offline.invalid/v1",
            "offline",
            "offline",
            context_limit=1000,
            max_output_tokens=100,
        )
        try:
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(client, "_create") as network,
            ):
                records, errors = synthesize(
                    members, source, client, "b", "r", Path(folder)
                )
                network.assert_not_called()
            self.assertEqual(client.stats["calls"], 0)
            self.assertEqual(len(records), 8)
            self.assertTrue(all(record.definition is None for record in records))
            self.assertEqual(
                {key for record in records for key in record.candidate_ids},
                {c.candidate_id for c in members},
            )
            self.assertTrue(
                any(
                    e.get("issue") == "synthesis_failed:ContextBudgetError"
                    for e in errors
                )
            )
            decisions = [e for e in errors if e["stage"] == "name_decision"]
            self.assertEqual(len(decisions), 8)
            self.assertTrue(
                all(e["decision"] == "unreviewed" and e["failed"] for e in decisions)
            )
        finally:
            client.close()

    def test_complete_eight_member_collision_ignores_payload_packing_hint(self) -> None:
        """八成员完整组超过打包提示仍不拆身份，保留所有源文供真实请求预算裁决。"""
        members = [candidate(f"u{i}") for i in range(8)]
        source = [
            Unit(c.candidate_id, "正文" * 1500, 1, 1, [], "paragraph") for c in members
        ]
        client = FakeClient(
            [
                Grouping(
                    groups=[
                        {
                            "candidate_ids": [c.candidate_id for c in members],
                            "status": "same",
                        }
                    ]
                ),
                accepted_review(
                    evidence_by_candidate={
                        c.candidate_id: c.evidence_ids for c in members
                    },
                    findings=[
                        {
                            "support_reason": "测试来源支持范围",
                            "definition_supported": False,
                            "confidence": "high",
                            "name": "力",
                            "aliases": [],
                            "category": "概念",
                            "definition": None,
                            "conditions": [],
                            "issues": [],
                            "evidence_ids": [c.candidate_id for c in members],
                            "draft_ids": [c.candidate_id for c in members],
                        }
                    ],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                members, source, client, "b", "r", Path(folder)
            )
        self.assertFalse(any(e["severity"] == "error" for e in errors))
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            set(records[0].candidate_ids), {c.candidate_id for c in members}
        )
        for messages in client.messages:
            payload = json.loads(messages[-1]["content"])
            self.assertEqual(len(payload["candidates"]), 8)
            self.assertEqual(
                [row["text"] for row in payload["sources"]],
                [unit.text for unit in source],
            )

    def test_independent_groups_cannot_merge(self) -> None:
        """不同单例合批只减少请求，不放宽成员边界或借用其他候选证据。"""
        members = [candidate("u1", "力"), candidate("u2", "速度")]
        invalid = accepted_review(
            evidence_by_candidate={
                c.candidate_id: c.evidence_ids or c.name_evidence_ids for c in members
            },
            findings=[
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": True,
                    "confidence": "high",
                    "name": "力与速度",
                    "definition": "不应合并",
                    "category": "概念",
                    "aliases": [],
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": ["u1"],
                    "draft_ids": ["u1", "u2"],
                }
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            records, errors = synthesize(
                members, units(members), FakeClient([invalid]), "b", "r", Path(folder)
            )
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r.definition is None for r in records))
        self.assertTrue(any(e["severity"] == "error" for e in errors))


if __name__ == "__main__":
    unittest.main()
