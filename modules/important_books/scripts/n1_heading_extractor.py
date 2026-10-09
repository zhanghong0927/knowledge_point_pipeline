"""基于标题层级的 N1 规则抽取器，仅输出知识点标题，不调用模型。"""
from __future__ import annotations

import argparse
import csv
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any



VERSION = "n1-knowledge-point-extractor-1.4"
MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
NUMBERED = re.compile(
    r"^\s{0,3}(第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章节篇部卷编条款])\s*(.*)$"
    r"|^\s{0,3}((?:\d+\.){1,5}\d*|\d+)[、.．)）]?\s+(.+?)\s*$"
)
CHINESE_SUBHEADING = re.compile(r"^\s{0,3}([一二三四五六七八九十百]+)[、.．]\s*(.+?)\s*$")
CHINESE_PAREN_HEADING = re.compile(r"^\s{0,3}[（(]([一二三四五六七八九十百]+)[）)]\s*(.+?)\s*$")
ARABIC_PAREN_HEADING = re.compile(r"^\s{0,3}[（(](\d+)[）)]\s*(.+?)\s*$")
PART_LABELS = ("目录", "目次", "contents", "table of contents")
SKIP_LABELS = {
    "参考文献", "参考书目", "bibliography", "references", "索引", "index",
    "附录", "版权", "版权信息", "版权声明", "郑重声明", "出版说明", "扉页", "封面", "目录", "目次",
    "内容提要", "内容简介", "前言", "序言", "序",
    "contents", "table of contents", "图书在版编目", "cip数据", "cip",
}
PARA_SPLIT = re.compile(r"\n\s*\n")
INLINE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
LIST_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、．]\s+)")
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])(?:\s+|(?=[^\s]))")
INDEX_PAGES = re.compile(r"(?P<term>.+?),\s*(?P<pages>\d+[a-z]?(?:[–—-]\d+[a-z]?)?(?:,\s*\d+[a-z]?(?:[–—-]\d+[a-z]?)?)*)\s*$", re.I)
INDEX_NOTE = re.compile(r"^page numbers in italics refer to", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_title(value: str) -> str:
    value = re.sub(r"(?:\s*/\s*\d+|\s*\.{2,}\s*\d+)\s*$", "", value)
    return re.sub(r"\s+", " ", value).strip().strip("# ")


def heading_from_line(line: str, *, previous_line: str = "", next_line: str = "") -> tuple[int, str, str] | None:
    """Return (level, title, method) or None; heuristic numbered headings are marked."""
    m = MD_HEADING.match(line)
    if m:
        title = clean_title(m.group(2))
        explicit_level = explicit_number_level(title)
        return max(len(m.group(1)), explicit_level or 0), title, "markdown_numbered" if explicit_level else "markdown"
    plain_title = clean_title(line)
    if exclusion_reason(plain_title, True):
        return 1, plain_title, "section_label"
    m = CHINESE_SUBHEADING.match(line)
    if m:
        title = clean_title(m.group(2))
        # Chinese ordinal list items are common in prose; retain as headings only
        # when the line looks like a label and is structurally separated.
        if not _looks_like_structural_numbered_title(title, previous_line, next_line):
            return None
        return 4, clean_title(f"{m.group(1)}、{title}"), "numbered"
    m = CHINESE_PAREN_HEADING.match(line)
    if m:
        title = clean_title(m.group(2))
        if not _looks_like_structural_numbered_title(title, previous_line, next_line):
            return None
        return 5, clean_title(f"（{m.group(1)}）{title}"), "numbered"
    m = ARABIC_PAREN_HEADING.match(line)
    if m:
        title = clean_title(m.group(2))
        if not _looks_like_structural_numbered_title(title, previous_line, next_line):
            return None
        return 6, clean_title(f"（{m.group(1)}）{title}"), "numbered"
    m = NUMBERED.match(line)
    if not m:
        return None
    if m.group(1):
        marker, rest = m.group(1).strip(), m.group(2)
        marker_type = re.search(r"[章节篇部卷编条款]$", marker).group(0)
        level = {"篇": 1, "部": 1, "卷": 1, "编": 1, "章": 2, "节": 3, "条": 4, "款": 5}.get(marker_type, 2)
        return level, clean_title(f"{marker} {rest}"), "numbered"
    marker, title = m.group(3), m.group(4)
    marker = marker.rstrip(".")
    if marker.isdigit() and (line.rstrip().endswith(("。", "；", ";", "！", "？", "!", "?"))
                             or not _looks_like_structural_numbered_title(title, previous_line, next_line)):
        return None
    level = min(6, max(2, marker.count(".") + 2))
    return level, clean_title(f"{marker} {title}"), "numbered"


def explicit_number_level(title: str) -> int | None:
    """Infer semantic depth from an explicit heading prefix, including MD headings."""
    title = clean_title(title)
    m = re.match(r"^第\s*[零〇一二三四五六七八九十百千万两\d]+\s*([章节篇部卷编条款])", title)
    if m:
        return {"篇": 1, "部": 1, "卷": 1, "编": 1, "章": 2, "节": 3, "条": 4, "款": 5}.get(m.group(1), 2)
    m = re.match(r"^(\d+(?:\.\d+){1,5})\b", title)
    if m:
        return min(6, m.group(1).count(".") + 2)
    m = CHINESE_SUBHEADING.match(title)
    if m:
        return 4
    if CHINESE_PAREN_HEADING.match(title):
        return 5
    if ARABIC_PAREN_HEADING.match(title):
        return 6
    return None


def _looks_like_structural_numbered_title(title: str, previous_line: str, next_line: str) -> bool:
    """Conservative context check for ambiguous list-like numbering."""
    title = clean_title(title)
    if not title or len(title) > 90 or re.search(r"[。！？!?；;]$", title):
        return False
    # A heading usually has a short label followed by prose; reject another
    # immediately numbered item as this is more likely a list.
    if not next_line.strip() or CHINESE_SUBHEADING.match(next_line) or NUMBERED.match(next_line):
        return False
    # Structural separation: blank line or a recognized heading immediately before.
    return not previous_line.strip() or bool(MD_HEADING.match(previous_line)) or bool(NUMBERED.match(previous_line))


def exclusion_reason(title: str, enabled: bool) -> str | None:
    if not enabled:
        return None
    normalized = re.sub(r"[\s:：.。—–-]+", "", title).casefold()
    if any(normalized == re.sub(r"[\s:：.。—–-]+", "", label).casefold()
           or normalized.startswith(re.sub(r"[\s:：.。—–-]+", "", label).casefold())
           for label in SKIP_LABELS):
        return "appendix_or_front_back_matter"
    if any(normalized.startswith(re.sub(r"[\s:：.。—–-]+", "", label).casefold()) for label in PART_LABELS):
        return "table_of_contents"
    return None


def normalize_paragraph(text: str) -> str:
    """Preserve wording while removing common Markdown presentation wrappers."""
    text = INLINE_IMAGE.sub("", text)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、．]\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_~]{1,3}", "", text)
    text = re.sub(r"\\\\\s*$", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def paragraph_is_noise(text: str) -> bool:
    clean = normalize_paragraph(text)
    if not clean:
        return True
    if len(clean) < 18 and not re.search(r"[。！？!?；;]$", clean):
        return True
    if re.search(r"\.{3,}|…{2,}|\b(?:contents|table of contents)\b", clean, re.IGNORECASE):
        return True
    if re.match(r"^(?:第[一二三四五六七八九十百\d]+章|chapter\s+\d+|\d+(?:\.\d+){0,4}).{0,40}\.{2,}\s*\d+\s*$", clean, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:\[?\d{1,4}\]?|[ivxlcdm]{1,8})", clean, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:图|表|figure|fig\.?|table)\s*[\d.\-–—]+\s*[：:、.．-]?", clean, re.IGNORECASE):
        return True
    return False


