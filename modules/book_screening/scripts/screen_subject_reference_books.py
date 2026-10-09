from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


CHINESE_TERM_GROUPS = (
    ("辞海", ("辞海",)),
    ("词典/辞典", ("词典", "辞典", "大词典", "大辞典")),
    ("百科全书", ("百科全书", "大百科全书", "百科")),
    ("图典/辞书", ("图典", "辞书")),
    ("术语/名词/词汇", ("术语", "名词", "词汇")),
)
ENGLISH_TERM_GROUPS = (
    ("词典/辞典", ("dictionary", "lexicon")),
    ("百科全书", ("encyclopedia", "encyclopaedia")),
    ("图典/辞书", ("glossary",)),
    ("术语/名词/词汇", ("terminology", "vocabulary", "nomenclature")),
)
REVIEW_CONTEXT_TERMS = (
    "习题",
    "试题",
    "练习",
    "考试",
    "考研",
    "学习指导",
    "复习",
    "题解",
    "名词解释",
    "翻译研究",
    "术语研究",
    "词典研究",
    "辞典研究",
    "编纂研究",
    "小百科",
    "普及读物",
    "绘本",
    "宝宝",
    "少儿",
    "儿童",
    "青少年",
    "漫画",
    "常识",
    "图鉴",
    "magazine",
    "stories",
)
AUDIT_FIELDS = (
    "screen_decision",
    "reference_type",
    "matched_terms",
    "screen_reason",
    "dedupe_key",
    "duplicate_of_key",
)
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "outputs" / "subject_reference_screening"


@dataclass(frozen=True)
class TitleDecision:
    decision: str
    reference_type: str
    matched_terms: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ScreeningResult:
    strict_rows: list[dict[str, str]]
    review_rows: list[dict[str, str]]
    false_positive_rows: list[dict[str, str]]
    duplicate_rows: list[dict[str, str]]
    no_hit_count: int


