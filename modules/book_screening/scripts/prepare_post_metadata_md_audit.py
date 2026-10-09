from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


DECISIONS = {"KEEP", "REVIEW", "DROP"}
LLM_FIELDS = (
    "metadata_llm_decision",
    "metadata_llm_subject_fit",
    "metadata_llm_knowledge_extraction_fit",
    "metadata_llm_book_type",
    "metadata_llm_reason",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    csv.field_size_limit(2_147_483_647)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader((line.replace("\x00", "") for line in handle))
        fields = list(reader.fieldnames or [])
        return fields, [dict(row) for row in reader]


def _stable_key(row: dict[str, str], fallback_index: int) -> str:
    identifier = _text(row.get("identifier"))
    if identifier:
        return f"id:{identifier}"
    parsed_path = _text(row.get("parsed_path") or row.get("parsed_file_path"))
    if parsed_path:
        return f"parsed:{parsed_path}"
    return f"row:{fallback_index}:{_text(row.get('title'))}"


def _validate_decisions(
    important_rows: list[dict[str, str]], decision_rows: list[dict[str, str]]
) -> dict[int, dict[str, str]]:
    by_source_row: dict[int, dict[str, str]] = {}
    for result in decision_rows:
        raw_number = _text(result.get("source_row_number"))
        try:
            source_row_number = int(raw_number)
        except ValueError as exc:
            raise ValueError(f"无效 source_row_number: {raw_number!r}") from exc
        if source_row_number in by_source_row:
            raise ValueError(f"字段精筛结果包含重复 source_row_number: {source_row_number}")
        decision = _text(result.get("decision")).upper()
        if decision not in DECISIONS:
            raise ValueError(f"字段精筛结果包含无效 decision: {decision!r}")
        normalized = dict(result)
        normalized["decision"] = decision
        by_source_row[source_row_number] = normalized

    expected = set(range(1, len(important_rows) + 1))
    actual = set(by_source_row)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            "字段精筛结果不完整或与输入不一致；"
            f"missing={missing[:10]}, extra={extra[:10]}, "
            f"expected={len(expected)}, actual={len(actual)}"
        )

    for source_row_number, source in enumerate(important_rows, 1):
        result = by_source_row[source_row_number]
        source_identifier = _text(source.get("identifier"))
        result_identifier = _text(result.get("identifier"))
        if source_identifier and result_identifier and source_identifier != result_identifier:
            raise ValueError(
                f"第 {source_row_number} 行 identifier 不一致: "
                f"source={source_identifier!r}, result={result_identifier!r}"
            )
    return by_source_row


def _annotate_important(
    row: dict[str, str], decision: dict[str, str]
) -> dict[str, str]:
    output = dict(row)
    output["book_track"] = "其他重要书籍"
    output["metadata_llm_decision"] = decision["decision"]
    output["metadata_llm_subject_fit"] = _text(decision.get("subject_fit"))
    output["metadata_llm_knowledge_extraction_fit"] = _text(
        decision.get("knowledge_extraction_fit")
    )
    output["metadata_llm_book_type"] = _text(decision.get("book_type"))
    output["metadata_llm_reason"] = _text(decision.get("reason"))
    return output


def prepare_md_audit_input(
    dictionary_csv: Path,
    important_csv: Path,
    metadata_results_csv: Path,
    output_csv: Path,
    summary_json: Path | None = None,
) -> dict[str, Any]:
    dictionary_fields, dictionary_rows = _read_csv(dictionary_csv)
    important_fields, important_rows = _read_csv(important_csv)
    _, decision_rows = _read_csv(metadata_results_csv)
    decisions = _validate_decisions(important_rows, decision_rows)

    decision_counts = Counter(result["decision"] for result in decisions.values())
    combined: list[dict[str, str]] = []
    seen: set[str] = set()
    dictionary_keys: set[str] = set()
    dictionary_duplicates = 0
    for index, source in enumerate(dictionary_rows, 1):
        row = dict(source)
        row["book_track"] = "辞海类"
        key = _stable_key(row, index)
        if key in seen:
            dictionary_duplicates += 1
            continue
        seen.add(key)
        dictionary_keys.add(key)
        combined.append(row)

    important_keep_rows = 0
    cross_track_duplicates = 0
    important_duplicates = 0
    for source_row_number, source in enumerate(important_rows, 1):
        decision = decisions[source_row_number]
        if decision["decision"] != "KEEP":
            continue
        important_keep_rows += 1
        row = _annotate_important(source, decision)
        key = _stable_key(row, source_row_number)
        if key in seen:
            if key in dictionary_keys:
                cross_track_duplicates += 1
            else:
                important_duplicates += 1
            continue
        seen.add(key)
        combined.append(row)

    output_fields = list(dictionary_fields)
    for field in important_fields:
        if field not in output_fields:
            output_fields.append(field)
    if "book_track" not in output_fields:
        output_fields.append("book_track")
    for field in LLM_FIELDS:
        if field not in output_fields:
            output_fields.append(field)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(combined)

    summary: dict[str, Any] = {
        "dictionary_csv": str(dictionary_csv),
        "important_csv": str(important_csv),
        "metadata_results_csv": str(metadata_results_csv),
        "output_csv": str(output_csv),
        "dictionary_rows": len(dictionary_rows),
        "dictionary_duplicates_removed": dictionary_duplicates,
        "important_rows": len(important_rows),
        "metadata_decision_counts": {
            decision: decision_counts.get(decision, 0)
            for decision in ("KEEP", "REVIEW", "DROP")
        },
        "important_keep_rows_before_dedup": important_keep_rows,
        "cross_track_duplicates_removed": cross_track_duplicates,
        "important_duplicates_removed": important_duplicates,
        "output_rows": len(combined),
    }
    if summary_json is not None:
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="合并辞海候选与字段大模型 KEEP 重要书籍，生成 MD 全量审核输入"
    )
    parser.add_argument("--dictionary-csv", type=Path, required=True)
    parser.add_argument("--important-csv", type=Path, required=True)
    parser.add_argument("--metadata-results-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = prepare_md_audit_input(
        args.dictionary_csv,
        args.important_csv,
        args.metadata_results_csv,
        args.output_csv,
        args.summary_json,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
