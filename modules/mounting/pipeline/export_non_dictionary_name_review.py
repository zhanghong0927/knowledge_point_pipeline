"""Export verified non-dictionary name-cleaning REVIEW rows in the earlier six-column format."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path("/home/wangqiyuan/work/full_exact_path_dedup_20260923/name_cleaning")
OUT = Path("/home/wangqiyuan/work/full_exact_path_dedup_20260923/name_cleaning_review_export")
SUBJECTS = [
    ("mechanical_engineering", "机械"), ("architecture", "建筑"),
    ("history", "历史学"), ("literature", "文学"),
    ("philosophy", "哲学"), ("economy", "经济学"),
    ("military", "军事学"), ("art", "艺术学"),
    ("civil_engineering", "土木"), ("management", "管理学"),
    ("sociology", "社会学"), ("education", "教育学"),
]
FIELDS = (("definition", "定义"), ("description", "描述"),
          ("en_definition", "英文定义"), ("en_description", "英文描述"))
COLUMNS = ("subject", "id", "name", "knowledge_point", "reason", "definition_explanation")


def explanation(record: dict) -> str:
    return "\n".join(
        f"{label}：{value}"
        for key, label in FIELDS
        if isinstance((value := record.get(key)), str) and value.strip()
    )


def export(*, require_all: bool) -> dict:
    completed = [(key, zh) for key, zh in SUBJECTS if (ROOT / key / "verified.json").is_file()]
    missing = [key for key, _ in SUBJECTS if not (ROOT / key / "verified.json").is_file()]
    if require_all and missing:
        raise RuntimeError(f"Not all subjects are verified: {missing}")
    if not completed:
        raise RuntimeError("No verified subjects")
    OUT.mkdir(parents=True, exist_ok=True)
    suffix = "全12学科" if not missing else f"已完成{len(completed)}学科"
    jsonl = OUT / f"非辞海词条名清洗_待复核_{suffix}.jsonl"
    csv_path = OUT / f"非辞海词条名清洗_待复核_{suffix}.csv"
    count = Counter()
    with jsonl.open("w", encoding="utf-8") as out_json, csv_path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as out_csv:
        writer = csv.DictWriter(out_csv, fieldnames=COLUMNS)
        writer.writeheader()
        for subject, zh in completed:
            base = ROOT / subject
            verified = json.loads((base / "verified.json").read_text(encoding="utf-8"))
            expected = verified["counts"]["review"]
            source = base / "llm_name_title_format_review_full.jsonl"
            with source.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    quality = record["llm_name_title_format_quality"]
                    if quality["decision"] != "review":
                        raise ValueError(f"Wrong decision: {subject} {record.get('id')}")
                    row = {
                        "subject": zh,
                        "id": str(record["id"]),
                        "name": record.get("name", ""),
                        "knowledge_point": record.get("knowledge_point", ""),
                        "reason": quality.get("reason", ""),
                        "definition_explanation": explanation(record),
                    }
                    out_json.write(json.dumps(row, ensure_ascii=False) + "\n")
                    writer.writerow(row)
                    count[subject] += 1
            if count[subject] != expected:
                raise ValueError(f"Count mismatch: {subject}: {count[subject]} != {expected}")
    report = {
        "subjects_complete": len(completed),
        "missing_subjects": missing,
        "rows": sum(count.values()),
        "by_subject": dict(count),
        "jsonl": str(jsonl),
        "csv": str(csv_path),
        "jsonl_sha256": hashlib.sha256(jsonl.read_bytes()).hexdigest(),
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    }
    (OUT / f"report_{suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-all", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(require_all=args.require_all), ensure_ascii=False))


if __name__ == "__main__":
    main()
