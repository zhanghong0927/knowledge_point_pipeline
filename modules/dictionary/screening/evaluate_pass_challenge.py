import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def input_keys(root):
    keys = set()
    for path in (root / "输入清单").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        keys.update((item["subject"], item["file_name"]) for item in data["records"])
    return keys


def as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "是"}


def evaluate(root, human_manifest):
    results = read_csv(root / "全学科审核结果.csv")
    expected = input_keys(root)
    actual = {(row["subject"], row["file_name"]) for row in results}
    if len(actual) != len(results) or actual != expected:
        raise ValueError("challenge result identity coverage failed")
    human_rows = read_csv(human_manifest)
    human = {(row["subject"], row["md_file"]): row for row in human_rows}
    if not expected <= set(human):
        raise ValueError("human manifest does not cover challenge input")

    combined = []
    for row in results:
        label = human[(row["subject"], row["file_name"])]
        combined.append({
            **row,
            "review_id": label["review_id"],
            "identifier": label["identifier"],
            "manifest_title": label["title"],
            "human_retained": label["human_retained"],
        })
    combined.sort(key=lambda row: (row["subject"], row["short_id"]))
    with (root / "PASS反证复核_人工对照结果.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(combined[0]))
        writer.writeheader()
        writer.writerows(combined)

    kept = [row for row in combined if row["human_retained"] == "是"]
    rejected = [row for row in combined if row["human_retained"] == "否"]
    if len(kept) + len(rejected) != len(combined):
        raise ValueError("unknown human label")
    passes = [row for row in combined if row["decision"] == "PASS"]
    initial_passes = [row for row in combined if row["decision_before_pass_challenge"] == "PASS"]
    triggered = [row for row in combined if as_bool(row["pass_challenge_triggered"])]
    demoted = [row for row in combined if as_bool(row["decision_adjusted_by_pass_challenge"])]
    summary = {
        "total": len(combined),
        "coverage_ok": True,
        "human_retained": len(kept),
        "human_rejected": len(rejected),
        "initial_counts": dict(Counter(row["decision_before_pass_challenge"] for row in combined)),
        "final_counts": dict(Counter(row["decision"] for row in combined)),
        "triggered": len(triggered),
        "demoted_to_review": len(demoted),
        "human_retained_demoted": sum(row["human_retained"] == "是" for row in demoted),
        "human_rejected_demoted": sum(row["human_retained"] == "否" for row in demoted),
        "initial_pass_manual_approval_rate": round(
            100 * sum(row["human_retained"] == "是" for row in initial_passes) / len(initial_passes), 2
        ) if initial_passes else None,
        "final_pass_manual_approval_rate": round(
            100 * sum(row["human_retained"] == "是" for row in passes) / len(passes), 2
        ) if passes else None,
        "human_retained_pass_recall": round(
            100 * sum(row["decision"] == "PASS" for row in kept) / len(kept), 2
        ) if kept else None,
        "human_retained_not_drop_rate": round(
            100 * sum(row["decision"] != "DROP" for row in kept) / len(kept), 2
        ) if kept else None,
        "challenge_recommendations": dict(Counter(
            row["pass_challenge_recommendation"] for row in triggered
        )),
        "challenge_payloads": dict(Counter(
            row["pass_challenge_dominant_payload"] for row in triggered
        )),
        "subjects": {
            subject: {
                "total": sum(row["subject"] == subject for row in combined),
                "final_counts": dict(Counter(
                    row["decision"] for row in combined if row["subject"] == subject
                )),
                "human_retained_demoted": sum(
                    row["subject"] == subject and row["human_retained"] == "是"
                    for row in demoted
                ),
                "human_rejected_demoted": sum(
                    row["subject"] == subject and row["human_retained"] == "否"
                    for row in demoted
                ),
            }
            for subject in sorted({row["subject"] for row in combined})
        },
    }
    (root / "PASS反证复核_人工对照摘要.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="PASS 反证复核与人工标签对照")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.root, args.manifest), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
