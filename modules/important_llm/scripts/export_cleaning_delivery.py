"""按书冻结已完成逐项抽取的知识点，输出JSONL和来源索引供下游清洗。"""

import argparse
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from book_extractor.models import Record

INCOMPLETE_CODES = {
    "quality_unverified",
    "name_unreviewed",
    "context_incomplete",
    "context_extension_failed",
    "cross_batch_grouping_deferred",
    "synthesis_context_oversized",
}


def exclusion_reasons(
    record: dict[str, Any], errors: list[dict[str, Any]]
) -> list[str]:
    """根据记录和已关联的流程错误返回排除原因；不把整书partial扩散到全部记录。"""
    reasons = set()
    if (
        not isinstance(record.get("definition"), str)
        or not record["definition"].strip()
    ):
        reasons.add("definition_missing")
    for issue in record.get("issues", []):
        code = issue.split(":", 1)[0].strip()
        if code in INCOMPLETE_CODES or code.endswith(
            ("_failed", "_unreviewed", "_deferred")
        ):
            reasons.add(code)
    candidates = set(record.get("candidate_ids", []))
    for error in errors:
        if error.get("severity") == "limitation":
            continue
        related = error.get("record_id") == record["record_id"]
        related |= bool(candidates.intersection(error.get("candidate_ids", [])))
        chunk = error.get("chunk_id")
        if chunk:
            related |= any(key.startswith(chunk + "-") for key in candidates)
        if related:
            reasons.add(
                "linked_pipeline_error:"
                + error.get("issue", error.get("error", "unknown"))
            )
    return sorted(reasons)


def export(root: Path, output: Path) -> dict[str, Any]:
    """从停机的root冻结记录到全新output目录，返回计数；遇到来源或身份异常即中止。"""
    output.mkdir(parents=True, exist_ok=False)
    formal = json.loads((root / "inputs/manifest.json").read_text())
    books = {book["book_id"]: book for book in formal["books"]}
    paths = sorted((root / "runs").glob("*/manifest.json"))
    assert len(paths) == len(books)
    counts = Counter()
    reasons = Counter()
    rows = []
    identities = set()
    with (output / "excluded.jsonl").open("x", encoding="utf-8") as excluded:
        for path in paths:
            manifest = json.loads(path.read_text())
            assert manifest["status"] in {"complete", "partial"}
            assert not (path.parent / ".running").exists()
            metadata = books[manifest["book_id"]]
            units = json.loads((path.parent / "units.json").read_text())
            unit_ids = {unit["id"] for unit in units}
            assert len(unit_ids) == len(units)
            assert (
                hashlib.sha256(
                    "".join(unit["text"] for unit in units).encode()
                ).hexdigest()
                == manifest["text_sha256"]
            )
            accepted = 0
            accepted_lines = []
            total = 0
            with (path.parent / "records.jsonl").open(encoding="utf-8") as records:
                for line in records:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    Record.model_validate(raw, context={"evidence_ids": unit_ids})
                    assert raw["run_id"] == manifest["run_id"]
                    assert raw["book_id"] == manifest["book_id"]
                    identity = (raw["run_id"], raw["record_id"])
                    assert identity not in identities
                    identities.add(identity)
                    total += 1
                    blocked = exclusion_reasons(raw, manifest.get("errors", []))
                    if blocked:
                        reasons.update(blocked)
                        excluded.write(
                            json.dumps(
                                {
                                    "book_id": raw["book_id"],
                                    "run_id": raw["run_id"],
                                    "record_id": raw["record_id"],
                                    "reasons": blocked,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        continue
                    accepted += 1
                    provenance = {
                        "title": manifest["title"],
                        "subject": metadata.get("subject", ""),
                        "language": metadata.get("language", ""),
                        "model": manifest["config"]["model"],
                    }
                    accepted_lines.append(
                        json.dumps(
                            {**raw, "provenance": provenance}, ensure_ascii=False
                        )
                        + "\n"
                    )
            assert total == manifest["records"]
            counts.update(input=total, accepted=accepted, excluded=total - accepted)
            rows.append(
                {
                    "book_id": manifest["book_id"],
                    "run_id": manifest["run_id"],
                    "title": manifest["title"],
                    "subject": metadata.get("subject", ""),
                    "language": metadata.get("language", ""),
                    "model": manifest["config"]["model"],
                    "book_status": manifest["status"],
                    "input_records": total,
                    "accepted_records": accepted,
                    "excluded_records": total - accepted,
                    "source": manifest["source"],
                    "source_sha256": manifest["source_sha256"],
                    "records": f"books/{manifest['book_id']}/records.jsonl"
                    if accepted
                    else None,
                    "units": f"books/{manifest['book_id']}/units.json"
                    if accepted
                    else None,
                }
            )
            if accepted:
                target = output / "books" / manifest["book_id"]
                assert target.resolve().parent == (output / "books").resolve()
                target.mkdir(parents=True)
                (target / "records.jsonl").write_text(
                    "".join(accepted_lines), encoding="utf-8"
                )
                shutil.copy2(path.parent / "units.json", target / "units.json")
                shutil.copy2(path, target / "manifest.json")
            assert json.loads(path.read_text()) == manifest, (
                "Source manifest changed during export"
            )
    assert counts["input"] == counts["accepted"] + counts["excluded"]
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": dict(counts),
        "books": len(rows),
        "books_with_accepted_records": sum(row["accepted_records"] > 0 for row in rows),
        "exclusion_reasons_nonexclusive": dict(reasons),
        "quality_note": "流程完整性筛选，不代表语义验收；保留质量提示交下游清洗。",
    }
    for name, value in (("summary.json", summary), ("books.json", rows)):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (output / "README.md").write_text(
        "# 逐书知识点清洗交付\n\n"
        f"冻结时间：{summary['created_at']}（UTC）。输入{counts['input']}条，"
        f"交付{counts['accepted']}条，排除{counts['excluded']}条。\n\n"
        "- books/<book_id>/records.jsonl：每本书独立的清洗输入，"
        "含完整字段及书籍信息。\n"
        "- books.json：逐书数量、来源路径、SHA256和模型。\n"
        "- books/<book_id>/units.json：冻结的原文单元；evidence_ids可直接回查。\n"
        "- excluded.jsonl：被排除的记录ID和原因，未混入交付记录。\n\n"
        "筛选要求：有非空释义、记录Schema及来源ID合法、书籍运行已结束；"
        "排除调用失败、未复核、上下文未完成、综合超预算、跨批分组延后等记录，"
        "并通过manifest的record_id、candidate_ids、chunk_id排除关联流程错误。"
        "整书partial不导致其中已完成记录全部丢弃。\n\n"
        "本目录不是语义质检通过集。name_uncertain、grouping_uncertain、来源截断及"
        "模型质量说明均保留供清洗；不主动翻译、修改释义、去重或跨书合并。"
        "流程仅对命中风险条件的记录额外复核，不能理解为每条都经历第二次LLM复核。"
        "这是重跑前的固定快照，后续结果不会覆盖它。\n",
        encoding="utf-8",
    )
    checksums = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    (output / "checksums.json").write_text(
        json.dumps(checksums, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    """读取输入任务和输出交付目录参数，执行一次不可覆盖的冻结导出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            export(args.root.resolve(), args.output.resolve()), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
