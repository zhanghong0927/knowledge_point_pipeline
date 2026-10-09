"""为发现编排和综合测试显式提供已接受名称的响应，不改变生产校验默认值。"""

from pathlib import Path
from typing import Any

from book_extractor.models import (
    Candidate,
    Finding,
    IndependentSynthesis,
    Record,
    SynthesisReview,
)
from book_extractor.synthesis import _record


def accepted_review(
    *, findings: list[dict[str, Any]], evidence_by_candidate: dict[str, list[str]]
) -> SynthesisReview:
    """为测试中已确认的每个成员明确给出接受理由和来源。"""
    return SynthesisReview(
        name_decisions=[
            {
                "candidate_id": key,
                "decision": "accept",
                "reason": "测试源文已命名",
                "evidence_ids": evidence_by_candidate[key],
            }
            for finding in findings
            for key in finding["draft_ids"]
        ],
        findings=findings,
    )


def independent_review(
    review: SynthesisReview, context: dict[str, Any]
) -> IndependentSynthesis:
    """把测试预设的原ID响应转换成实际独立候选传输结构，不代替生产校验。"""
    mapping = {original: key for key, original in context["independent_ids"].items()}
    findings = {}
    for finding in review.findings:
        if len(finding.draft_ids) != 1:
            raise ValueError("独立候选的测试响应不能跨成员合并")
        findings[finding.draft_ids[0]] = finding.model_dump(exclude={"draft_ids"})
    return IndependentSynthesis(
        items=[
            {
                **decision.model_dump(),
                "candidate_id": mapping[decision.candidate_id],
                "finding": findings.get(decision.candidate_id),
            }
            for decision in review.name_decisions
        ]
    )


def independent_from_payload(payload: dict[str, Any]) -> IndependentSynthesis:
    """为独立候选测试生成无释义响应；使用实际收到的短编号和候选自身来源。"""
    return IndependentSynthesis(
        items=[
            {
                "candidate_id": row["candidate_id"],
                "reason": "测试源文已命名",
                "decision": "accept",
                "evidence_ids": [part["id"] for part in row["sources"]],
                "finding": {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": False,
                    "confidence": "high",
                    "name": row["name"],
                    "aliases": [],
                    "category": "概念",
                    "definition": None,
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": [part["id"] for part in row["sources"]],
                },
            }
            for row in payload["candidates"]
        ]
    )


def accepted_work(
    candidates: list[Candidate],
    units: Any,
    client: Any,
    book_id: str,
    run_id: str,
    directory: Path,
    **kwargs: Any,
) -> tuple[list[Record], list[dict]]:
    """隔离发现阶段测试：明确接受各候选并产生null记录及对应审计。"""
    records = []
    audits = []
    for candidate in candidates:
        ids = candidate.evidence_ids or candidate.name_evidence_ids
        finding = Finding(
            support_reason="测试来源支持范围",
            definition_supported=False,
            confidence="high",
            name=candidate.name,
            definition=None,
            category="概念",
            aliases=[],
            conditions=[],
            issues=[],
            evidence_ids=ids,
        )
        records.append(_record(finding, [candidate], book_id, run_id, []))
        audits.append(
            {
                "stage": "name_decision",
                "severity": "limitation",
                "issue": "name_decision",
                "candidate_id": candidate.candidate_id,
                "name": candidate.name,
                "decision": "accept",
                "reason": "测试源文已命名",
                "evidence_ids": ids,
                "failed": False,
            }
        )
    return records, audits