def sentence_units(paragraph: str) -> list[str]:
    clean = normalize_paragraph(paragraph)
    if not clean:
        return []
    chunks = [x.strip() for x in SENTENCE_SPLIT.split(clean) if x.strip()]
    # Short heading-like labels often lack terminal punctuation; retain them with the next sentence.
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) < 24 and not re.search(r"[。！？!?；;]$", merged[-1]):
            merged[-1] += chunk
        else:
            merged.append(chunk)
    return merged


def make_knowledge_points(title: str, parent_path: list[str]) -> list[dict[str, Any]]:
    """Emit only the heading concept; hierarchy stays in separate metadata fields."""
    del parent_path  # Retained in the signature for compatibility with callers.
    clean_title = re.sub(
        r"^(?:第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章节篇部卷编条款]|"
        r"[一二三四五六七八九十百]+[、.．]|[（(][一二三四五六七八九十百]+[）)]|"
        r"\d+(?:\.\d+){0,5}[.)、．]?|[（(]\d+[）)])\s*",
        "", title.strip(),
    ).strip()
    return [{"knowledge_point": clean_title or title.strip()}]


def extract_index(lines: list[str], headings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    """Extract entries from an explicit back-of-book Index section."""
    index_heading = next((h for h in reversed(headings)
                          if re.fullmatch(r"(?:subject\s+|author\s+)?index(?:\s+of\s+.+)?", h["title"], re.I)), None)
    if not index_heading:
        return [], [], None
    start = index_heading["line"]
    later = [h for h in headings if h["line"] > start]
    end = len(lines) + 1
    for h in later:
        title = h["title"].strip()
        # Alphabetic/numeric divider headings are part of the index. A real
        # following section begins only after index content has started and
        # its heading is not a one-character group marker.
        if re.fullmatch(r"[A-Z0-9]", title, re.I):
            continue
        if title.casefold() == "taylor & francis ebooks":
            end = h["line"]
            break
    entries: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    parent_term: str | None = None
    for line_no in range(start + 1, end):
        raw = lines[line_no - 1]
        stripped = raw.strip()
        if not stripped:
            continue
        if INLINE_IMAGE.fullmatch(stripped):
            continue
        if re.fullmatch(r"#{1,6}\s*[A-Z0-9]\s*#*", stripped, re.I):
            continue
        if INDEX_NOTE.match(stripped):
            notes.append({"text": stripped, "source_line": line_no})
            continue
        cross = re.search(r";\s*(see(?: also)?)\s+(.+?)\s*$", stripped, re.I)
        cross_ref = None
        if cross:
            cross_ref = {"type": cross.group(1).lower(), "target": cross.group(2).strip()}
            stripped = stripped[:cross.start()].rstrip("; ,")
        m = INDEX_PAGES.match(stripped)
        if not m and (stripped.startswith(("[", "![") ) or re.match(r"^(?:https?://|www\.)", stripped, re.I)):
            continue
        term = m.group("term").strip(" ,") if m else stripped
        pages = re.findall(r"\d+[a-z]?(?:[–—-]\d+[a-z]?)?", m.group("pages"), re.I) if m else []
        # Index subentries are conventionally indented; retain their parent explicitly.
        indented = bool(raw[:1].isspace())
        if not indented:
            parent_term = term
        entries.append({"term": term, "parent_term": parent_term if indented else None,
                        "page_refs": pages, "cross_reference": cross_ref,
                        "source_line": line_no, "raw": raw})
    return entries, notes, start


def extract_document(md_path: Path, *, exclude_front_back: bool = True) -> dict[str, Any]:
    raw = md_path.read_text(encoding="utf-8-sig")
    lines = raw.splitlines()
    headings: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    in_fence = False
    fence_char = ""
    for line_no, line in enumerate(lines, 1):
        # Image-only Markdown lines are layout/content artifacts, never headings.
        if INLINE_IMAGE.fullmatch(line.strip()):
            continue
        fence = re.match(r"^\s{0,3}(```+|~~~+)", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue
        previous_line = lines[line_no - 2] if line_no > 1 else ""
        next_line = lines[line_no] if line_no < len(lines) else ""
        found = heading_from_line(line, previous_line=previous_line, next_line=next_line)
        if not found:
            continue
        level, title, method = found
        if not title:
            continue
        skip = exclusion_reason(title, exclude_front_back)
        headings.append({"line": line_no, "level": level, "title": title, "method": method,
                         "skip_reason": skip})

    index_entries, index_notes, index_heading_line = extract_index(lines, headings)
    if index_heading_line is not None:
        in_index = False
        for heading in headings:
            if heading["line"] == index_heading_line:
                in_index = True
            if in_index:
                heading["skip_reason"] = heading["skip_reason"] or "back_of_book_index"
                if heading["line"] > index_heading_line and heading["title"].casefold() == "taylor & francis ebooks":
                    break

    units: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    last_level: int | None = None
    for idx, heading in enumerate(headings):
        level = heading["level"]
        if last_level is not None and level > last_level + 1:
            warnings.append({"line": heading["line"], "code": "heading_level_jump",
                             "message": f"标题层级从 {last_level} 跳到 {level}；按实际级别保留，不自动补造标题。"})
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        parent_path = [x["title"] for x in stack]
        next_line = headings[idx + 1]["line"] if idx + 1 < len(headings) else len(lines) + 1
        start, end = heading["line"], next_line - 1
        content_lines = lines[start:end]
        body = "\n".join(content_lines).strip()
        is_skipped = heading["skip_reason"] is not None
        points = [] if is_skipped else make_knowledge_points(heading["title"], parent_path)
        unit = {
            "unit_id": f"H{len(units) + 1:05d}",
            "title": heading["title"],
            "level": level,
            "parent_path": parent_path,
            "heading_line": heading["line"],
            "content_line_start": start + 1,
            "content_line_end": end,
            "heading_method": heading["method"],
            "status": "excluded" if is_skipped else ("extracted" if points else "needs_review"),
            "exclude_reason": heading["skip_reason"],
            "knowledge_points": points,
            "knowledge_point_count": len(points),
            "content_char_count": len(body),
        }
        units.append(unit)
        if not is_skipped:
            stack.append({"level": level, "title": heading["title"]})
        last_level = level

    if not headings:
        warnings.append({"line": None, "code": "no_headings_found",
                         "message": "未识别到标题；该文件可能不是 N1，或标题未被 Markdown/编号规则识别。"})
    elif sum(u["status"] == "extracted" for u in units) == 0:
        warnings.append({"line": None, "code": "no_content_units",
                         "message": "识别到的标题均在排除目录/附属内容范围内，没有生成正文单元。"})

    stat = md_path.stat()
    return {
        "schema_version": VERSION,
        "source": {"path": str(md_path.resolve()), "sha256": sha256(md_path), "size_bytes": stat.st_size,
                   "line_count": len(lines)},
        "rule": {"id": "N1", "name": "章节·多级标题型", "unit": "knowledge_point_only",
                 "exclude_front_back_matter": exclude_front_back},
        "summary": {"heading_count": len(headings), "extracted_units": sum(u["status"] == "extracted" for u in units),
                    "knowledge_point_count": sum(u["knowledge_point_count"] for u in units),
                    "review_units": sum(u["status"] == "needs_review" for u in units),
                    "excluded_units": sum(u["status"] == "excluded" for u in units), "warning_count": len(warnings),
                    "index_entry_count": len(index_entries), "index_heading_line": index_heading_line},
        "units": units,
        "index_entries": index_entries,
        "index_notes": index_notes,
        "warnings": warnings,
    }


def discover(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() != ".md":
            raise ValueError(f"Input file must be .md: {root}")
        return [root]
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def run(root: Path | None, out: Path, *, exclude_front_back: bool = True, files: list[Path] | None = None) -> dict[str, Any]:
    files = files if files is not None else discover(root)  # type: ignore[arg-type]
    if not files:
        raise ValueError(f"No .md files found under: {root}")
    out.mkdir(parents=True, exist_ok=True)
    results_dir = out / "results"
    results_dir.mkdir(exist_ok=True)
    manifest: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    all_index: list[dict[str, Any]] = []
    all_knowledge_points: list[dict[str, Any]] = []
    all_title_reviews: list[dict[str, Any]] = []
    used: set[str] = set()
    for path in files:
        base = path.stem
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "book"
        name, suffix = safe, 2
        while name.casefold() in used:
            name, suffix = f"{safe}_{suffix}", suffix + 1
        used.add(name.casefold())
        try:
            result = extract_document(path, exclude_front_back=exclude_front_back)
            result = filter_result(result, path.read_text(encoding="utf-8-sig"))
            all_index.extend({"book": path.stem, **entry} for entry in result["index_entries"])
            for unit in result["units"]:
                filter_meta = unit.get("title_filter", {})
                if filter_meta.get("action") == "review":
                    candidates = filter_meta.get("candidate_knowledge_points", [])
                    all_title_reviews.append({
                        "identifier": path.stem,
                        "title_candidate": candidates[0].get("knowledge_point", "") if candidates else unit.get("title", ""),
                        "heading_line": unit.get("heading_line", ""),
                        "reason_code": filter_meta.get("reason_code", ""),
                        "content_char_count": unit.get("content_char_count", ""),
                    })
                if unit["status"] != "extracted":
                    continue
                for point in unit["knowledge_points"]:
                    all_knowledge_points.append({
                        "identifier": path.stem,
                        "title": path.stem,
                        "knowledge_point": point["knowledge_point"],
                        "level": unit["level"],
                        "parent_path": " > ".join(unit["parent_path"]),
                        "heading_line": unit["heading_line"],
                        "source_line_start": unit["content_line_start"],
                        "source_line_end": unit["content_line_end"],
                    })
            target = results_dir / f"{name}.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            manifest.append({"source": str(path.resolve()), "result": str(target.resolve()), **result["summary"]})
        except Exception as exc:  # one malformed file must not block a batch
            failure = {"source": str(path.resolve()), "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            manifest.append({**failure, "status": "technical_failed"})
    with (out / "knowledge_points.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["identifier", "title", "knowledge_point", "level", "parent_path",
                  "heading_line", "source_line_start", "source_line_end"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_knowledge_points)
    with (out / "title_review.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["identifier", "title_candidate", "heading_line", "reason_code", "content_char_count"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_title_reviews)
    with (out / "index_entries.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["book", "term", "parent_term", "page_refs", "cross_reference", "source_line", "raw"])
        writer.writeheader()
        for entry in all_index:
            row = dict(entry)
            row["page_refs"] = "; ".join(row["page_refs"])
            row["cross_reference"] = (f"{row['cross_reference']['type']}: {row['cross_reference']['target']}" if row["cross_reference"] else "")
            writer.writerow(row)
    summary = {"schema_version": VERSION, "input_root": str(root.resolve()) if root else None, "output_dir": str(out.resolve()),
               "books_total": len(files), "books_succeeded": len(files) - len(failures),
               "books_failed": len(failures), "units_extracted": sum(x.get("extracted_units", 0) for x in manifest),
               "knowledge_points_extracted": sum(x.get("knowledge_point_count", 0) for x in manifest),
               "index_entries_extracted": len(all_index),
               "knowledge_points_csv": str((out / "knowledge_points.csv").resolve()),
               "title_review_csv": str((out / "title_review.csv").resolve()),
               "title_filter_removed_count": sum(x.get("title_filter_removed_count", 0) for x in manifest),
               "title_filter_review_count": sum(x.get("title_filter_review_count", 0) for x in manifest),
               "units_excluded": sum(x.get("excluded_units", 0) for x in manifest),
               "manifest": manifest, "failures": failures}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


TITLE_FILTER_VERSION = "n1-title-filter-1.3"

# Section labels that are almost never knowledge concepts. Keep this list
# intentionally narrow; uncertain headings go to review instead.
NOISE_LABELS = {
    "acknowledgements", "acknowledgments", "about the author", "author",
    "copyright", "dedication", "disclaimer", "foreword", "preface",
    "contents", "table of contents", "list of figures", "list of tables",
    "list of illustrations", "list of abbreviations", "list of symbols",
    "references", "bibliography", "further reading", "index", "subject index",
    "author index", "questions", "review questions", "discussion questions",
    "self test questions", "exercises", "problems and exercises",
    "参考文献", "主要参考文献", "思考题", "思考与讨论", "复习思考题", "练习题", "中文著作", "外文著作", "外文文献", "译著",
}

AUTHOR_NAME_RE = re.compile(
    r"^[A-Z][A-Za-z.'’`-]+(?:\s+[A-Z][A-Za-z.'’`-]+){1,3}$"
)
CASE_LABEL_RE = re.compile(
    r"^(?:(?:case study|case|example|illustration)\s*[:：#.-]|案例(?:分析)?\s*[\d一二三四五六七八九十百]*\s*[:：])",
    re.I,
)
PAGE_OR_MARKER_RE = re.compile(r"^(?:[A-Z]|\d{1,3}|[ivxlcdm]{1,8})$", re.I)
TOC_PAGE_SUFFIX_RE = re.compile(r"(?:\.{2,}|…+|⋯+|·{2,}|[-—–]{2,})\s*\d{1,4}\s*$")
CHAPTER_LEVEL_RE = re.compile(r"^第\s*[零〇一二三四五六七八九十百千万两\d]+\s*([篇部卷编章节条款])")
DECIMAL_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){1,5})\b")
CHINESE_ENUM_RE = re.compile(r"^[一二三四五六七八九十百]+[、.．]")
CHINESE_PAREN_RE = re.compile(r"^[（(][一二三四五六七八九十百]+[）)]")
ARABIC_PAREN_RE = re.compile(r"^[（(]\d+[）)]")
SINGLE_ARABIC_RE = re.compile(r"^\d+[.)、．]\s*\S")
# Numbering is useful for hierarchy inference, but is not part of the concept name.
LEADING_ENUM_PREFIX_RE = re.compile(
    r"^(?:第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章节篇部卷编条款]|"
    r"[一二三四五六七八九十百]+[、.．]|[（(][一二三四五六七八九十百]+[）)]|"
    r"\d+(?:\.\d+){0,5}[.)、．]?|[（(]\d+[）)])\s*"
)


def _display_title(value: str) -> str:
    """Remove structural numbering from the displayed knowledge-point title."""
    title = _clean_title(value)
    return LEADING_ENUM_PREFIX_RE.sub("", title).strip() or title


def _clean_title(value: str) -> str:
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" .。—–-\t")


def _canonical(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", _clean_title(value)).casefold()


def _is_contents_title(value: str) -> bool:
    return _canonical(value) in {"contents", "tableofcontents", "目录", "目次"}


def _find_toc_runs(units: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Find consecutive heading-shaped TOC rows with page leaders and no body."""
    candidates = [
        unit for unit in units
        if TOC_PAGE_SUFFIX_RE.search(_clean_title(str(unit.get("title", ""))))
        and int(unit.get("content_char_count", 0) or 0) <= 3
    ]
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_line: int | None = None
    for unit in candidates:
        line_no = int(unit.get("heading_line") or 0)
        if current and (previous_line is None or line_no - previous_line > 5):
            if len(current) >= 3:
                runs.append(current)
            current = []
        current.append(unit)
        previous_line = line_no
    if len(current) >= 3:
        runs.append(current)
    return runs


def _find_contents_line(lines: list[str], units: list[dict[str, Any]]) -> int | None:
    candidates: list[int] = []
    for line_no, line in enumerate(lines, 1):
        if _is_contents_title(line):
            candidates.append(line_no)
    candidates.extend(
        int(unit["heading_line"])
        for unit in units
        if _is_contents_title(str(unit.get("title", "")))
        and unit.get("heading_line") is not None
    )
    return min(candidates) if candidates else None


def _noise_label(title: str) -> bool:
    normalized_title = re.sub(r"^[一二三四五六七八九十]+[、.．]\s*", "", _clean_title(title))
    canonical = _canonical(normalized_title)
    labels = {_canonical(x) for x in NOISE_LABELS}
    if canonical in labels:
        return True
    # Page-numbered bibliography headings often look like "参考文献 …… 247".
    without_page = TOC_PAGE_SUFFIX_RE.sub("", normalized_title).strip(" :：.。—–-")
    return _canonical(without_page) in labels


def _cover_repeats_title(title: str, lines: list[str], heading_line: int) -> bool:
    """Detect a cover/title-page heading repeated in nearby cover text."""
    normalized_title = _canonical(title)
    if len(normalized_title) < 5:
        return False
    prefix = " ".join(lines[: max(0, heading_line - 1)])
    normalized_prefix = _canonical(prefix)
    return normalized_title in normalized_prefix


def _decision(
    unit: dict[str, Any],
    lines: list[str],
    contents_line: int | None,
    index_line: int | None,
    toc_lines: set[int],
) -> tuple[str, str] | None:
    title = _clean_title(str(unit.get("title", "")))
    line_no = int(unit.get("heading_line") or 0)
    if not title:
        return "exclude", "empty_title"

    if _noise_label(title):
        return "exclude", "non_knowledge_section_label"

    if line_no in toc_lines:
        return "exclude", "table_of_contents_entry"

    # The explicit index parser owns index content. Heading-shaped alphabetic
    # dividers and terms following INDEX should not become N1 knowledge points.
    if index_line is not None and line_no >= index_line:
        return "exclude", "back_of_book_index_heading"
    if PAGE_OR_MARKER_RE.fullmatch(title):
        return "exclude", "isolated_page_or_index_marker"

    before_contents = contents_line is not None and line_no < contents_line
    if before_contents:
        if _cover_repeats_title(title, lines, line_no):
            return "exclude", "repeated_cover_title"
        if AUTHOR_NAME_RE.fullmatch(title):
            return "exclude", "author_name_in_front_matter"
        # Don't silently lose unusual headings before the contents page.
        return "review", "heading_before_contents"

    if CASE_LABEL_RE.match(title) or title.endswith((":", "：")):
        return "review", "case_or_label_heading"

    return None


def _semantic_level(title: str, current_level: int, stack: list[dict[str, Any]]) -> int:
    title = _clean_title(title)
    chapter = CHAPTER_LEVEL_RE.match(title)
    if chapter:
        return {"篇": 1, "部": 1, "卷": 1, "编": 1, "章": 2, "节": 3, "条": 4, "款": 5}.get(
            chapter.group(1), current_level
        )
    decimal = DECIMAL_HEADING_RE.match(title)
    if decimal:
        return min(6, decimal.group(1).count(".") + 2)
    if CHINESE_ENUM_RE.match(title):
        return 4
    if CHINESE_PAREN_RE.match(title):
        return 5
    if ARABIC_PAREN_RE.match(title):
        return 6
    if SINGLE_ARABIC_RE.match(title):
        parent = next((item for item in reversed(stack) if not item["single_arabic"]), None)
        return min(6, max(2, int(parent["level"]) + 1)) if parent else 2
    return current_level


def _rebuild_hierarchy(units: list[dict[str, Any]]) -> tuple[int, int]:
    """Rebuild accepted N1 parent paths after noisy units are removed."""
    stack: list[dict[str, Any]] = []
    changed_paths = 0
    changed_levels = 0
    for unit in units:
        if unit.get("status") != "extracted" or not unit.get("knowledge_points"):
            continue
        old_level = int(unit.get("level", 2))
        level = _semantic_level(str(unit.get("title", "")), old_level, stack)
        if old_level != level:
            changed_levels += 1
            unit.setdefault("title_filter", {"action": "keep", "reason_code": "semantic_level_inferred"})
            unit["title_filter"]["original_level"] = old_level
            unit["level"] = level
        while stack and int(stack[-1]["level"]) >= level:
            stack.pop()
        new_parent = [str(item["title"]) for item in stack]
        old_parent = unit.get("parent_path", [])
        if old_parent != new_parent:
            changed_paths += 1
            if "title_filter" not in unit:
                unit["title_filter"] = {"action": "keep", "reason_code": "parent_path_rebuilt"}
            unit["title_filter"]["original_parent_path"] = old_parent
        unit["parent_path"] = new_parent
        display_title = _display_title(str(unit.get("title", "")))
        for point in unit["knowledge_points"]:
            point["knowledge_point"] = display_title
        stack.append({
            "level": level,
            "title": str(unit.get("title", "")),
            "single_arabic": bool(SINGLE_ARABIC_RE.match(_clean_title(str(unit.get("title", ""))))),
        })
    return changed_paths, changed_levels


def filter_result(
    result: dict[str, Any],
    source_text: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return a filtered copy of an N1 extraction result.

    High-confidence noise becomes ``excluded``. Ambiguous front-matter or
    case/label headings become ``needs_review``. Original titles, line numbers,
    source metadata, and index entries are preserved for auditability.
    """
    output = copy.deepcopy(result)
    if not force and output.get("title_filter", {}).get("version") == TITLE_FILTER_VERSION:
        return output

    lines = source_text.splitlines()
    units = output.get("units", [])
    toc_runs = _find_toc_runs(units)
    toc_lines = {
        int(unit.get("heading_line") or 0)
        for run in toc_runs for unit in run
    }
    explicit_contents_line = _find_contents_line(lines, units)
    inferred_contents_line = min(toc_lines) if toc_lines else None
    contents_line = min(
        [line for line in (explicit_contents_line, inferred_contents_line) if line is not None],
        default=None,
    )
    index_line = output.get("summary", {}).get("index_heading_line")
    if index_line is not None:
        index_line = int(index_line)

    removed = 0
    sent_to_review = 0
    for unit in units:
        if unit.get("status") != "extracted" or not unit.get("knowledge_points"):
            continue
        decision = _decision(unit, lines, contents_line, index_line, toc_lines)
        if decision is None:
            continue
        action, reason = decision
        candidates = copy.deepcopy(unit.get("knowledge_points", []))
        unit["title_filter"] = {
            "action": action,
            "reason_code": reason,
            "candidate_knowledge_points": candidates,
        }
        unit["knowledge_points"] = []
        unit["knowledge_point_count"] = 0
        if action == "exclude":
            unit["status"] = "excluded"
            unit["exclude_reason"] = f"title_filter:{reason}"
            removed += 1
        else:
            unit["status"] = "needs_review"
            unit["review_reason"] = f"title_filter:{reason}"
            sent_to_review += 1

    rebuilt_parent_paths, semantic_levels_adjusted = _rebuild_hierarchy(units)

    summary = output.setdefault("summary", {})
    summary["extracted_units"] = sum(u.get("status") == "extracted" for u in units)
    summary["knowledge_point_count"] = sum(
        int(u.get("knowledge_point_count", 0)) for u in units
    )
    summary["review_units"] = sum(u.get("status") == "needs_review" for u in units)
    summary["excluded_units"] = sum(u.get("status") == "excluded" for u in units)
    summary["title_filter_removed_count"] = removed
    summary["title_filter_review_count"] = sent_to_review
    output["title_filter"] = {
        "version": TITLE_FILTER_VERSION,
        "contents_line": contents_line,
        "index_preserved_separately": True,
        "toc_ranges": [
            {"start_line": int(run[0].get("heading_line") or 0),
             "end_line": int(run[-1].get("heading_line") or 0),
             "entry_count": len(run)}
            for run in toc_runs
        ],
        "parent_paths_rebuilt": rebuilt_parent_paths,
        "semantic_levels_adjusted": semantic_levels_adjusted,
        "removed_count": removed,
        "review_count": sent_to_review,
    }
    return output


def write_knowledge_points_csv(
    result: dict[str, Any], path: Path, *, identifier: str | None = None
) -> int:
    source_path = Path(result.get("source", {}).get("path", "book.md"))
    book_id = identifier or source_path.stem
    rows: list[dict[str, Any]] = []
    for unit in result.get("units", []):
        if unit.get("status") != "extracted":
            continue
        for item in unit.get("knowledge_points", []):
            rows.append({
                "identifier": book_id,
                "title": source_path.stem,
                "knowledge_point": item.get("knowledge_point", ""),
                "level": unit.get("level", ""),
                "parent_path": " > ".join(unit.get("parent_path", [])),
                "heading_line": unit.get("heading_line", ""),
                "source_line_start": unit.get("content_line_start", ""),
                "source_line_end": unit.get("content_line_end", ""),
            })
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "identifier", "title", "knowledge_point", "level", "parent_path",
        "heading_line", "source_line_start", "source_line_end",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_review_csv(
    result: dict[str, Any], path: Path, *, identifier: str | None = None
) -> int:
    source_path = Path(result.get("source", {}).get("path", "book.md"))
    book_id = identifier or source_path.stem
    rows = []
    for unit in result.get("units", []):
        metadata = unit.get("title_filter", {})
        if metadata.get("action") != "review":
            continue
        candidates = metadata.get("candidate_knowledge_points", [])
        point = candidates[0].get("knowledge_point", "") if candidates else unit.get("title", "")
        rows.append({
            "identifier": book_id,
            "title_candidate": point,
            "heading_line": unit.get("heading_line", ""),
            "reason_code": metadata.get("reason_code", ""),
            "content_char_count": unit.get("content_char_count", ""),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["identifier", "title_candidate", "heading_line", "reason_code", "content_char_count"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def filter_main() -> int:
    parser = argparse.ArgumentParser(description="过滤 N1 结果中的标题噪声，并保留待复核项与 INDEX。")
    parser.add_argument("--input-json", required=True, type=Path, help="N1 抽取结果 JSON")
    parser.add_argument("--source-md", type=Path, help="原始 Markdown；省略时读取 JSON 中的 source.path")
    parser.add_argument("--output-json", type=Path, help="过滤后的 JSON，默认在输入名后追加 .filtered")
    parser.add_argument("--output-csv", type=Path, help="知识点 CSV，默认在输入名后追加 .filtered")
    parser.add_argument("--review-csv", type=Path, help="待复核标题 CSV，默认在输入名后追加 .review")
    parser.add_argument("--identifier", help="输出 CSV 的 identifier，默认使用原始 MD 文件名")
    parser.add_argument("--force", action="store_true", help="对已经过滤过的 JSON 再次运行")
    args = parser.parse_args()

    raw = json.loads(args.input_json.read_text(encoding="utf-8-sig"))
    md_path = args.source_md or Path(raw.get("source", {}).get("path", ""))
    if not md_path or not md_path.is_file():
        parser.error("找不到源 MD；请用 --source-md 指定，或确保 JSON 的 source.path 可访问。")
    result = filter_result(raw, md_path.read_text(encoding="utf-8-sig"), force=args.force)

    output_json = args.output_json or args.input_json.with_name(args.input_json.stem + ".filtered.json")
    output_csv = args.output_csv or args.input_json.with_name(args.input_json.stem + ".filtered.csv")
    review_csv = args.review_csv or args.input_json.with_name(args.input_json.stem + ".review.csv")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    point_count = write_knowledge_points_csv(result, output_csv, identifier=args.identifier)
    review_count = write_review_csv(result, review_csv, identifier=args.identifier)
    print(json.dumps({
        "output_json": str(output_json),
        "output_csv": str(output_csv),
        "review_csv": str(review_csv),
        "knowledge_points": point_count,
        "removed": result.get("title_filter", {}).get("removed_count", 0),
        "review": review_count,
        "index_entries_preserved": result.get("summary", {}).get("index_entry_count", 0),
    }, ensure_ascii=False))
    return 0




def main() -> int:
    if "--input-json" in sys.argv[1:] or any(arg.startswith("--input-json=") for arg in sys.argv[1:]):
        return filter_main()
    parser = argparse.ArgumentParser(description="N1 章节·多级标题型规则抽取（本地结构切分，不调用模型）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="单个 MD 文件或包含 MD 的目录")
    group.add_argument("--files", type=Path, nargs="+", help="指定要处理的一个或多个 MD 文件")
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--include-front-back", action="store_true", help="不排除目录、参考文献、索引、附录等标题")
    args = parser.parse_args()
    try:
        result = run(args.input, args.out, exclude_front_back=not args.include_front_back, files=args.files)
        print(json.dumps({k: v for k, v in result.items() if k not in {"manifest", "failures"}}, ensure_ascii=False))
        for failure in result["failures"]:
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1 if result["books_failed"] else 0
    except Exception as exc:
        print(json.dumps({"status": "technical_failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

