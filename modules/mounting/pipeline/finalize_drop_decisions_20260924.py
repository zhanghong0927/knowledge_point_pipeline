"""Finalize persistent technical failures as DROP, preserving all source data."""

import csv
import json
from collections import Counter
from pathlib import Path


WORK = Path("/home/wangqiyuan/work")
ROOTS = {
    "dictionary": WORK / "single_endpoint_12subjects_run_v2_20260922",
    "non_dictionary": WORK / "non_dictionary_12subjects_run_20260923",
}
REVIEW = WORK / "review_closeout_20260924"
OUT = WORK / "endpoint_161925_closeout_20260924"


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def latest_review_source(d, stage):
    for path in (
        d / f"auto_resolved_{stage}/final_reviews.jsonl",
        d / f"resolved_{stage}_20260923/final_reviews.jsonl",
        d / stage / "final.jsonl",
    ):
        if path.is_file():
            return path
    raise FileNotFoundError(f"{d}/{stage}: no source review")


def add(evidence, kind, subject, stage, item, reason, source, **extra):
    evidence.append({
        "set": kind,
        "subject": subject,
        "stage": stage,
        "id": str(item.get("id", "")),
        "request_id": item.get("request_id") or item.get("record_id"),
        "name": item.get("name", ""),
        "knowledge_point": item.get("knowledge_point", ""),
        "decision": "DROP",
        "reason": reason,
        "source": str(source),
        **extra,
    })