def normalize_title_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title or "").casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def safe_subject_name(subject_name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", subject_name).strip(" .")
    return cleaned or "未命名学科"


def _find_terms(title: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for reference_type, terms in CHINESE_TERM_GROUPS:
        for term in terms:
            if term in title:
                hits.append((reference_type, term))
    for reference_type, terms in ENGLISH_TERM_GROUPS:
        for term in terms:
            if re.search(rf"\b{re.escape(term)}\b", title, flags=re.IGNORECASE):
                hits.append((reference_type, term))
    return hits


def classify_title(title: str) -> TitleDecision:
    normalized = unicodedata.normalize("NFKC", title or "").casefold().strip()
    raw_hits = _find_terms(normalized)
    if not raw_hits:
        return TitleDecision("no_hit", "", (), "题名无辞海或工具书类型词。")

    cleaned = normalized.replace("图典型", "")
    cleaned = re.sub(r"\bdictionary\s+learning\b", "", cleaned, flags=re.IGNORECASE)
    effective_hits = _find_terms(cleaned)
    if not effective_hits:
        return TitleDecision(
            "false_positive",
            "",
            tuple(dict.fromkeys(term for _, term in raw_hits)),
            "工具书类型词属于跨词或专门概念误命中。",
        )

    matched_terms = tuple(dict.fromkeys(term for _, term in effective_hits))
    reference_type = effective_hits[0][0]
    if any(term in normalized for term in REVIEW_CONTEXT_TERMS):
        return TitleDecision(
            "review",
            reference_type,
            matched_terms,
            "题名同时具有学习资料或研究对象特征，需人工判断是否为工具书。",
        )

    return TitleDecision(
        "strict_keep",
        reference_type,
        matched_terms,
        "题名明确表明辞海、词典、百科或术语型工具书属性。",
    )


def _annotate_row(row: dict[str, str], decision: TitleDecision) -> dict[str, str]:
    output = dict(row)
    output.update(
        {
            "screen_decision": decision.decision,
            "reference_type": decision.reference_type,
            "matched_terms": " | ".join(decision.matched_terms),
            "screen_reason": decision.reason,
            "dedupe_key": "",
            "duplicate_of_key": "",
        }
    )
    return output


def screen_rows(
    rows: list[dict[str, str]],
    manual_review_overrides: dict[str, str] | None = None,
    *,
    identifier_column: str = "identifier",
    title_column: str = "title",
) -> ScreeningResult:
    manual_review_overrides = manual_review_overrides or {}
    classified: list[tuple[dict[str, str], TitleDecision]] = []
    for row in rows:
        decision = classify_title(row.get(title_column, ""))
        identifier = (row.get(identifier_column) or "").strip()
        if decision.decision == "strict_keep" and identifier in manual_review_overrides:
            decision = TitleDecision(
                decision="review",
                reference_type=decision.reference_type,
                matched_terms=decision.matched_terms,
                reason="人工逐条复核：" + manual_review_overrides[identifier],
            )
        classified.append((row, decision))

    strict_rows: list[dict[str, str]] = []
    review_rows: list[dict[str, str]] = []
    false_positive_rows = [
        _annotate_row(row, decision)
        for row, decision in classified
        if decision.decision == "false_positive"
    ]
    duplicate_rows: list[dict[str, str]] = []
    seen_identifiers: set[str] = set()
    seen_titles: set[str] = set()

    for target_decision, destination in (
        ("strict_keep", strict_rows),
        ("review", review_rows),
    ):
        for row, decision in classified:
            if decision.decision != target_decision:
                continue
            annotated = _annotate_row(row, decision)
            identifier = (row.get(identifier_column) or "").strip()
            title_key = normalize_title_key(row.get(title_column, ""))
            duplicate_of_key = ""
            if identifier and identifier in seen_identifiers:
                duplicate_of_key = f"{identifier_column}:{identifier}"
            elif title_key and title_key in seen_titles:
                duplicate_of_key = f"{title_column}:{title_key}"

            if duplicate_of_key:
                annotated.update(
                    {
                        "screen_decision": "duplicate",
                        "screen_reason": "与优先级更高或更早出现的候选书目重复。",
                        "duplicate_of_key": duplicate_of_key,
                    }
                )
                duplicate_rows.append(annotated)
                continue

            if identifier:
                seen_identifiers.add(identifier)
            if title_key:
                seen_titles.add(title_key)
            annotated["dedupe_key"] = (
                f"{identifier_column}:{identifier}"
                if identifier
                else f"{title_column}:{title_key}"
            )
            destination.append(annotated)

    return ScreeningResult(
        strict_rows=strict_rows,
        review_rows=review_rows,
        false_positive_rows=false_positive_rows,
        duplicate_rows=duplicate_rows,
        no_hit_count=sum(1 for _, decision in classified if decision.decision == "no_hit"),
    )


def write_csv(path: Path, rows: list[dict[str, str]], source_fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [field for field in source_fields if field not in AUDIT_FIELDS] + list(AUDIT_FIELDS)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_screening(
    input_csv: Path,
    output_dir: Path,
    *,
    subject_name: str | None = None,
    identifier_column: str = "identifier",
    title_column: str = "title",
    manual_review_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    with input_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        source_fields = list(reader.fieldnames or [])
        rows = list(reader)

    if title_column not in source_fields:
        raise ValueError(f"输入缺少题名字段：{title_column}")
    if identifier_column not in source_fields:
        raise ValueError(f"输入缺少标识字段：{identifier_column}")

    subject = safe_subject_name(subject_name or input_csv.stem)
    result = screen_rows(
        rows,
        manual_review_overrides=manual_review_overrides,
        identifier_column=identifier_column,
        title_column=title_column,
    )
    all_candidates = [*result.strict_rows, *result.review_rows]
    prefix = f"{subject}_工具书辞海"
    outputs = {
        "all_candidates_csv": output_dir / f"{prefix}_全部候选.csv",
        "strict_csv": output_dir / f"{prefix}_严格保留.csv",
        "review_csv": output_dir / f"{prefix}_待复核.csv",
        "false_positive_csv": output_dir / f"{prefix}_误命中.csv",
        "duplicates_csv": output_dir / f"{prefix}_重复项.csv",
    }
    write_csv(outputs["all_candidates_csv"], all_candidates, source_fields)
    write_csv(outputs["strict_csv"], result.strict_rows, source_fields)
    write_csv(outputs["review_csv"], result.review_rows, source_fields)
    write_csv(outputs["false_positive_csv"], result.false_positive_rows, source_fields)
    write_csv(outputs["duplicates_csv"], result.duplicate_rows, source_fields)

    reference_types = Counter(row["reference_type"] for row in all_candidates)
    matched_terms = Counter(
        term
        for row in all_candidates
        for term in row.get("matched_terms", "").split(" | ")
        if term
    )
    summary: dict[str, object] = {
        "subject_name": subject,
        "input_csv": str(input_csv),
        "input_rows": len(rows),
        "strict_keep_rows": len(result.strict_rows),
        "review_rows": len(result.review_rows),
        "all_candidate_rows": len(all_candidates),
        "false_positive_rows": len(result.false_positive_rows),
        "duplicate_rows": len(result.duplicate_rows),
        "no_hit_rows": result.no_hit_count,
        "reference_type_counts": dict(reference_types),
        "matched_term_counts": dict(matched_terms),
        "outputs": {key: str(value) for key, value in outputs.items()},
    }
    summary_path = output_dir / f"{prefix}_筛选汇总.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_json"] = str(summary_path)
    return summary


def run_batch(
    input_dir: Path,
    output_root: Path,
    *,
    pattern: str = "*.csv",
    identifier_column: str = "identifier",
    title_column: str = "title",
    overrides_by_subject: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    summaries: list[dict[str, object]] = []
    for input_csv in sorted(input_dir.glob(pattern)):
        subject = input_csv.stem
        overrides = (overrides_by_subject or {}).get(subject, {})
        summaries.append(
            run_screening(
                input_csv,
                output_root / safe_subject_name(subject),
                subject_name=subject,
                identifier_column=identifier_column,
                title_column=title_column,
                manual_review_overrides=overrides,
            )
        )

    batch_summary: dict[str, object] = {
        "input_dir": str(input_dir),
        "pattern": pattern,
        "subject_count": len(summaries),
        "input_rows": sum(int(item["input_rows"]) for item in summaries),
        "strict_keep_rows": sum(int(item["strict_keep_rows"]) for item in summaries),
        "review_rows": sum(int(item["review_rows"]) for item in summaries),
        "all_candidate_rows": sum(int(item["all_candidate_rows"]) for item in summaries),
        "subjects": summaries,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "批量筛选汇总.json"
    summary_path.write_text(
        json.dumps(batch_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    batch_summary["summary_json"] = str(summary_path)
    return batch_summary


def load_overrides(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("人工复核配置必须是 JSON 对象。")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通用学科辞海、词典、百科和术语型工具书筛选。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="单个学科 CSV。")
    source.add_argument("--input-dir", type=Path, help="包含多个学科 CSV 的目录。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--subject-name", help="单文件模式下的学科名，默认使用文件名。")
    parser.add_argument("--identifier-column", default="identifier")
    parser.add_argument("--title-column", default="title")
    parser.add_argument("--pattern", default="*.csv", help="批量模式文件匹配规则。")
    parser.add_argument("--overrides", type=Path, help="可选的人工待复核 JSON 配置。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    overrides = load_overrides(args.overrides)
    if args.input_dir:
        summary = run_batch(
            args.input_dir,
            args.output_dir,
            pattern=args.pattern,
            identifier_column=args.identifier_column,
            title_column=args.title_column,
            overrides_by_subject=overrides,  # type: ignore[arg-type]
        )
    else:
        subject = args.subject_name or args.input.stem
        if overrides and all(isinstance(value, dict) for value in overrides.values()):
            single_overrides = overrides.get(subject, {})
        else:
            single_overrides = overrides
        summary = run_screening(
            args.input,
            args.output_dir,
            subject_name=subject,
            identifier_column=args.identifier_column,
            title_column=args.title_column,
            manual_review_overrides=single_overrides,  # type: ignore[arg-type]
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
