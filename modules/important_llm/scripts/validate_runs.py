"""独立校验保存的来源覆盖、候选引用、记录身份和完成状态。

该校验只证明结构一致性，不代表知识点的语义质量已经通过审核。"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from book_extractor.markdown import Unit
from book_extractor.models import Candidate, Grouping, NameDecision, Record
from book_extractor.synthesis import _member_texts, _payload


def read_lines(path: Path) -> list[dict[str, Any]]:
    """读取 path 的 JSONL 并返回非空行的对象列表。

    损坏或截断的 JSON 行直接报错，不静默跳过。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def audit_group(
    row: dict[str, Any],
    directory: Path,
    lookup: dict[str, Candidate],
    cache: dict[str, Grouping],
) -> list[str]:
    """回读分组缓存确认名称审计的组归属；未审核或无缓存只允许原单例。"""
    key = row["grouping_cache_key"]
    members = row["group_candidate_ids"]
    candidate_id = row["candidate_id"]
    assert isinstance(members, list) and members, "empty audit group"
    assert candidate_id in members and len(members) == len(set(members)), (
        "invalid audit group"
    )
    assert set(members) <= set(lookup), "unknown audit group member"
    if row["decision"] == "unreviewed" or key is None:
        assert key is None and members == [candidate_id], "unconfirmed audit group"
        return members
    assert isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key), (
        "invalid grouping cache key"
    )
    if key not in cache:
        path = directory / "synthesis" / f"grouping-{key}.json"
        cache[key] = Grouping.model_validate_json(path.read_text(encoding="utf-8"))
    grouping = cache[key]
    grouped = {member for group in grouping.groups for member in group.candidate_ids}
    assert grouped <= set(lookup), "grouping cache contains unknown candidates"
    matching = [
        group for group in grouping.groups if candidate_id in group.candidate_ids
    ]
    assert len(matching) == 1, "candidate missing from grouping cache"
    group = matching[0]
    assert members == group.candidate_ids, "audit group differs from grouping cache"
    assert group.status == "same" or len(members) == 1, "uncertain group cannot expand"
    return members