def main():
    if OUT.exists():
        raise FileExistsError(f"Refusing to overwrite {OUT}")
    review_status = json.loads((REVIEW / "status.json").read_text())
    if review_status["errors"] or len(review_status["results"]) != 46:
        raise ValueError("review retry is incomplete")
    non_mount_status = json.loads((ROOTS["non_dictionary"] / "auto_resolve_reviews_status.json").read_text())
    dict_mount_status = json.loads((ROOTS["dictionary"] / "post_restart_mount_status_20260924.json").read_text())
    if len(non_mount_status["reports"]) + len(non_mount_status["errors"]) != 11:
        raise ValueError("non-dictionary mount retry is incomplete")
    if len(dict_mount_status["reports"]) + len(dict_mount_status["errors"]) != 3:
        raise ValueError("dictionary mount retry is incomplete")

    existing_decisions = {
        (r["subject"], r["stage"], r["request_id"]): r
        for r in rows(ROOTS["dictionary"] / "closeout_queue_20260923/small_review_jobs/final_decisions.jsonl")
    }
    if len(existing_decisions) != 21:
        raise ValueError("expected 21 pre-existing dictionary review decisions")
    evidence = []
    recovered = Counter()
    for kind, root in ROOTS.items():
        subjects = sorted(d for d in root.iterdir() if d.is_dir() and (d / "done.json").is_file())
        if len(subjects) != (12 if kind == "dictionary" else 11):
            raise ValueError(f"{kind}: subject count changed")
        for d in subjects:
            subject = d.name
            for stage in ("audit", "review"):
                source = latest_review_source(d, stage)
                retry_dir = REVIEW / kind / subject / stage
                summary = json.loads((retry_dir / "summary.json").read_text())
                delta = {r["request_id"]: r["review"] for r in rows(retry_dir / "delta.jsonl")}
                if len(delta) != summary["pending"]:
                    raise ValueError(f"{kind}/{subject}/{stage}: retry delta count mismatch")
                seen = set()
                for row in rows(source):
                    if row["review"]["judgment"] != "technical_failure":
                        continue
                    item = row["item"]
                    rid = item["request_id"]
                    prior = existing_decisions.get((subject, stage, rid)) if kind == "dictionary" else None
                    if prior is not None:
                        if prior["decision"] != "reasonable":
                            add(evidence, kind, subject, stage, item,
                                "prior_final_decision_" + str(prior["decision"]), source)
                        else:
                            recovered[(kind, subject, stage)] += 1
                        continue
                    if rid not in delta:
                        raise ValueError(f"{kind}/{subject}/{stage}/{rid}: missing retry result")
                    seen.add(rid)
                    verdict = delta[rid]
                    if verdict["judgment"] == "technical_failure":
                        add(evidence, kind, subject, stage, item, "technical_failure_after_final_retry", source)
                    elif stage == "review" and verdict["judgment"] != "reasonable":
                        add(evidence, kind, subject, stage, item,
                            "remount_review_" + verdict["judgment"] + "_after_final_retry", source)
                    else:
                        recovered[(kind, subject, stage)] += 1
                if seen != set(delta):
                    raise ValueError(f"{kind}/{subject}/{stage}: retry contains nontechnical IDs")

            old_mount = (
                d / "auto_retry_mount_excerpt/with_cards.jsonl"
                if kind == "dictionary" else d / "remount/with_cards.jsonl"
            )
            if not old_mount.is_file():
                continue
            failed = {
                r["record_id"]: r
                for r in rows(old_mount)
                if r["knowledge_labeling"]["status"] != "ok"
                and r["knowledge_labeling"].get("error")
            }
            if failed:
                new_mount = (
                    d / "post_restart_mount_20260924/with_cards.jsonl"
                    if kind == "dictionary" else d / "auto_retry_mount_excerpt/with_cards.jsonl"
                )
                latest = {r["record_id"]: r for r in rows(new_mount)} if new_mount.is_file() else {}
                if not set(latest) <= set(failed):
                    raise ValueError(f"{kind}/{subject}: mount retry contains unknown IDs")
                for rid, original in failed.items():
                    retry = latest.get(rid)
                    if (
                        retry
                        and retry["knowledge_labeling"]["status"] == "ok"
                        and retry["knowledge_labeling"].get("decision") in {"accepted_parent", "accepted_leaf"}
                    ):
                        recovered[(kind, subject, "mount")] += 1
                    else:
                        row = retry or original
                        err = row["knowledge_labeling"].get("error", "")
                        add(evidence, kind, subject, "mount", row.get("knowledge_card", row),
                            "mount_failure_after_final_retry", old_mount, record_id=rid, error=err)

            review_dirs = [d / "auto_retry_mount_review", d / "post_restart_mount_review_20260924"]
            for review_dir in review_dirs:
                source = review_dir / "final.jsonl"
                if not source.is_file():
                    continue
                for row in rows(source):
                    if row["review"]["judgment"] != "reasonable":
                        add(evidence, kind, subject, "mount_review", row["item"],
                            "mount_review_" + row["review"]["judgment"] + "_after_retry", source)

    OUT.mkdir(parents=True)
    with (OUT / "drop_evidence.jsonl").open("w", encoding="utf-8") as stream:
        for row in evidence:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    grouped = {}
    for row in evidence:
        key = (row["set"], row["subject"], row["id"] or row.get("record_id") or row["request_id"])
        target = grouped.setdefault(key, {
            "set": row["set"], "subject": row["subject"], "id": row["id"],
            "name": row["name"], "knowledge_point": row["knowledge_point"],
            "stages": set(), "reasons": set(),
        })
        target["stages"].add(row["stage"])
        target["reasons"].add(row["reason"])
    with (OUT / "drop_ids.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("set", "subject", "id", "name", "knowledge_point", "stages", "reasons"))
        writer.writeheader()
        for row in sorted(grouped.values(), key=lambda x: (x["set"], x["subject"], x["id"])):
            writer.writerow(dict(row, stages=";".join(sorted(row["stages"])), reasons=";".join(sorted(row["reasons"]))))
    summary = {
        "drop_evidence_rows": len(evidence),
        "unique_drop_records": len(grouped),
        "by_set_subject_stage": {
            "/".join(k): v for k, v in sorted(Counter((r["set"], r["subject"], r["stage"]) for r in evidence).items())
        },
        "recovered_after_retry": {"/".join(k): v for k, v in sorted(recovered.items())},
        "source_delivery_mutated": False,
        "note": "DROP decisions are an audited overlay; source delivery files were not deleted.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
