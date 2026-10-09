"""Retry only unresolved review failures without rewriting source runs."""

import hashlib
import json
import os
from pathlib import Path

from core import load_records
from runner import BASE, js, jl, review_batch


WORK = Path("/home/wangqiyuan/work")
ROOTS = {
    "non_dictionary": WORK / "non_dictionary_12subjects_run_20260923",
    "dictionary": WORK / "single_endpoint_12subjects_run_v2_20260922",
}
OUT = WORK / "review_closeout_20260924"
DICTIONARY_DECISIONS = (
    ROOTS["dictionary"]
    / "closeout_queue_20260923/small_review_jobs/final_decisions.jsonl"
)


def rows_for_stage(subject_dir: Path, stage: str):
    candidates = (
        subject_dir / f"auto_resolved_{stage}/final_reviews.jsonl",
        subject_dir / f"resolved_{stage}_20260923/final_reviews.jsonl",
        subject_dir / stage / "final.jsonl",
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"No review source: {subject_dir}/{stage}")
    return source, load_records(source)


def main():
    if "161925863191374656" not in BASE:
        raise ValueError(f"Wrong endpoint: {BASE}")
    excluded = {
        (row["subject"], row["stage"], row["request_id"])
        for row in load_records(DICTIONARY_DECISIONS)
    }
    if len(excluded) != 21:
        raise ValueError(f"Expected 21 previously decided dictionary rows, got {len(excluded)}")
    OUT.mkdir(exist_ok=True)
    status = {"endpoint": BASE, "results": [], "errors": {}}
    for kind, root in ROOTS.items():
        subjects = sorted(d for d in root.iterdir() if d.is_dir() and (d / "done.json").is_file())
        expected = 11 if kind == "non_dictionary" else 12
        if len(subjects) != expected:
            raise ValueError(f"{kind}: expected {expected} subjects, got {len(subjects)}")
        for subject_dir in subjects:
            for stage in ("audit", "review"):
                key = f"{kind}/{subject_dir.name}/{stage}"
                try:
                    source, rows = rows_for_stage(subject_dir, stage)
                    ids = [r["item"]["request_id"] for r in rows]
                    if len(ids) != len(set(ids)):
                        raise ValueError("duplicate request IDs")
                    pending = [
                        r["item"] for r in rows
                        if r["review"]["judgment"] == "technical_failure"
                        and (subject_dir.name, stage, r["item"]["request_id"]) not in excluded
                    ]
                    dest = OUT / kind / subject_dir.name / stage
                    summary_path = dest / "summary.json"
                    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                    if summary_path.is_file():
                        prior = json.loads(summary_path.read_text())
                        if prior["source_sha256"] != source_hash or prior["pending"] != len(pending):
                            raise ValueError("completed retry source or pending set changed")
                        result = prior
                    else:
                        dest.mkdir(parents=True, exist_ok=True)
                        verdicts = review_batch(pending, dest / "api", chunk_size=80) if pending else {}
                        if set(verdicts) != {r["request_id"] for r in pending}:
                            raise ValueError("retry result ID coverage mismatch")
                        delta = [
                            {"request_id": r["request_id"], "review": verdicts[r["request_id"]]}
                            for r in pending
                        ]
                        jl(dest / "delta.jsonl", delta)
                        result = {
                            "key": key,
                            "source": str(source),
                            "source_sha256": source_hash,
                            "records": len(rows),
                            "pending": len(pending),
                            "resolved": sum(r["review"]["judgment"] != "technical_failure" for r in delta),
                            "remaining_technical": sum(r["review"]["judgment"] == "technical_failure" for r in delta),
                            "excluded_prior_decisions": sum(
                                r["review"]["judgment"] == "technical_failure"
                                and (subject_dir.name, stage, r["item"]["request_id"]) in excluded
                                for r in rows
                            ),
                            "endpoint": BASE,
                        }
                        js(summary_path, result)
                    status["results"].append(result)
                    js(OUT / "status.json", status)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                except Exception as exc:
                    status["errors"][key] = str(exc)
                    js(OUT / "status.json", status)
                    print(json.dumps({"key": key, "error": str(exc)}, ensure_ascii=False), flush=True)
    js(OUT / "status.json", status)


if __name__ == "__main__":
    main()