def validate(directory: Path) -> dict[str, Any]:
    """校验 directory 的来源、候选覆盖、引用和运行状态，返回结构摘要。

    读取已有产物，不调用模型；回拼、身份或完成性冲突会触发断言或校验异常。"""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    units = json.loads((directory / "units.json").read_text(encoding="utf-8"))
    text = (directory / "source.md").read_bytes().decode("utf-8")
    assert "".join(u["text"] for u in units) == text, "source unit round-trip"
    assert hashlib.sha256(text.encode()).hexdigest() == manifest["text_sha256"], (
        "source hash"
    )
    ids = {unit["id"] for unit in units}
    assert len(ids) == len(units), "duplicate unit IDs"
    candidate_context = {
        "evidence_ids": ids,
        "evidence_texts": {unit["id"]: unit["text"] for unit in units},
    }
    candidates = [
        Candidate.model_validate(row, context=candidate_context)
        for row in read_lines(directory / "candidates.jsonl")
    ]
    records = [
        Record.model_validate(row, context={"evidence_ids": ids})
        for row in read_lines(directory / "records.jsonl")
    ]
    candidate_ids = {c.candidate_id for c in candidates}
    assert len(candidate_ids) == len(candidates), "duplicate candidate IDs"
    lookup = {candidate.candidate_id: candidate for candidate in candidates}
    source = {unit["id"]: Unit(**unit) for unit in units}
    audits = read_lines(directory / "name_decisions.jsonl")
    audit_ids = [row["candidate_id"] for row in audits]
    assert set(audit_ids) == candidate_ids and len(audit_ids) == len(set(audit_ids)), (
        "name audit coverage"
    )
    rejected = set()
    unreviewed = set()
    uncertain = set()
    grouping_cache: dict[str, Grouping] = {}
    for row in audits:
        candidate = lookup[row["candidate_id"]]
        assert set(row) == {
            "candidate_id",
            "name",
            "decision",
            "reason",
            "evidence_ids",
            "failed",
            "group_candidate_ids",
            "grouping_cache_key",
        }, "unexpected audit fields"
        assert row["name"] == candidate.name, "audit candidate name mismatch"
        group = audit_group(row, directory, lookup, grouping_cache)
        allowed = {
            key
            for member in group
            for key in [*lookup[member].evidence_ids, *lookup[member].name_evidence_ids]
        }
        if row["decision"] == "unreviewed":
            assert (
                row["failed"] is True
                and isinstance(row["reason"], str)
                and row["reason"].strip()
            ), "unreviewed status"
            assert row["evidence_ids"] and set(row["evidence_ids"]) <= allowed, (
                "unreviewed source attribution"
            )
            unreviewed.add(candidate.candidate_id)
        else:
            assert row["failed"] is False, "reviewed decision cannot be marked failed"
            NameDecision.model_validate(
                {key: row[key] for key in NameDecision.model_fields},
                context={"evidence_ids": allowed},
            )
            if row["decision"] == "reject":
                rejected.add(candidate.candidate_id)
            if row["decision"] == "uncertain":
                uncertain.add(candidate.candidate_id)
    members = [member for record in records for member in record.candidate_ids]
    assert not set(members) & rejected, "rejected candidate reappears in records"
    assert set(members) | rejected == candidate_ids and len(members) == len(
        set(members)
    ), "candidate loss or duplication"
    for record in records:
        allowed = {
            evidence_id
            for candidate_id in record.candidate_ids
            for evidence_id in (
                lookup[candidate_id].evidence_ids
                + lookup[candidate_id].name_evidence_ids
            )
        }
        assert set(record.evidence_ids) <= allowed, (
            "record borrows another candidate source"
        )
        # 按候选实际读取切片重验，不能用全书正文替错挂别名或翻译提供支持。
        parts = _payload([lookup[key] for key in record.candidate_ids], source)
        texts = _member_texts(
            parts["sources"]
            + (parts["name_sources"] if record.definition is None else []),
            set(record.candidate_ids),
        )
        Record.model_validate(
            record.model_dump(),
            context={
                "evidence_ids": set(texts),
                "evidence_texts": {
                    key: texts[key] for key in record.evidence_ids if key in texts
                },
            },
        )
        if set(record.candidate_ids) & uncertain:
            assert "name_uncertain" in record.issues, "uncertain identity marker lost"
        if set(record.candidate_ids) & unreviewed:
            assert record.definition is None and "name_unreviewed" in record.issues, (
                "unreviewed identity disguised as success"
            )
    counts = {
        label: sum(row["decision"] == decision for row in audits)
        for label, decision in [
            ("accepted", "accept"),
            ("rejected", "reject"),
            ("uncertain", "uncertain"),
            ("unreviewed", "unreviewed"),
        ]
    }
    counts["failed"] = sum(row["failed"] for row in audits)
    assert manifest["name_decision_statistics"] == counts, (
        "name decision statistics mismatch"
    )
    if unreviewed:
        assert manifest["status"] == "partial", (
            "unreviewed names require partial status"
        )
        failed_members = {
            key
            for error in manifest["errors"]
            if error.get("severity") == "error"
            for key in error.get("candidate_ids", [])
        }
        assert unreviewed <= failed_members, (
            "unreviewed identity without explicit failure"
        )
    assert len({r.record_id for r in records}) == len(records), "duplicate record IDs"
    assert all(
        r.book_id == manifest["book_id"] and r.run_id == manifest["run_id"]
        for r in records
    ), "wrong book/run"
    chunks = json.loads((directory / "chunks.json").read_text(encoding="utf-8"))
    unfinished = []
    for chunk in chunks:
        checkpoint = directory / "discovery" / (chunk["id"] + ".json")
        if not checkpoint.exists() or not json.loads(
            checkpoint.read_text(encoding="utf-8")
        ).get("complete"):
            unfinished.append(chunk["id"])
    if manifest["status"] == "complete":
        assert not unfinished, "complete run has uncovered chunks"
        assert not any(e.get("severity") != "limitation" for e in manifest["errors"]), (
            "complete run hides errors"
        )
    return {
        "run_id": manifest["run_id"],
        "title": manifest["title"],
        "status": manifest["status"],
        "chunks": len(chunks),
        "candidates": len(candidates),
        "records": len(records),
        "name_decisions": counts,
        "unfinished_chunks": unfinished,
        "merged_records": sum(len(r.candidate_ids) > 1 for r in records),
        "missing_definitions": sum(r.definition is None for r in records),
        "references_valid": True,
    }


def main() -> None:
    """读取运行目录参数，逐书验证并写 validation.json，返回 None。

    输出只表明结构完整性，不代表知识点语义正确。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    manifests = sorted(args.directory.glob("*/manifest.json"))
    if not manifests:
        parser.error("No run manifests found")
    # 从运行清单枚举，避免尚未产出records的失败书被静默漏掉而显示验证成功。
    results = [validate(path.parent) for path in manifests]
    output = {
        "books": results,
        "count": len(results),
        "note": "Structural integrity, not semantic accuracy.",
    }
    (args.directory / "validation.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"books": len(results), "records": sum(r["records"] for r in results)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
