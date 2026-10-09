#!/usr/bin/env python3
"""High-recall deterministic cleanup before model-based knowledge-point review.

The rule stage removes only records that are unambiguously unusable. Ambiguous
names, broad concepts, uncommon terminology, abbreviations, and records without
an explicit definition are retained and annotated for the model stage.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, TextIO

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None  # type: ignore[assignment,misc]


RULE_VERSION = "books_two_stage_rule_v3_numbered_layout_20261008"
_T2S = OpenCC("t2s") if OpenCC is not None else None

CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
TECH_CHAR_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
URL_RE = re.compile(r"(?:https?://|www\.|ftp://|\b[a-z0-9.-]+\.(?:com|org|net|cn|edu)\b)", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
# Match actual HTML-like tags without deleting domain commands such as <偏移>.
HTML_RE = re.compile(r"</?[A-Za-z][^>]*>")
IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]*\)")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)]\([^)]*\)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MOJIBAKE_RE = re.compile(r"(?:�|锟斤拷|烫烫烫|屯屯屯|ï¿½|Ã.|Â.|â€|\?{4,})")
BARE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|[IVXLCDM]{1,10})$", re.I)
BARE_DATE_RE = re.compile(r"^(?:18|19|20)\d{2}(?:年|[-/.])?(?:\d{1,2}(?:月|[-/.])?)?(?:\d{1,2}日?)?$")
SINGLE_LATIN_RE = re.compile(r"^[A-Za-z]$")
MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,6}\s*")
LEADING_BULLET_RE = re.compile(r"^\s*[·•▪◦◆◇■□●○▲▶▼◀►◄▸▹]+\s*")
LEADING_ENUM_RE = re.compile(
    r"^\s*(?:"
    r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+|"
    r"[（(](?:\d+|[一二三四五六七八九十]+|[A-Za-z])[）)]|"
    r"第\s*[一二三四五六七八九十百千万零〇0-9]+\s*(?:章|节|篇|部分)[、:：.]?|"
    r"\d+(?:\.\d+){0,5}[、.)）:：]|"
    r"\d+\s+|"
    r"[A-Za-z][.)、]\s+|"
    r"[一二三四五六七八九十]+[、.．]"
    r")\s*"
)
SCAFFOLD_PREFIX_RE = re.compile(
    r"^\s*[【\[]\s*(?:任务引入|任务描述|项目引入|知识准备|学习目标|"
    r"任务实施|相关知识|案例导入|问题导入)\s*[】\]]\s*[：:]?\s*"
)
STRUCTURAL_TITLE_RE = re.compile(
    r"^(?:目录|前言|序言|绪论|内容简介|参考文献|附录|索引|本章小结|"
    r"小结|思考题|习题|练习题|复习题|作业题|测试题|"
    r"词条结构和安排|SEE WEB LINKS|Figure|Table|"
    r"contents|preface|references|index|exercise(?:s)?|chapter\s+[A-Z0-9IVXLCDM]+)$",
    re.I,
)
ENTRY_LABEL_RE = re.compile(r"^词条\s*\d+(?:\.\d+)*(?:[-—]\d+)?$", re.I)
RECOVERABLE_EXTRACTION_LABEL_RE = re.compile(
    r"^(?:英译|含义|词条\s*\d+(?:\.\d+)*(?:[-—]\d+)?)$",
    re.I,
)
FIGURE_TABLE_TITLE_RE = re.compile(
    r"^(?:(?:图|表)\s*[A-Za-z0-9一二三四五六七八九十]+(?:[-—.·]\d+)*|"
    r"(?:fig(?:ure)?|table|plate|photo)\s*[A-Za-z]?\d+)(?:\s|[：:].*)?$",
    re.I,
)
WORKED_EXAMPLE_RE = re.compile(r"^(?:例|例题|example)\s*\d+(?:[-—.]\d+)*\s*$", re.I)
QUESTION_OR_COMMAND_RE = re.compile(
    r"^(?:请|试|讨论|回答|思考|分析|说明|证明|计算|求解|比较|简述|为什么|为何|如何)"
)
SENTENCE_END_RE = re.compile(r"[。！？!?；;]$")
CROSS_REFERENCE_RE = re.compile(r"\b(?:see|refer\s+to)\b", re.I)
LATEX_TOC_TRAILER_RE = re.compile(
    r"\s*\\\(\s*(?:\\(?:ldots|cdots|dots)\s*)+\\\)\s*\d{1,5}\s*$",
    re.I,
)
FIGURE_REF_RE = re.compile(
    r"[（(]?\s*(?:(?:如|见|参见|由|从)\s*)?"
    r"(?:(?:图|表)\s*[A-Za-z]?[0-9一二三四五六七八九十]+[A-Za-z]?|"
    r"\b(?:fig(?:ure)?|table)\b\.?\s*[A-Za-z]?\d+[A-Za-z]?)"
    r"(?:\s*[-—−.·]\s*[A-Za-z0-9]+)*"
    r"(?:\s*(?:中|所示|可知))?\s*[）)]?",
    re.I,
)
PARAGRAPH_MARK_RE = re.compile(r"[▲▶▼◀►◄▸▹▪◦◆◇■□●○•]")
REPEATED_SYMBOL_RE = re.compile(r"([^\w\s\u3400-\u9fff])\1{4,}")
OUTER_QUOTES = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "《": "》",
}
OPEN_BRACKETS = "([{（【"
CLOSE_BRACKETS = ")]｝）】".replace("｝", "}")
BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input-format", choices=("auto", "csv", "json", "jsonl"), default="auto")
    parser.add_argument("--max-name-chars", type=int, default=160)
    parser.add_argument("--max-context-chars", type=int, default=6000)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument(
        "--keep-single-letter",
        action="store_true",
        help="Keep one-letter Latin entries such as A or x; default drops them.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def open_text(path: Path, mode: str = "rt"):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, mode, encoding="utf-8-sig")
    return path.open(mode, encoding="utf-8-sig")


def detect_format(path: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    lower = name.lower()
    if lower.endswith(".csv"):
        return "csv"
    return "json" if lower.endswith(".json") else "jsonl"


def iter_records(path: Path, input_format: str) -> Iterator[tuple[int, dict[str, Any]]]:
    fmt = detect_format(path, input_format)
    if fmt == "csv":
        with open_text(path) as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("CSV input is missing a header")
            for index, row in enumerate(reader, 1):
                yield index, dict(row)
        return
    if fmt == "json":
        with open_text(path) as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("records") or [payload]
        if not isinstance(payload, list):
            raise ValueError("JSON input must be an array or contain items/records")
        for index, row in enumerate(payload, 1):
            if isinstance(row, dict):
                yield index, row
        return
    with open_text(path) as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_no}")
            yield line_no, row


def normalize_text(value: Any, *, simplify: bool = True) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    if simplify and _T2S is not None:
        text = _T2S.convert(text)
    text = CONTROL_RE.sub("", text).replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def compact_key(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", normalize_text(value)).casefold()


def record_key(row: dict[str, Any], line_no: int) -> str:
    existing = row.get("cleaning_record_id")
    if existing:
        return str(existing)
    parts = [
        str(row.get("record_id") or row.get("global_id") or row.get("id") or ""),
        str(row.get("source") or row.get("source_id") or row.get("input_file") or ""),
        str(line_no),
        str(row.get("name") or row.get("key_zh") or ""),
        str(row.get("knowledge_point") or row.get("key_en") or ""),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def strip_outer_quotes(text: str) -> tuple[str, bool]:
    value = text.strip()
    if len(value) < 2:
        return value, False
    expected = OUTER_QUOTES.get(value[0])
    if expected and value[-1] == expected:
        inner = value[1:-1].strip()
        if inner and value[0] not in inner and expected not in inner:
            return inner, True
    return value, False


def strip_unmatched_edge_brackets(text: str) -> tuple[str, bool]:
    value = text.strip()
    changed = False
    while value and value[0] in BRACKET_PAIRS and BRACKET_PAIRS[value[0]] not in value[1:]:
        value = value[1:].lstrip()
        changed = True
    reverse = {close: opening for opening, close in BRACKET_PAIRS.items()}
    while value and value[-1] in reverse and reverse[value[-1]] not in value[:-1]:
        value = value[:-1].rstrip()
        changed = True
    return value, changed


def script_switches(text: str) -> int:
    kinds: list[str] = []
    for char in text:
        kind = "cjk" if CJK_RE.fullmatch(char) else "latin" if LATIN_RE.fullmatch(char) else "other"
        if kind == "other":
            continue
        if not kinds or kinds[-1] != kind:
            kinds.append(kind)
    return max(0, len(kinds) - 1)


def symbol_ratio(text: str) -> float:
    non_space = [char for char in text if not char.isspace()]
    if not non_space:
        return 1.0
    useful = sum(bool(TECH_CHAR_RE.fullmatch(char)) for char in non_space)
    return 1.0 - useful / len(non_space)


def title_source(row: dict[str, Any], field: str) -> Any:
    if field == "name":
        return row.get("name", row.get("key_zh", ""))
    return row.get("knowledge_point", row.get("key_en", ""))


def clean_title(value: Any, max_chars: int, keep_single_letter: bool) -> tuple[str, list[str], str | None]:
    raw = normalize_text(value)
    flags: list[str] = []
    text = MARKDOWN_LINK_RE.sub(r"\1", HTML_RE.sub("", IMAGE_RE.sub("", raw)))
    before = text
    text = MARKDOWN_HEADING_RE.sub("", text)
    text = SCAFFOLD_PREFIX_RE.sub("", text)
    text = LEADING_BULLET_RE.sub("", text)
    for _ in range(2):
        text = LEADING_ENUM_RE.sub("", text)
    text, toc_count = LATEX_TOC_TRAILER_RE.subn("", text)
    if toc_count:
        flags.append("latex_toc_leader_and_page_removed")
    text = text.strip(" \t\r\n:：、-—")
    if text != before:
        flags.append("title_prefix_removed")

    text, changed = strip_outer_quotes(text)
    if changed:
        flags.append("outer_quotes_removed")
    text, changed = strip_unmatched_edge_brackets(text)
    if changed:
        flags.append("unmatched_edge_bracket_removed")
    text = normalize_text(text)

    if not text:
        return "", flags, "empty_title"
    if MOJIBAKE_RE.search(text) or REPEATED_SYMBOL_RE.search(text):
        return text, flags, "garbled_title"
    if URL_RE.fullmatch(text) or EMAIL_RE.fullmatch(text):
        return text, flags, "url_or_email_title"
    if RECOVERABLE_EXTRACTION_LABEL_RE.fullmatch(text):
        flags.append("extraction_label_requires_model_review")
    elif STRUCTURAL_TITLE_RE.fullmatch(text):
        return text, flags, "structural_or_metadata_title"
    if FIGURE_TABLE_TITLE_RE.fullmatch(text):
        return text, flags, "figure_or_table_title"
    if WORKED_EXAMPLE_RE.fullmatch(text):
        return text, flags, "worked_example_title"
    if BARE_NUMBER_RE.fullmatch(text) or BARE_DATE_RE.fullmatch(text):
        return text, flags, "bare_number_or_date"
    if SINGLE_LATIN_RE.fullmatch(text) and not keep_single_letter:
        return text, flags, "single_latin_letter"
    if len(text) > max_chars:
        if len(text) > max(max_chars * 4, 640):
            return text, flags, "extremely_overlong_title"
        flags.append("overlong_title_requires_model_review")
    if len(TECH_CHAR_RE.findall(text)) == 0:
        return text, flags, "no_usable_title_characters"
    if symbol_ratio(text) > 0.62 and len(TECH_CHAR_RE.findall(text)) < 5:
        if any(mark in text for mark in ("$", "\\", "_", "^")):
            flags.append("formula_or_symbol_title_requires_model_review")
        else:
            return text, flags, "symbol_dominated_title"
    if SENTENCE_END_RE.search(text) and len(text) >= 12:
        if CROSS_REFERENCE_RE.search(text):
            flags.append("cross_reference_entry_requires_model_review")
        else:
            flags.append("sentence_like_title_requires_model_review")
    if text.endswith(".") and len(text) >= 20 and len(text.split()) >= 4:
        if CROSS_REFERENCE_RE.search(text):
            flags.append("cross_reference_entry_requires_model_review")
        else:
            flags.append("sentence_like_title_requires_model_review")
    # BM25 candidates may contain valid procedural knowledge such as
    # “计算齿轮接触强度” or “如何选择轴承”. Keep those for semantic review
    # unless they were already rejected as complete sentences above.
    if QUESTION_OR_COMMAND_RE.match(text) and len(text) >= 8:
        flags.append("question_or_procedure_title_requires_model_review")

    switches = script_switches(text)
    if switches >= 4:
        flags.append("mixed_script_requires_model_review")
    if switches >= 8 and len(text) >= 20:
        flags.append("severely_mixed_script_title_requires_model_review")
    if len(TECH_CHAR_RE.findall(text)) <= 1:
        flags.append("very_short_title_requires_model_review")
    if re.search(r"[：:].+[：:]", text):
        flags.append("multiple_colon_segments_requires_model_review")
    return text, flags, None


def clean_context(value: Any, max_chars: int) -> tuple[str, list[str]]:
    text = normalize_text(value)
    flags: list[str] = []
    before = text
    text = MARKDOWN_LINK_RE.sub(r"\1", HTML_RE.sub("", IMAGE_RE.sub("", text)))
    text = PARAGRAPH_MARK_RE.sub("。", text)
    text = FIGURE_REF_RE.sub("", text)
    text = normalize_text(text)
    text = re.sub(r"\s+([，。；：！？,.!?;:])", r"\1", text)
    text = re.sub(r"(?:。\s*){2,}", "。", text).strip()
    if text != before:
        flags.append("context_markup_or_layout_reference_removed")
    if max_chars and len(text) > max_chars:
        prefix = text[:max_chars]
        boundary = max((prefix.rfind(mark) for mark in "。！？；.!?;"), default=-1)
        text = prefix[: boundary + 1] if boundary >= max_chars // 2 else prefix.rstrip() + "…"
        flags.append("context_truncated")
    return text, flags


def context_value(row: dict[str, Any]) -> str:
    parts = []
    for field in ("definition", "en_definition", "description", "en_description", "explanation", "raw_text"):
        value = normalize_text(row.get(field))
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


def classify(row: dict[str, Any], line_no: int, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    key = record_key(row, line_no)
    name_raw = title_source(row, "name")
    knowledge_raw = title_source(row, "knowledge_point")
    name, name_flags, name_reason = clean_title(name_raw, args.max_name_chars, args.keep_single_letter)
    knowledge, knowledge_flags, knowledge_reason = clean_title(
        knowledge_raw, args.max_name_chars, args.keep_single_letter
    )
    context, context_flags = clean_context(context_value(row), args.max_context_chars)

    removed_fields: list[str] = []
    if name_reason:
        name = ""
        if normalize_text(name_raw):
            removed_fields.append("name")
    if knowledge_reason:
        knowledge = ""
        if normalize_text(knowledge_raw):
            removed_fields.append("knowledge_point")

    reasons = []
    if normalize_text(name_raw) and name_reason:
        reasons.append(name_reason)
    if normalize_text(knowledge_raw) and knowledge_reason:
        reasons.append(knowledge_reason)
    status = "pass"
    reason = "rule_pass"
    if not name and not knowledge:
        status = "rejected"
        reason = reasons[0] if len(set(reasons)) == 1 and reasons else "no_valid_title_after_rule_cleanup"

    output = dict(row)
    output["cleaning_record_id"] = key
    output["raw_name"] = normalize_text(name_raw, simplify=False)
    output["raw_knowledge_point"] = normalize_text(knowledge_raw, simplify=False)
    output["name"] = name
    output["knowledge_point"] = knowledge
    if context:
        output["cleaning_context"] = context
    output["rule_cleaning"] = {
        "version": RULE_VERSION,
        "status": status,
        "reason": reason,
        "field_reasons": {"name": name_reason, "knowledge_point": knowledge_reason},
        "removed_fields": removed_fields,
        "flags": sorted(set(name_flags + knowledge_flags + context_flags)),
        "source_line": line_no,
    }
    return status, output


def output_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "pass": output_dir / "rule_pass.jsonl",
        "rejected": output_dir / "rule_rejected.jsonl",
        "sample": output_dir / "rule_pass_sample.jsonl",
        "report": output_dir / "rule_clean_report.json",
    }


def ensure_outputs(paths: dict[str, Path], overwrite: bool) -> None:
    paths["report"].parent.mkdir(parents=True, exist_ok=True)
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("Output exists; use --overwrite: " + ", ".join(map(str, existing)))


def write_sample(source_path: Path, sample_path: Path, size: int) -> None:
    if size <= 0:
        if sample_path.exists():
            sample_path.unlink()
        return
    temporary = sample_path.with_suffix(sample_path.suffix + ".tmp")
    with source_path.open(encoding="utf-8") as source, temporary.open("w", encoding="utf-8") as target:
        for index, line in enumerate(source):
            if index >= size:
                break
            target.write(line)
    temporary.replace(sample_path)


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.max_name_chars < 8 or args.max_context_chars < 0 or args.sample_size < 0 or args.max_records < 0:
        raise ValueError("Invalid numeric argument")
    paths = output_paths(args.output_dir)
    ensure_outputs(paths, args.overwrite)
    temporary = {
        key: paths[key].with_suffix(paths[key].suffix + ".tmp") for key in ("pass", "rejected")
    }
    handles: dict[str, TextIO] = {
        key: path.open("w", encoding="utf-8") for key, path in temporary.items()
    }
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    removed_fields: Counter[str] = Counter()
    processed = 0
    try:
        for line_no, row in iter_records(args.input, args.input_format):
            status, output = classify(row, line_no, args)
            handles[status].write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts[status] += 1
            audit = output["rule_cleaning"]
            reasons[str(audit["reason"])] += 1
            flags.update(audit["flags"])
            removed_fields.update(audit["removed_fields"])
            processed += 1
            if processed % 50_000 == 0:
                print(
                    f"Rule clean: processed={processed:,} pass={counts['pass']:,} rejected={counts['rejected']:,}",
                    flush=True,
                )
            if args.max_records and processed >= args.max_records:
                break
    finally:
        for handle in handles.values():
            handle.close()
    for key in ("pass", "rejected"):
        temporary[key].replace(paths[key])
    write_sample(paths["pass"], paths["sample"], args.sample_size)
    report = {
        "stage": "rule_clean",
        "version": RULE_VERSION,
        "input": str(args.input),
        "output_dir": str(args.output_dir),
        "simplification": "opencc:t2s" if _T2S is not None else "unavailable",
        "settings": {
            "max_name_chars": args.max_name_chars,
            "max_context_chars": args.max_context_chars,
            "keep_single_letter": args.keep_single_letter,
            "max_records": args.max_records,
        },
        "counts": dict(counts),
        "reasons": dict(reasons.most_common()),
        "flags": dict(flags.most_common()),
        "removed_fields": dict(removed_fields.most_common()),
        "outputs": {key: str(value) for key, value in paths.items()},
    }
    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
