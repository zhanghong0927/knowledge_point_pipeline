from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


LLM_AUDIT_FIELDS = (
    "important_llm_decision",
    "important_llm_subject_fit",
    "important_llm_knowledge_extraction_fit",
    "important_llm_book_type",
    "important_llm_reason",
)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    csv.field_size_limit(2_147_483_647)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader((line.replace("\x00", "") for line in handle))
        return list(reader.fieldnames or []), list(reader)


def _identifier(row: dict[str, Any]) -> str:
    return str(row.get("identifier") or "").strip()


def _validate_llm_results(
    important_rows: list[dict[str, str]],
    llm_rows: list[dict[str, str]],
) -> dict[int, dict[str, str]]:
    if len(llm_rows) != len(important_rows):
        raise ValueError(
            f"字段模型结果不完整: 重要书籍 {len(important_rows)} 行，模型结果 {len(llm_rows)} 行"
        )
    by_number: dict[int, dict[str, str]] = {}
    for result in llm_rows:
        raw_number = (result.get("source_row_number") or "").strip()
        try:
            row_number = int(raw_number)
        except ValueError as exc:
            raise ValueError(f"无效 source_row_number: {raw_number!r}") from exc
        if row_number in by_number:
            raise ValueError(f"字段模型结果 source_row_number 重复: {row_number}")
        if row_number < 1 or row_number > len(important_rows):
            raise ValueError(f"字段模型结果 source_row_number 越界: {row_number}")
        source_id = _identifier(important_rows[row_number - 1])
        result_id = _identifier(result)
        if source_id and result_id and source_id != result_id:
            raise ValueError(
                f"第 {row_number} 行 identifier 不一致: source={source_id}, result={result_id}"
            )
        decision = (result.get("decision") or "").strip().upper()
        if decision not in {"KEEP", "REVIEW", "DROP"}:
            raise ValueError(f"第 {row_number} 行 decision 无效: {decision!r}")
        result["decision"] = decision
        by_number[row_number] = result
    expected = set(range(1, len(important_rows) + 1))
    missing = sorted(expected - set(by_number))
    if missing:
        raise ValueError(f"字段模型结果不完整，缺少 source_row_number: {missing[:10]}")
    return by_number


def _decorate(row: dict[str, str], track: str, result: dict[str, str] | None = None) -> dict[str, str]:
    output = dict(row)
    output["book_track"] = track
    if result is None:
        for field in LLM_AUDIT_FIELDS:
            output[field] = ""
    else:
        output.update({
            "important_llm_decision": result.get("decision", ""),
            "important_llm_subject_fit": result.get("subject_fit", ""),
            "important_llm_knowledge_extraction_fit": result.get("knowledge_extraction_fit", ""),
            "important_llm_book_type": result.get("book_type", ""),
            "important_llm_reason": result.get("reason", ""),
        })
    return output


def prepare_md_input(
    dictionary_csv: Path,
    important_csv: Path,
    llm_result_csv: Path,
    output_csv: Path,
    summary_json: Path,
) -> dict[str, Any]:
    dictionary_fields, dictionary_rows = _read_csv(dictionary_csv)
    important_fields, important_rows = _read_csv(important_csv)
    _, llm_rows = _read_csv(llm_result_csv)
    results = _validate_llm_results(important_rows, llm_rows)

    output_rows: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicate_dictionary_rows = 0
    cross_track_duplicates = 0
    for row in dictionary_rows:
        identity = _identifier(row)
        if identity and identity in seen:
            duplicate_dictionary_rows += 1
            continue
        output_rows.append(_decorate(row, "辞海类"))
        if identity:
            seen.add(identity)

    important_keep = 0
    for row_number, row in enumerate(important_rows, 1):
        result = results[row_number]
        if result["decision"] != "KEEP":
            continue
        important_keep += 1
        identity = _identifier(row)
        if identity and identity in seen:
            cross_track_duplicates += 1
            continue
        output_rows.append(_decorate(row, "其他重要书籍", result))
        if identity:
            seen.add(identity)

    fields = list(dict.fromkeys([
        *dictionary_fields,
        *important_fields,
        "book_track",
        *LLM_AUDIT_FIELDS,
    ]))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)

    summary: dict[str, Any] = {
        "dictionary_csv": str(dictionary_csv),
        "important_csv": str(important_csv),
        "llm_result_csv": str(llm_result_csv),
        "output_csv": str(output_csv),
        "dictionary_rows": len(dictionary_rows),
        "dictionary_duplicate_rows_removed": duplicate_dictionary_rows,
        "important_source_rows": len(important_rows),
        "important_keep_rows_before_dedup": important_keep,
        "cross_track_duplicates_removed": cross_track_duplicates,
        "output_rows": len(output_rows),
    }
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_smoke_sample(input_csv: Path, output_csv: Path, per_track: int = 5) -> dict[str, int]:
    if per_track < 1:
        raise ValueError("per_track must be positive")
    fields, rows = _read_csv(input_csv)
    selected: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for track in ("辞海类", "其他重要书籍"):
        candidates = sorted(
            (row for row in rows if (row.get("book_track") or "").strip() == track),
            key=lambda row: (_identifier(row), row.get("title", "")),
        )
        picked = candidates[:per_track]
        selected.extend(picked)
        counts[track] = len(picked)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Combine dictionary candidates with metadata-LLM KEEP books for MD audit")
    parser.add_argument("--dictionary-csv", type=Path, required=True)
    parser.add_argument("--important-csv", type=Path, required=True)
    parser.add_argument("--llm-result-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--smoke-output-csv", type=Path)
    parser.add_argument("--smoke-per-track", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = prepare_md_input(
        args.dictionary_csv,
        args.important_csv,
        args.llm_result_csv,
        args.output_csv,
        args.summary_json,
    )
    if args.smoke_output_csv is not None:
        result["smoke_counts"] = write_smoke_sample(
            args.output_csv, args.smoke_output_csv, args.smoke_per_track
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
