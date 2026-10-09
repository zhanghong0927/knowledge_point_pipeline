"""Retry only dictionary remount API failures in an immutable run."""

import hashlib
import json
from pathlib import Path

from core import load_records
from runner import BASE, complete_boundary_dir, js, mount, review_batch
from ab_review import prepare_reviews
from retry_technical_alt import mapping_excerpt_items


ROOT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922")
SUBJECTS = ("civil_engineering", "education", "sociology")


def source_failures(subject_dir: Path):
    source = subject_dir / "auto_retry_mount_excerpt/with_cards.jsonl"
    rows = load_records(source)
    failed = {
        row["record_id"]
        for row in rows
        if row["knowledge_labeling"]["status"] != "ok"
        and "API request failed" in str(row["knowledge_labeling"].get("error", ""))
    }
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = load_records(subject_dir / "remount/input.jsonl")
    items = [row for row in original if row["record_id"] in failed]
    if len(items) != len(failed):
        raise ValueError(f"{subject_dir.name}: remount input does not cover all failures")
    return source, source_hash, items


def run_subject(subject: str):
    d = ROOT / subject
    source, source_hash, items = source_failures(d)
    dest = d / "post_restart_mount_20260924"
    summary_path = dest / "summary.json"
    if summary_path.is_file():
        report = json.loads(summary_path.read_text())
        if report["source_sha256"] != source_hash or report["input_records"] != len(items):
            raise ValueError(f"{subject}: completed retry source changed")
        return report
    cards_dir = complete_boundary_dir(d)
    if cards_dir is None:
        raise ValueError(f"{subject}: no complete semantic cards")
    tree = json.loads((cards_dir / "knowledge_tree.json").read_text())
    cards = load_records(cards_dir / "semantic_cards.jsonl")
    mounted = mount(mapping_excerpt_items(items), dest, tree, cards, d)
    if {r["record_id"] for r in mounted} != {r["record_id"] for r in items}:
        raise ValueError(f"{subject}: mapping result ID coverage mismatch")
    checks, _ = prepare_reviews(items, {"with_cards": mounted}, tree)
    reviewed = review_batch(checks, d / "post_restart_mount_review_20260924", chunk_size=80) if checks else {}
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError(f"{subject}: retry source changed during run")
    report = {
        "subject": subject,
        "endpoint": BASE,
        "source": str(source),
        "source_sha256": source_hash,
        "input_records": len(items),
        "remaining_failed": sum(r["knowledge_labeling"]["status"] != "ok" for r in mounted),
        "reviewed": len(checks),
        "review_technical": sum(x["judgment"] == "technical_failure" for x in reviewed.values()),
        "output": str(dest),
    }
    js(summary_path, report)
    return report


def main():
    if "161925863191374656" not in BASE:
        raise ValueError(f"Wrong endpoint: {BASE}")
    status = {"endpoint": BASE, "reports": [], "errors": {}}
    for subject in SUBJECTS:
        try:
            report = run_subject(subject)
            status["reports"].append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        except Exception as exc:
            status["errors"][subject] = str(exc)
            print(json.dumps({"subject": subject, "error": str(exc)}, ensure_ascii=False), flush=True)
        js(ROOT / "post_restart_mount_status_20260924.json", status)


if __name__ == "__main__":
    main()
