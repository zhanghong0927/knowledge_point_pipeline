from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import urllib.request
import unicodedata
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_API_URL = (
    "http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job."
    "v5000-prod.nhss.zhejianglab.com/v1/chat/completions"
)
DEFAULT_MODEL = "/mnt/si002991n0no/default/model/Qwen/Qwen3.8-27B"
TRACKS = ("辞海类", "其他重要书籍")
DECISIONS = ("PASS", "REVIEW", "DROP")
KNOWLEDGE_TYPES = {
    "dictionary_entry",
    "general_knowledge",
    "product_specific_procedure",
    "software_operation",
    "paper_experiment",
    "exercise_question",
    "catalog_table",
    "biography_news_marketing",
    "frontmatter_index",
    "invalid_text",
    "unclear",
}
LOW_UTILITY_KNOWLEDGE_TYPES = {
    "product_specific_procedure",
    "software_operation",
    "paper_experiment",
    "exercise_question",
    "catalog_table",
    "biography_news_marketing",
    "invalid_text",
}
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
REFERENCE_REVIEW_TERMS = (
    "习题", "试题", "练习", "考试", "考研", "学习指导", "复习", "题解",
    "名词解释", "翻译研究", "术语研究", "词典研究", "辞典研究", "编纂研究",
    "小百科", "普及读物", "绘本", "宝宝", "少儿", "儿童", "青少年", "漫画",
    "常识", "图鉴", "magazine", "stories",
)
IMPORTANT_PRIORITY_GENRES = {"HIGHER_EDU_TEXTBOOK", "STANDARD", "HANDBOOK"}
IMPORTANT_REVIEW_GENRES = {
    "COURSEWARE", "POPULAR_SCIENCE", "SOCIAL_COMMENTARY", "K12_TEXTBOOK",
    "BUSINESS_INSIGHT", "GUIDE", "EXAM_PREP", "BIOGRAPHY", "SELF_HELP", "HISTORY",
}
AUDIT_FIELDS = (
    "metadata_decision",
    "metadata_drop_reasons",
    "reference_screen_decision",
    "reference_type",
    "reference_matched_terms",
    "reference_screen_reason",
    "reference_duplicate_of",
    "important_screen_decision",
    "important_screen_reason",
    "book_track",
    "track_evidence",
    "metadata_stratum",
    "pilot_selected",
    "resolved_md_path",
    "audit_status",
    "final_decision",
    "matched_l1_paths",
    "dominant_languages",
    "substantial_non_zh_en",
    "subject_relevance",
    "knowledge_extraction_suitability",
    "document_structure",
    "sample_knowledge_counts",
    "edited_collection_candidate",
    "edited_collection_signals",
    "full_text_signals",
    "full_text_gate_reason",
    "decision_adjusted_by_knowledge_gate",
    "decision_adjusted_by_full_text_gate",
    "decision_adjusted_by_final_safety_gate",
    "ocr_quality",
    "rule_cleanable",
    "periodical_detected",
    "periodical_evidence",
    "audit_summary",
    "audit_evidence",
)


@dataclass(frozen=True)
class TrackDecision:
    track: str
    evidence: tuple[str, ...]
    reference_screen_decision: str
    reference_type: str
    reference_reason: str


@dataclass(frozen=True)
class MetadataDecision:
    decision: str
    drop_reasons: tuple[str, ...]
    track: str
    track_evidence: tuple[str, ...]
    stratum: str
    reference_screen_decision: str
    reference_type: str
    reference_reason: str
    important_screen_decision: str
    important_screen_reason: str

    def as_audit_fields(self) -> dict[str, str]:
        return {
            "metadata_decision": self.decision,
            "metadata_drop_reasons": " | ".join(self.drop_reasons),
            "reference_screen_decision": self.reference_screen_decision,
            "reference_type": self.reference_type,
            "reference_matched_terms": " | ".join(self.track_evidence),
            "reference_screen_reason": self.reference_reason,
            "important_screen_decision": self.important_screen_decision,
            "important_screen_reason": self.important_screen_reason,
            "book_track": self.track,
            "track_evidence": " | ".join(self.track_evidence),
            "metadata_stratum": self.stratum,
            "pilot_selected": "否",
        }


def normalize(value: Any) -> str:
    return str(value or "").strip()


def _identity_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalize(value).casefold())


def detect_periodical_source(row: dict[str, Any], text: str = "") -> tuple[str, ...]:
    """Return conservative signals that identify a periodical issue, not a book."""
    title = normalize(row.get("title"))
    publisher = normalize(row.get("publisher"))
    metadata = "\n".join(
        normalize(row.get(key))
        for key in ("title", "publisher", "abstract", "description", "comments")
    )
    signals: list[str] = []
    if re.search(r"\u6742\u5fd7\u793e|\u671f\u520a\u793e", publisher):
        signals.append("publisher_periodical")
    title_key = _identity_text(title)
    publisher_key = _identity_text(publisher)
    if (
        "\u7f16\u8f91\u90e8" in publisher
        and title_key
        and title_key in publisher_key
        and not _reference_term_hits(title.casefold())
    ):
        signals.append("publisher_editorial_issue")
    if re.search(
        r"(?:\u672c\u520a|\u672c\u671f\u671f\u520a|\u8be5\u671f\u520a|\u6742\u5fd7).{0,40}"
        r"(?:\u7b2c\s*\d+\s*(?:\u5377|\u671f)|\u603b\u7b2c\s*\d+\s*\u671f|\u6708\u520a|\u53cc\u6708\u520a|\u5b63\u520a)",
        metadata,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        signals.append("metadata_periodical")
    head = text[:12000]
    has_issn = bool(re.search(r"\bISSN\s*\d{4}\s*[-\u2013]\s*\d{3}[\dXx]\b", head, re.IGNORECASE))
    has_issue = bool(re.search(
        r"(?:\u7b2c\s*\d+\s*(?:\u5377|\u671f)|\u603b\u7b2c\s*\d+\s*\u671f|"
        r"\u6708\u520a|\u53cc\u6708\u520a|\u5b63\u520a|No\.?\s*\d+\s*\(\s*Total\s+No|"
        r"Vol\.?\s*\d+\s*,?\s*No\.?\s*\d+|Monthly|Bimonthly|Quarterly)",
        head,
        flags=re.IGNORECASE,
    ))
    has_chinese_issue = bool(re.search(
        r"(?:19|20)\d{2}\s*\u5e74\s*\u7b2c\s*\d+\s*\u671f.{0,80}"
        r"(?:\u603b\s*\u7b2c\s*\d+\s*\u671f|\u7b2c\s*\d+\s*\u5377)",
        head,
        flags=re.IGNORECASE | re.DOTALL,
    ))
    if has_chinese_issue:
        signals.append("md_chinese_issue")
    if has_issn and has_issue:
        signals.append("md_issn_issue")
    return tuple(dict.fromkeys(signals))


def analyze_edited_collection_signals(text: str) -> dict[str, Any]:
    normalized = text.casefold()
    editor_count = len(
        re.findall(r"(?im)\b(?:editors?|edited\s+by)\b|主编|编者", text[:80000])
    )
    section_prefix = r"(?:\d+(?:\.\d+)*\.?\s*)?"
    reference_section_count = len(re.findall(
        rf"(?im)^\s*#{{0,6}}\s*{section_prefix}(?:references|bibliography|参考文献)\s*$",
        text,
    ))
    conclusion_section_count = len(re.findall(
        rf"(?im)^\s*#{{0,6}}\s*{section_prefix}(?:conclusions?(?:\s+and\s+[^\n]+)?|"
        r"discussion\s+and\s+conclusion|结论|小结)\s*$",
        text,
    ))
    abstract_section_count = len(re.findall(
        rf"(?im)^\s*#{{0,6}}\s*{section_prefix}(?:abstract|摘要)\s*$", text
    ))
    chapter_heading_count = len(
        re.findall(
            r"(?im)^\s*#{0,6}\s*(?:chapter\s+\d+|第[一二三四五六七八九十百0-9]+章)\b",
            text,
        )
    )
    phrase_candidates = (
        "contributors to this volume",
        "first authors in chapters",
        "applied research conducted by the authors",
        "collection of selected topics",
        "individually important contributions",
        "section editors",
    )
    phrase_hits = [phrase for phrase in phrase_candidates if phrase in normalized]
    explicit_research_authorship = any(
        phrase in phrase_hits
        for phrase in (
            "first authors in chapters",
            "applied research conducted by the authors",
            "collection of selected topics",
            "individually important contributions",
        )
    )
    contributor_chapter_pattern = (
        "contributors to this volume" in phrase_hits
        and reference_section_count >= 3
        and chapter_heading_count >= 5
    )
    lines = text[:120000].splitlines()
    chapter_author_count = 0
    for index, line in enumerate(lines):
        if not re.search(r"(?i)\bchapter\s+\d+\b|第[一二三四五六七八九十百0-9]+章", line):
            continue
        following = [item.strip() for item in lines[index + 1:index + 6] if item.strip()]
        if any(
            not re.search(r"\d", item)
            and len(item) <= 180
            and bool(re.search(r"\b(?:and|&)\b|,", item, flags=re.IGNORECASE))
            and sum(char.isupper() for char in item) >= 6
            for item in following[:2]
        ):
            chapter_author_count += 1

    explicit_collection = explicit_research_authorship and editor_count > 0
    repeated_independent_chapters = (
        editor_count > 0
        and chapter_heading_count >= 4
        and reference_section_count >= 4
        and conclusion_section_count >= 3
        and chapter_author_count >= 3
    )
    high_confidence = (
        (explicit_collection and reference_section_count >= 3)
        or repeated_independent_chapters
    )
    candidate = high_confidence or (
        editor_count > 0
        and (
            contributor_chapter_pattern
            or explicit_research_authorship
            or (
                chapter_heading_count >= 4
                and reference_section_count >= 3
                and conclusion_section_count >= 2
                and chapter_author_count >= 2
            )
        )
    )
    return {
        "edited_collection_candidate": candidate,
        "edited_collection_confidence": "high" if high_confidence else ("medium" if candidate else "low"),
        "editor_marker_count": editor_count,
        "reference_section_count": reference_section_count,
        "conclusion_section_count": conclusion_section_count,
        "abstract_section_count": abstract_section_count,
        "chapter_heading_count": chapter_heading_count,
        "chapter_author_count": chapter_author_count,
        "phrase_hits": phrase_hits,
    }


def _matched_line_evidence(text: str, match: re.Match[str], kind: str) -> dict[str, Any]:
    line_no = text.count("\n", 0, match.start()) + 1
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end < 0:
        line_end = len(text)
    raw_line = text[line_start:line_end]
    relative_start = match.start() - line_start
    window_start = max(0, relative_start - 80)
    anchor = re.sub(r"\s+", " ", raw_line[window_start:window_start + 240]).strip()
    return {"kind": kind, "line": line_no, "anchor": anchor}


def analyze_full_text_signals(text: str, row: dict[str, Any]) -> dict[str, Any]:
    """Extract high-precision risk signals from the complete Markdown text."""
    title = normalize(row.get("title"))
    genre = normalize(row.get("content_genre") or row.get("document_type")).upper()
    line_count = text.count("\n") + 1
    targeted_evidence: list[dict[str, Any]] = []
    hard_drop_reasons: list[str] = []

    year_matches: list[tuple[int, re.Match[str]]] = []
    year_patterns = (
        r"(?i)\b(?:originally|first)\s+published[^\n]{0,120}?(?<!\d)((?:18|19|20)\d{2})(?!\d)",
        r"(?i)\boriginal\s+edition\s+(?:was\s+)?published[^\n]{0,100}?(?<!\d)((?:18|19|20)\d{2})(?!\d)",
        r"(?:原版|初版|首次|最初)[^\n]{0,50}?(?:出版|发行)[^\n]{0,50}?((?:18|19|20)\d{2})年?",
    )
    front_text = "\n".join(text.splitlines()[:800])[:120000]
    non_book_subject = re.compile(
        r"(?i)\b(?:standard|code|equation|formula|model|paper|article|study|"
        r"method|process|algorithm|magazine|journal|classification\s+system)"
        r"\b[^.\n]{0,100}$"
    )
    for pattern in year_patterns:
        for match in re.finditer(pattern, front_text):
            line = match.group(0).casefold()
            if any(marker in line for marker in ("ebook", "electronic", "digitized", "电子版", "数字版")):
                continue
            line_start = front_text.rfind("\n", 0, match.start()) + 1
            prefix = front_text[line_start:match.start()]
            if non_book_subject.search(prefix):
                continue
            year_matches.append((int(match.group(1)), match))
    original_year = min((year for year, _ in year_matches), default=None)
    if original_year is not None:
        match = next(match for year, match in year_matches if year == original_year)
        targeted_evidence.append(
            _matched_line_evidence(front_text, match, "original_publication_year")
        )
        if original_year < 2000:
            hard_drop_reasons.append("original_publication_year_before_2000")

    solution_pattern = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:solution|answer|解答|答案)\s*[:：]?"
    )
    solution_matches = list(solution_pattern.finditer(text))
    exam_context = bool(
        genre == "EXAM_PREP"
        or re.search(r"(?i)\bGATE\b|exam|practice\s+questions?|solved\s+problems?|"
                     r"习题集|题库|试题|考试|考研|考级|题解", title)
    )
    exam_marketing = re.search(
        r"(?i)solved\s+problems?|practice\s+questions?|question\s+bank|"
        r"历年.{0,12}(?:试题|真题)|题库|习题集",
        text[:30000],
    )
    exam_dominated = exam_context and len(solution_matches) >= 50 and bool(exam_marketing)
    if exam_dominated:
        hard_drop_reasons.append("exam_question_answer_dominated")
        targeted_evidence.append(_matched_line_evidence(text, exam_marketing, "exam_dominance"))

    abstract_match = re.search(
        r"(?im)^\s*#{0,6}\s*(?:abstract|摘要)\s*$", text
    )
    reference_match = re.search(
        r"(?im)^\s*#{0,6}\s*(?:\d+(?:\.\d+)*\.?\s*)?"
        r"(?:references|bibliography|参考文献)\s*$",
        text,
    )
    paper_match = re.search(r"(?i)\bthis\s+paper\b|本文(?:提出|研究|介绍|分析|探讨)", text)
    proceedings_match = re.search(
        r"(?i)\bproceedings\b|conference\s+paper|journal\s+article|会议论文|期刊论文",
        text[:60000],
    )
    single_article = bool(
        line_count <= 1200
        and abstract_match
        and reference_match
        and paper_match
        and proceedings_match
    )
    if single_article:
        hard_drop_reasons.append("single_article_or_conference_paper")
        targeted_evidence.append(_matched_line_evidence(text, paper_match, "single_article"))

    collection = analyze_edited_collection_signals(text)
    explicit_collection_phrases = {
        "collection of selected topics",
        "individually important contributions",
        "section editors",
    }
    explicit_collection_statement = explicit_collection_phrases.issubset(
        set(collection.get("phrase_hits") or [])
    )
    if collection["edited_collection_confidence"] == "high":
        if explicit_collection_statement:
            hard_drop_reasons.append("independent_research_chapter_collection")
        phrase = collection.get("phrase_hits") or []
        if phrase:
            match = re.search(re.escape(phrase[0]), text, flags=re.IGNORECASE)
            if match:
                targeted_evidence.append(_matched_line_evidence(text, match, "edited_collection"))

    stop_tokens = {
        "THE", "AND", "FOR", "WITH", "FROM", "GUIDE", "HANDBOOK", "MANUAL",
        "INTRODUCTION", "MECHANICAL", "ENGINEERING", "TECHNOLOGY", "DESIGN",
        "SYSTEM", "SYSTEMS", "TECHNIC", "BUILDER", "UNOFFICIAL",
    }
    title_tokens = [
        token for token in re.findall(r"\b[A-Z][A-Z0-9+-]{2,}\b", title)
        if token not in stop_tokens
    ]
    token_counts = {
        token: len(re.findall(rf"(?i)\b{re.escape(token)}\b", text))
        for token in dict.fromkeys(title_tokens)
    }
    dominant_tokens = [token for token, count in token_counts.items() if count >= 30]
    procedure_count = len(re.findall(
        r"(?i)\b(?:assemble|build|install|repair|replace|connect|step|parts?|pieces?|"
        r"beams?|pins?|gears?)\b|安装|装配|拆卸|更换|维修步骤",
        text,
    ))
    product_context = genre in {"POPULAR_SCIENCE", "GUIDE"} or bool(
        re.search(r"(?i)guide|builder|用户手册|操作指南|维修指南", title)
    )
    product_specific = bool(product_context and dominant_tokens and procedure_count >= 30)
    if product_specific:
        match = re.search(rf"(?i)\b{re.escape(dominant_tokens[0])}\b", text)
        if match:
            targeted_evidence.append(_matched_line_evidence(text, match, "product_specific"))

    return {
        "line_count": line_count,
        "original_publication_year": original_year,
        "original_publication_year_match_count": len(year_matches),
        "exam_solution_heading_count": len(solution_matches),
        "exam_context": exam_context,
        "exam_dominated": exam_dominated,
        "single_article_candidate": single_article,
        "edited_collection_candidate": collection["edited_collection_candidate"],
        "edited_collection_confidence": collection["edited_collection_confidence"],
        "edited_collection_explicit_statement": explicit_collection_statement,
        "edited_collection_signals": collection,
        "product_specific_candidate": product_specific,
        "dominant_title_tokens": dominant_tokens,
        "procedure_marker_count": procedure_count,
        "hard_drop_reasons": list(dict.fromkeys(hard_drop_reasons)),
        "targeted_evidence": targeted_evidence[:6],
    }


def parse_publication_year(value: Any) -> int | None:
    text = normalize(value)
    if not text:
        return None
    match = re.search(r"(?<!\d)(1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)", text)
    return int(match.group(1)) if match else None


def supported_file_type(row: dict[str, Any]) -> bool:
    detail = normalize(row.get("pdf_detail_type"))
    distribution = normalize(row.get("distributionformat")).casefold()
    return detail == "非影印版PDF" or (not detail and distribution == "epub")


def _reference_term_hits(title: str) -> list[tuple[str, str]]:
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


def classify_book_track(row: dict[str, Any]) -> TrackDecision:
    title = normalize(row.get("title")).casefold()
    raw_hits = _reference_term_hits(title)
    if not raw_hits:
        fallback = normalize(row.get("content_genre")) or "高召回候选"
        return TrackDecision("其他重要书籍", (fallback,), "no_hit", "", "题名无辞海类类型词。")
    effective = title.replace("图典型", "")
    effective = re.sub(r"\bdictionary\s+learning\b", "", effective)
    hits = _reference_term_hits(effective)
    if not hits:
        terms = tuple(dict.fromkeys(term for _, term in raw_hits))
        return TrackDecision(
            "其他重要书籍", terms, "false_positive", "", "辞海类类型词属于跨词或专门概念误命中。"
        )
    evidence = tuple(dict.fromkeys(term for _, term in hits))
    reference_type = hits[0][0]
    if any(term in title for term in REFERENCE_REVIEW_TERMS):
        return TrackDecision(
            "辞海类", evidence, "review", reference_type,
            "题名同时具有学习资料、研究对象或大众读物特征，需由 MD 复核。",
        )
    return TrackDecision(
        "辞海类", evidence, "strict_keep", reference_type,
        "题名明确具有辞海、词典、百科或术语型工具书属性。",
    )


def classify_important_book(row: dict[str, Any], track: str) -> tuple[str, str]:
    if track == "辞海类":
        return "not_applicable", "本书由独立辞海题名初筛处理。"
    genre = normalize(row.get("content_genre")).upper()
    category = normalize(row.get("content_category")).upper()
    usage = normalize(row.get("usage_type")).upper()
    audience = normalize(row.get("audience_level")).upper()
    if genre in IMPORTANT_PRIORITY_GENRES:
        return "strict_keep", f"content_genre={genre}，属于重要知识书源优先类型。"
    if category == "KNOWLEDGE_EXPOSITION" and genre not in IMPORTANT_REVIEW_GENRES:
        return "strict_keep", "content_category=KNOWLEDGE_EXPOSITION，具备知识解释潜力。"
    if (
        audience in {"ACADEMIC", "PROFESSIONAL"}
        and ("PROFESSIONAL_USE" in usage or "TEACHING" in usage)
        and genre not in IMPORTANT_REVIEW_GENRES
    ):
        return "strict_keep", "学术/专业受众且具有教学或专业使用信号。"
    reason = f"书型或用途信号需正文确认：content_genre={genre or '空'}。"
    return "review", reason


def metadata_screen_row(row: dict[str, Any]) -> MetadataDecision:
    reasons: list[str] = []
    year = parse_publication_year(row.get("publicationyear"))
    if year is None:
        reasons.append("missing_publicationyear")
    elif year < 2000:
        reasons.append("publicationyear_before_2000")
    if not supported_file_type(row):
        reasons.append("unsupported_file_type")
    language = normalize(row.get("language")).casefold()
    if language not in {"zh", "en"}:
        reasons.append("unsupported_metadata_language")
    if not normalize(row.get("parsed_path")):
        reasons.append("missing_parsed_path")
    track = classify_book_track(row)
    if detect_periodical_source(row):
        reasons.append("periodical_source")
    important_decision, important_reason = classify_important_book(row, track.track)
    file_tier = (
        "non_scanned_pdf"
        if normalize(row.get("pdf_detail_type")) == "非影印版PDF"
        else "epub_exception"
    )
    genre = normalize(row.get("content_genre")) or "unknown_genre"
    stratum = "|".join((language or "unknown", track.track, important_decision, genre, file_tier))
    return MetadataDecision(
        decision="DROP" if reasons else "KEEP",
        drop_reasons=tuple(reasons),
        track=track.track,
        track_evidence=track.evidence,
        stratum=stratum,
        reference_screen_decision=track.reference_screen_decision,
        reference_type=track.reference_type,
        reference_reason=track.reference_reason,
        important_screen_decision=important_decision,
        important_screen_reason=important_reason,
    )


def normalize_l1_boundaries(data: Any) -> list[dict[str, str]]:
    if isinstance(data, dict):
        data = data.get("nodes") or data.get("l1_nodes")
    if not isinstance(data, list) or not data:
        raise ValueError("L1 配置必须是非空数组，或包含 nodes/l1_nodes 数组")
    required = {"name", "path", "node_definition", "node_boundary"}
    output: list[dict[str, str]] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or required - set(item):
            raise ValueError(f"第 {index} 个 L1 节点缺少字段: {sorted(required - set(item or {}))}")
        normalized = {key: normalize(item[key]) for key in required}
        if not all(normalized.values()):
            raise ValueError(f"第 {index} 个 L1 节点存在空字段")
        output.append(normalized)
    return output


def load_l1_boundaries(path: Path) -> list[dict[str, str]]:
    return normalize_l1_boundaries(json.loads(path.read_text(encoding="utf-8-sig")))


def _config_scope_text(value: Any) -> str:
    if isinstance(value, dict):
        return "；".join(
            f"{normalize(key)}：{_config_scope_text(item)}"
            for key, item in value.items()
            if normalize(key) and _config_scope_text(item)
        )
    if isinstance(value, (list, tuple)):
        return "；".join(part for item in value if (part := _config_scope_text(item)))
    return normalize(value)


def _boundary_scope(boundary: dict[str, Any], keys: tuple[str, ...]) -> str:
    return "；".join(
        part for key in keys if (part := _config_scope_text(boundary.get(key)))
    )


def load_subject_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("学科边界附件必须是 JSON 对象")
    subject_name = normalize(data.get("subject_name"))
    boundary = data.get("boundary")
    if not subject_name or not isinstance(boundary, dict):
        raise ValueError("学科边界附件必须包含 subject_name 和 boundary 对象")
    core_scope = _boundary_scope(boundary, (
        "core_scope", "aliases", "direct_subject_labels", "strong_terms",
        "weak_terms", "cooccurrence_groups",
    ))
    if not core_scope:
        raise ValueError("学科边界附件必须提供明确的核心范围")
    normalized_boundary = {
        "core_scope": core_scope,
        "accepted_adjacent": _boundary_scope(
            boundary, ("accepted_adjacent", "adjacent_subject_labels")
        ),
        "excluded_scope": _boundary_scope(
            boundary, ("excluded_scope", "exclude_phrases")
        ),
    }
    embedded_l1 = data.get("l1_nodes") or data.get("nodes")
    return {
        "subject_name": subject_name,
        "boundary": normalized_boundary,
        "l1_nodes": normalize_l1_boundaries(embedded_l1) if embedded_l1 else [],
    }


def _stable_identifier(row: dict[str, Any]) -> str:
    return normalize(row.get("identifier")) or normalize(row.get("parsed_path")) or normalize(row.get("title"))


def normalize_title_key(title: str) -> str:
    value = unicodedata.normalize("NFKC", normalize(title)).casefold()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def annotate_reference_duplicates(rows: list[dict[str, str]]) -> int:
    seen_identifiers: set[str] = set()
    seen_titles: dict[str, str] = {}
    duplicate_count = 0
    for decision in ("strict_keep", "review"):
        for row in rows:
            if row.get("reference_screen_decision") != decision:
                continue
            identifier = normalize(row.get("identifier"))
            title_key = normalize_title_key(row.get("title", ""))
            duplicate_of = ""
            if identifier and identifier in seen_identifiers:
                duplicate_of = f"identifier:{identifier}"
            elif title_key and title_key in seen_titles:
                duplicate_of = f"title:{seen_titles[title_key]}"
            if duplicate_of:
                row["reference_screen_decision"] = "duplicate"
                row["reference_duplicate_of"] = duplicate_of
                duplicate_count += 1
                continue
            if identifier:
                seen_identifiers.add(identifier)
            if title_key:
                seen_titles[title_key] = identifier or title_key
    return duplicate_count


def select_stratified_pilot(
    rows: list[dict[str, str]], sample_size: int, seed: int
) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        key = _stable_identifier(row)
        if key and key not in unique:
            unique[key] = row
    population = list(unique.values())
    if sample_size >= len(population):
        return sorted(population, key=_stable_identifier)
    if sample_size < 1:
        return []

    track_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in population:
        track_groups[normalize(row.get("book_track")) or "unknown"].append(row)
    minimum_track_sample = min(sample_size, min(100, max(10, sample_size // 10)))
    track_quotas = {
        track: max(
            math.floor(sample_size * len(group) / len(population)),
            min(len(group), minimum_track_sample),
        )
        for track, group in track_groups.items()
    }
    while sum(track_quotas.values()) > sample_size:
        reducible = [
            track for track, quota in track_quotas.items()
            if quota > min(len(track_groups[track]), minimum_track_sample)
        ]
        if not reducible:
            reducible = [track for track, quota in track_quotas.items() if quota > 0]
        track = max(reducible, key=lambda key: (track_quotas[key], key))
        track_quotas[track] -= 1
    while sum(track_quotas.values()) < sample_size:
        expandable = [
            track for track, quota in track_quotas.items() if quota < len(track_groups[track])
        ]
        track = max(expandable, key=lambda key: (len(track_groups[key]) - track_quotas[key], key))
        track_quotas[track] += 1

    selected: list[dict[str, str]] = []
    for track in sorted(track_groups):
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in track_groups[track]:
            groups[normalize(row.get("metadata_stratum")) or "unknown"].append(row)
        target = track_quotas[track]
        quotas: dict[str, int] = {}
        fractions: list[tuple[float, str]] = []
        for key, group in groups.items():
            exact = target * len(group) / len(track_groups[track])
            quotas[key] = min(len(group), math.floor(exact))
            fractions.append((exact - math.floor(exact), key))
        remaining = target - sum(quotas.values())
        for _, key in sorted(fractions, key=lambda item: (-item[0], item[1])):
            if remaining <= 0:
                break
            if quotas[key] < len(groups[key]):
                quotas[key] += 1
                remaining -= 1
        for key in sorted(groups):
            group = sorted(groups[key], key=_stable_identifier)
            random.Random(f"{seed}:{key}").shuffle(group)
            selected.extend(group[: quotas[key]])
    return sorted(
        selected,
        key=lambda row: hashlib.sha256(
            f"{seed}:{_stable_identifier(row)}".encode("utf-8")
        ).hexdigest(),
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return fields, list(reader)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = list(dict.fromkeys([*fields, *AUDIT_FIELDS]))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _annotate_metadata_row(row: dict[str, str]) -> dict[str, str]:
    result = metadata_screen_row(row)
    annotated = dict(row)
    annotated.update(result.as_audit_fields())
    return annotated


def _validate_metadata_fields(source_fields: list[str]) -> None:
    required = {
        "identifier", "title", "publicationyear", "pdf_detail_type",
        "distributionformat", "language", "parsed_path",
    }
    missing = required - set(source_fields)
    if missing:
        raise ValueError(f"0611 CSV 缺少字段: {sorted(missing)}")


def _open_stream_writer(
    stack: ExitStack, path: Path, source_fields: list[str]
) -> csv.DictWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = stack.enter_context(path.open("w", encoding="utf-8-sig", newline=""))
    fields = list(dict.fromkeys([*source_fields, *AUDIT_FIELDS]))
    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    return writer


def _run_reference_all_metadata_screen_streaming(
    input_csv: Path,
    output_dir: Path,
    seed: int,
) -> dict[str, Any]:
    reference_rows: list[dict[str, str]] = []
    reason_counts: Counter[str] = Counter()
    input_rows = 0
    metadata_keep = 0
    with input_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        source_fields = list(reader.fieldnames or [])
        _validate_metadata_fields(source_fields)
        for row_index, row in enumerate(reader):
            input_rows += 1
            annotated = _annotate_metadata_row(row)
            if annotated.get("metadata_decision") == "KEEP":
                metadata_keep += 1
            reason_counts.update(
                reason
                for reason in normalize(annotated.get("metadata_drop_reasons")).split(" | ")
                if reason
            )
            if annotated.get("reference_screen_decision") in {"strict_keep", "review"}:
                annotated["_stream_row_index"] = str(row_index)
                reference_rows.append(annotated)

    duplicate_reference_rows = annotate_reference_duplicates(reference_rows)
    reference_overrides = {
        int(row["_stream_row_index"]): (
            row.get("reference_screen_decision", ""),
            row.get("reference_duplicate_of", ""),
        )
        for row in reference_rows
    }
    audit_population = [
        row for row in reference_rows
        if row.get("metadata_decision") == "KEEP"
        and row.get("reference_screen_decision") != "duplicate"
    ]
    pilot = sorted(audit_population, key=_stable_identifier)
    pilot_ids = {_stable_identifier(row) for row in pilot}
    for row in pilot:
        row["pilot_selected"] = "是"

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "MD审核试验样本.csv", pilot, source_fields)
    track_counts: Counter[str] = Counter()
    md_candidate_rows = 0
    second_pass_rows = 0
    with ExitStack() as stack:
        writers = {
            "screened": _open_stream_writer(stack, output_dir / "书目初筛结果.csv", source_fields),
            "reference": _open_stream_writer(
                stack, output_dir / "辞海类" / "初筛" / "题名规则全部候选0611字段.csv", source_fields
            ),
            "reference_md": _open_stream_writer(
                stack, output_dir / "辞海类" / "初筛" / "进入MD审核0611字段.csv", source_fields
            ),
            "reference_duplicate": _open_stream_writer(
                stack, output_dir / "辞海类" / "初筛" / "重复项0611字段.csv", source_fields
            ),
            "important_keep": _open_stream_writer(
                stack, output_dir / "其他重要书籍" / "初筛" / "严格保留0611字段.csv", source_fields
            ),
            "important_review": _open_stream_writer(
                stack, output_dir / "其他重要书籍" / "初筛" / "待复核0611字段.csv", source_fields
            ),
        }
        with input_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_index, row in enumerate(reader):
                second_pass_rows += 1
                annotated = _annotate_metadata_row(row)
                if row_index in reference_overrides:
                    decision, duplicate_of = reference_overrides[row_index]
                    annotated["reference_screen_decision"] = decision
                    annotated["reference_duplicate_of"] = duplicate_of
                if _stable_identifier(annotated) in pilot_ids:
                    annotated["pilot_selected"] = "是"
                writers["screened"].writerow(annotated)

                metadata_decision = annotated.get("metadata_decision")
                reference_decision = annotated.get("reference_screen_decision")
                if reference_decision in {"strict_keep", "review"}:
                    writers["reference"].writerow(annotated)
                    if metadata_decision == "KEEP":
                        writers["reference_md"].writerow(annotated)
                elif reference_decision == "duplicate":
                    writers["reference_duplicate"].writerow(annotated)

                if (
                    metadata_decision == "KEEP"
                    and annotated.get("book_track") == "其他重要书籍"
                ):
                    important_decision = annotated.get("important_screen_decision")
                    if important_decision == "strict_keep":
                        writers["important_keep"].writerow(annotated)
                    elif important_decision == "review":
                        writers["important_review"].writerow(annotated)

                if metadata_decision == "KEEP" and reference_decision != "duplicate":
                    md_candidate_rows += 1
                    track_counts[annotated.get("book_track", "")] += 1

    if second_pass_rows != input_rows:
        raise RuntimeError(
            f"两遍读取行数不一致: first={input_rows}, second={second_pass_rows}"
        )

    reference_candidates = [
        row for row in reference_rows
        if row.get("reference_screen_decision") in {"strict_keep", "review"}
    ]
    reference_md_candidates = [
        row for row in reference_candidates if row.get("metadata_decision") == "KEEP"
    ]
    summary = {
        "input_csv": str(input_csv),
        "input_rows": input_rows,
        "metadata_keep": metadata_keep,
        "metadata_drop": input_rows - metadata_keep,
        "reference_duplicate_rows": duplicate_reference_rows,
        "reference_title_candidates": len(reference_candidates),
        "reference_md_candidates": len(reference_md_candidates),
        "md_candidate_rows": md_candidate_rows,
        "audit_track": "辞海类",
        "audit_population_rows": len(audit_population),
        "audit_all": True,
        "pilot_rows": len(pilot),
        "sample_seed": seed,
        "track_counts": dict(track_counts),
        "drop_reason_counts": dict(reason_counts),
        "streaming_mode": True,
    }
    (output_dir / "书目初筛统计.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_metadata_screen(
    input_csv: Path,
    output_dir: Path,
    sample_size: int = 1000,
    seed: int = 20260903,
    audit_track: str = "全部",
    audit_all: bool = False,
) -> dict[str, Any]:
    if audit_track not in {"全部", *TRACKS}:
        raise ValueError(f"未知审核轨道: {audit_track}")
    if audit_track == "辞海类" and audit_all:
        return _run_reference_all_metadata_screen_streaming(input_csv, output_dir, seed)
    source_fields, source_rows = read_csv(input_csv)
    _validate_metadata_fields(source_fields)
    screened: list[dict[str, str]] = []
    keep_rows: list[dict[str, str]] = []
    for row in source_rows:
        annotated = _annotate_metadata_row(row)
        screened.append(annotated)
        if annotated.get("metadata_decision") == "KEEP":
            keep_rows.append(annotated)
    duplicate_reference_rows = annotate_reference_duplicates(screened)
    md_candidate_rows = [
        row for row in keep_rows if row.get("reference_screen_decision") != "duplicate"
    ]
    audit_population = [
        row for row in md_candidate_rows
        if audit_track == "全部" or row.get("book_track") == audit_track
    ]
    pilot = (
        sorted(audit_population, key=_stable_identifier)
        if audit_all
        else select_stratified_pilot(audit_population, sample_size, seed)
    )
    pilot_ids = {_stable_identifier(row) for row in pilot}
    for row in screened:
        if _stable_identifier(row) in pilot_ids:
            row["pilot_selected"] = "是"
    for row in pilot:
        row["pilot_selected"] = "是"

    write_csv(output_dir / "书目初筛结果.csv", screened, source_fields)
    write_csv(output_dir / "MD审核试验样本.csv", pilot, source_fields)
    reference_candidates = [
        row for row in screened
        if row.get("reference_screen_decision") in {"strict_keep", "review"}
    ]
    reference_duplicates = [
        row for row in screened if row.get("reference_screen_decision") == "duplicate"
    ]
    reference_md_candidates = [
        row for row in reference_candidates if row.get("metadata_decision") == "KEEP"
    ]
    important_candidates = [
        row for row in screened
        if row.get("metadata_decision") == "KEEP" and row.get("book_track") == "其他重要书籍"
    ]
    write_csv(
        output_dir / "辞海类" / "初筛" / "题名规则全部候选0611字段.csv",
        reference_candidates,
        source_fields,
    )
    write_csv(
        output_dir / "辞海类" / "初筛" / "进入MD审核0611字段.csv",
        reference_md_candidates,
        source_fields,
    )
    write_csv(
        output_dir / "辞海类" / "初筛" / "重复项0611字段.csv",
        reference_duplicates,
        source_fields,
    )
    for decision, filename in (("strict_keep", "严格保留0611字段.csv"), ("review", "待复核0611字段.csv")):
        write_csv(
            output_dir / "其他重要书籍" / "初筛" / filename,
            [row for row in important_candidates if row.get("important_screen_decision") == decision],
            source_fields,
        )
    reason_counts = Counter(
        reason
        for row in screened
        for reason in normalize(row.get("metadata_drop_reasons")).split(" | ")
        if reason
    )
    track_counts = Counter(row["book_track"] for row in md_candidate_rows)
    summary = {
        "input_csv": str(input_csv),
        "input_rows": len(source_rows),
        "metadata_keep": len(keep_rows),
        "metadata_drop": len(source_rows) - len(keep_rows),
        "reference_duplicate_rows": duplicate_reference_rows,
        "reference_title_candidates": len(reference_candidates),
        "reference_md_candidates": len(reference_md_candidates),
        "md_candidate_rows": len(md_candidate_rows),
        "audit_track": audit_track,
        "audit_population_rows": len(audit_population),
        "audit_all": audit_all,
        "pilot_rows": len(pilot),
        "sample_seed": seed,
        "track_counts": dict(track_counts),
        "drop_reason_counts": dict(reason_counts),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "书目初筛统计.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def parse_path_mappings(values: list[str] | None) -> dict[str, Path]:
    mappings: dict[str, Path] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"路径映射必须使用 PREFIX=LOCAL_ROOT: {value}")
        prefix, root = value.split("=", 1)
        prefix = prefix.strip()
        if not prefix:
            raise ValueError("路径映射前缀不能为空")
        mappings[prefix] = Path(root).expanduser()
    return mappings


def resolve_parsed_path(uri: str, mappings: dict[str, Path]) -> Path | None:
    raw = normalize(uri)
    if not raw:
        return None
    direct = Path(raw)
    if direct.is_file():
        return direct.resolve()
    for prefix, root in sorted(mappings.items(), key=lambda item: -len(item[0])):
        if raw.startswith(prefix):
            suffix = raw[len(prefix):].lstrip("/\\")
            candidate = root.joinpath(*re.split(r"[/\\]+", suffix))
            if candidate.is_file():
                return candidate.resolve()
    return None


def oss_cache_path(uri: str, cache_dir: Path) -> Path:
    suffix = Path(uri.rstrip("/")).suffix or ".md"
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return cache_dir / digest[:2] / f"{digest}{suffix}"


def parse_oss_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("oss://") or "/" not in uri[6:]:
        raise ValueError(f"无效 OSS 路径: {uri}")
    bucket, key = uri[6:].split("/", 1)
    if not bucket or not key:
        raise ValueError(f"无效 OSS 路径: {uri}")
    return bucket, key


def fetch_oss_path(
    uri: str,
    cache_dir: Path,
    ossutil_config: Path | None,
    source_oss_endpoint: str | None,
) -> Path | None:
    if not uri.startswith("oss://"):
        return None
    target = oss_cache_path(uri, cache_dir)
    if target.is_file():
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_oss_endpoint:
        access_id = normalize(
            os.environ.get("SOURCE_OSS_ACCESS_ID") or os.environ.get("OSS_ACCESS_KEY_ID")
        )
        access_key = normalize(
            os.environ.get("SOURCE_OSS_ACCESS_KEY") or os.environ.get("OSS_ACCESS_KEY_SECRET")
        )
        if access_id and access_key:
            try:
                import oss2

                bucket_name, key = parse_oss_uri(uri)
                bucket = oss2.Bucket(
                    oss2.Auth(access_id, access_key), source_oss_endpoint, bucket_name
                )
                bucket.get_object_to_file(key, str(target))
                if target.is_file() and target.stat().st_size > 0:
                    return target.resolve()
            except Exception:
                target.unlink(missing_ok=True)
    if ossutil_config is not None:
        command = ["ossutil", "cp", "--force", "--config-file", str(ossutil_config), uri, str(target)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if completed.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return target.resolve()
    target.unlink(missing_ok=True)
    return None


def read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def prepare_audit_text(text: str) -> str:
    marker = "\n[EMBEDDED_IMAGE_REMOVED]\n"
    text = re.sub(
        r"<img\b[^>]*\bsrc\s*=\s*(['\"])data:image/.*?\1[^>]*>",
        marker,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"&lt;img\b.*?\bsrc=&quot;data:image/.*?&quot;.*?/?&gt;",
        marker,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"!\[[^\]]*\]\(\s*data:image/[^\r\n)]*\)",
        marker,
        text,
        flags=re.IGNORECASE,
    )
    return text


def distributed_samples(text: str, count: int = 16, chunk_chars: int = 1200) -> list[dict[str, Any]]:
    if not text:
        return [{"sample_no": 1, "line_start": 1, "text": "[EMPTY DOCUMENT]"}]
    count = min(count, max(1, math.ceil(len(text) / chunk_chars)))
    maximum = max(0, len(text) - chunk_chars)
    starts = [0] if count == 1 else [round(i * maximum / (count - 1)) for i in range(count)]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start in starts:
        if start:
            newline = text.find("\n", start, start + 160)
            if newline >= 0:
                start = newline + 1
        end = min(len(text), start + chunk_chars)
        chunk = text[start:end].strip()
        if not chunk or chunk in seen:
            continue
        seen.add(chunk)
        heading_context = ""
        preceding = text[max(0, start - 6000):start]
        headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", preceding))
        if headings:
            heading_context = headings[-1].group(1).strip()[:200]
        output.append({
            "sample_no": len(output) + 1,
            "line_start": text.count("\n", 0, start) + 1,
            "heading_context": heading_context,
            "text": chunk,
        })
    return output


_TRADITIONAL_CONVERTER = None


def traditional_chinese_stats(text: str) -> dict[str, Any]:
    cjk = "".join(char for char in text if "\u4e00" <= char <= "\u9fff")
    if len(cjk) < 500:
        return {"traditional_chinese_body": False, "traditional_ratio": 0.0}
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise RuntimeError("繁体硬门槛需要 opencc-python-reimplemented") from exc
    global _TRADITIONAL_CONVERTER
    if _TRADITIONAL_CONVERTER is None:
        _TRADITIONAL_CONVERTER = OpenCC("t2s")
    simplified = _TRADITIONAL_CONVERTER.convert(cjk)
    changed = sum(left != right for left, right in zip(cjk, simplified))
    ratio = changed / max(1, len(cjk))
    return {
        "traditional_chinese_body": changed >= 500 and ratio >= 0.05,
        "traditional_ratio": round(ratio, 6),
    }


def build_system_prompt(
    track: str,
    subject: str,
    l1_nodes: list[dict[str, str]],
    subject_boundary: dict[str, str] | None = None,
) -> str:
    if track not in TRACKS:
        raise ValueError(f"未知书型: {track}")
    if subject_boundary:
        boundary_text = (
            f"- 核心范围：{subject_boundary.get('core_scope') or '未提供'}\n"
            f"- 可接受相邻范围：{subject_boundary.get('accepted_adjacent') or '未提供'}\n"
            f"- 明确排除范围：{subject_boundary.get('excluded_scope') or '未提供'}"
        )
    else:
        boundary_text = "未提供明确边界附件，仅依据学科名称判断。"
    if l1_nodes:
        l1_text = "\n".join(
            f"- {node['path']}：{node['node_definition']} 边界：{node['node_boundary']}"
            for node in l1_nodes
        )
        subject_scope_rule = (
            "先遵循明确的学科边界，再匹配 L1。与学科相关但 L1 覆盖不充分时 REVIEW，"
            "不因树边界不足直接 DROP。"
        )
    else:
        l1_text = (
            f"未提供L1节点。本轮仅判断是否属于{subject}领域，"
            "matched_l1_paths 必须返回空数组，不因该数组为空而降级。"
        )
        if subject_boundary:
            subject_scope_rule = (
                "未提供L1时，严格依据下方明确学科边界判断相关性和知识抽取适用性；"
                "不因 matched_l1_paths 为空而降级。"
            )
        else:
            subject_scope_rule = (
                f"未提供L1时，仅判断是否属于{subject}领域以及是否适合知识抽取；"
                "不得因为 matched_l1_paths 为空而降级。"
            )
    if track == "辞海类":
        structure_rule = (
            "本书按辞海类审核。允许只有双语词条对照而无扩展解释；允许词头对应多段、多页的百科长释义。"
            "必须反复出现可识别的词头或主题条目，并且内容能归属于相应词头；不能只凭书名判断。"
            "逐段标注结构：compact_entries 表示词头边界清楚且翻译或长短释义明确归属于该词头；"
            "long_article 表示连续章节、论文或叙事，缺少可重复词条边界；frontmatter_index 表示前言、目录、"
            "索引或参考文献；unclear 表示无法判断。排除 frontmatter_index 后，long_article 达一半时必须 DROP；"
            "compact_entries 不足三分之二或 long_article 超过五分之一时不能 PASS。"
        )
    else:
        structure_rule = (
            "本书按其他重要书籍审核，不要求辞海词条结构。教材、标准、规范、技术手册、专业专著和方法书，"
            "只有正文多数片段能直接形成学科概念、定义、原理、方法、参数或关系，且无需重建论文上下文，"
            "才可通过。判断依据必须是抽样正文的主导结构，不能仅因文档标注为博士论文、技术报告，或章节"
            "曾单独发表就 DROP；若其正文仍以系统定义、原理和方法说明为主，应按专著处理。只有正文主要由"
            "独立研究论文或会议论文彼此拼接，或由特定实验结果、新闻文摘、题目答案、宣传材料、零散案例组成时，"
            "才因知识点抽取不便而 DROP。案例不应单独降级：概念和通用方法占主体、案例仅用于说明时可 PASS。"
            "考试辅导书若以成体系的考点解释为主、题目可规则过滤，仍可 PASS。逐段结构可标为 "
            "structured_exposition；辞海结构字段填 not_applicable，不据此降级。"
            "用户输入中的 structural_signals 是对完整 MD 的结构计数。若出现编辑者、分章独立作者、每章"
            "参考文献或明确的研究合集措辞，必须核实它是否属于多作者独立论文/研究章节合集；确认后将 "
            "document_structure 标为 edited_research_collection 并 DROP。不能仅凭有 editor 或参考文献"
            "就排除普通教材、标准或系统性手册。用户输入中的 full_text_signals 来自完整 MD 的确定性扫描，"
            "targeted_evidence 是相应原文锚点；必须逐项核对这些全文高风险信号，不能因均匀抽样片段看似正常"
            "而忽略原版年份、题库密度、单篇论文、独立研究章节合集或产品专用内容。"
        )
    knowledge_type_rule = (
        "还必须逐段标注知识形态，且每段只能选择一个主导类型：dictionary_entry=词头与释义；"
        "general_knowledge=可脱离当前书页直接复用的概念、定义、原理、方法、参数或关系；"
        "product_specific_procedure=特定厂商、型号或设备的安装、检修、校准步骤；"
        "software_operation=特定软件版本的菜单、按钮、文件与操作步骤；"
        "paper_experiment=独立论文、特定实验、数据结果或参考文献链；"
        "exercise_question=习题、试题、答案或评分表；catalog_table=产品目录、型号或规格表；"
        "biography_news_marketing=传记、新闻、市场宣传或行业叙事；frontmatter_index=封面、目录、"
        "索引或参考文献；invalid_text=乱码、空表或语义严重丢失；unclear=证据不足。"
        "不能因为内容属于机械工程，就把产品专用步骤或软件操作标为 general_knowledge。"
    )
    return f"""
你是面向知识点抽取与分类树挂载的书籍 Markdown 质量审核员。学科为“{subject}”。
请完整阅读全部均匀抽样片段，独立判断正文语义、OCR/版面质量、学科相关性、知识密度和 L1 挂载可行性。
每个片段的 heading_context 是该位置最近的上级 Markdown 标题，可用于判断长正文是否仍属于一个明确词头；它为空时不要臆造词头。

{structure_rule}

{knowledge_type_rule}

硬规则：
1. 正文主语言只允许简体中文、英文或两者混合；繁体正文主体或大量其他语言必须 DROP。只要抽样中存在成段、成栏或反复出现的法语、德语、俄语、日语等正文，即使 dominant_languages 只列主要语言，也必须令 substantial_non_zh_en=true。
2. 页眉页脚、页码、固定 HTML 标签等可规则清洗噪声不应直接 DROP。
3. 大面积乱码、重复、串栏、漏文、词头释义错配或语义不可恢复时 DROP。
4. {subject_scope_rule}
5. 只有确实无法判断能否 PASS 时才 REVIEW；局部可规则处理的问题可 PASS。
6. 从封面或版权页识别原始出版年份，填入 original_publication_year；没有明确证据则填 null。你只负责提取年份，不要自行比较或据此改变 decision；程序会判断原始出版年份早于 2000 年的情况。电子版发布或上传年份不算原始出版年份。

学科边界：
{boundary_text}

L1 节点：
{l1_text}

只输出一个 JSON 对象：
{{
  "decision": "PASS|REVIEW|DROP",
  "dominant_languages": ["zh", "en"],
  "substantial_non_zh_en": false,
  "matched_l1_paths": ["L1 path"],
  "subject_relevance": "high|medium|low",
  "knowledge_extraction_suitability": "high|medium|low",
  "document_structure": "dictionary_entries|encyclopedia_entries|textbook|standard|handbook|monograph|edited_research_collection|mixed|other",
  "dictionary_structure": "clear|mixed|absent|not_applicable",
  "entry_definition_alignment": "high|medium|low|not_applicable",
  "continuous_prose_dominant": false,
  "original_publication_year": 2020,
  "sample_structure_labels": [
    {{"sample_no": 1, "structure": "compact_entries|long_article|structured_exposition|frontmatter_index|unclear"}}
  ],
  "sample_knowledge_labels": [
    {{"sample_no": 1, "knowledge_type": "dictionary_entry|general_knowledge|product_specific_procedure|software_operation|paper_experiment|exercise_question|catalog_table|biography_news_marketing|frontmatter_index|invalid_text|unclear"}}
  ],
  "ocr_quality": "high|medium|low",
  "rule_cleanable": true,
  "summary": "中文简要结论",
  "evidence": [{{"sample_no": 1, "anchor": "不超过20字的原文", "issue": "简要说明"}}],
  "confidence": 0.0
}}
PASS 的 evidence 可为空；REVIEW/DROP 至少给一条真实锚点。不要声称看过 PDF。
所有 JSON 字符串内容中不要使用未转义的英文双引号；需要引用时改用中文引号“”。
""".strip()


def normalize_structure_label(value: str, track: str) -> str:
    allowed = {
        "compact_entries", "long_article", "structured_exposition",
        "frontmatter_index", "unclear",
    }
    normalized = normalize(value)
    aliases = {
        "dictionary_entry": "compact_entries",
        "dictionary_entries": "compact_entries",
        "encyclopedia_entry": "long_article",
        "encyclopedia_entries": "long_article",
        "continuous_prose": "long_article",
        "prose": "long_article",
        "index": "frontmatter_index",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in allowed:
        return normalized
    if normalized == "mixed":
        return "unclear"
    if normalized == "catalog_table":
        return "structured_exposition"
    if track == "其他重要书籍":
        return "structured_exposition"
    return "unclear"


def normalize_knowledge_label(value: str) -> str:
    normalized = normalize(value).casefold()
    aliases = {
        "product_procedure": "product_specific_procedure",
        "specific_procedure": "product_specific_procedure",
        "software_steps": "software_operation",
        "paper": "paper_experiment",
        "experiment": "paper_experiment",
        "exercise": "exercise_question",
        "catalog": "catalog_table",
        "frontmatter": "frontmatter_index",
        "invalid": "invalid_text",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in KNOWLEDGE_TYPES:
        raise ValueError("无效逐段知识形态标签")
    return normalized


def parse_llm_result(
    content: str,
    l1_paths: set[str],
    expected_sample_count: int | None = None,
    track: str = "辞海类",
) -> dict[str, Any]:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("响应不含 JSON 对象")
    result = json.loads(content[start : end + 1])
    required = {
        "decision", "dominant_languages", "substantial_non_zh_en", "matched_l1_paths", "subject_relevance",
        "knowledge_extraction_suitability", "document_structure", "ocr_quality",
        "dictionary_structure", "entry_definition_alignment", "continuous_prose_dominant",
        "sample_structure_labels", "sample_knowledge_labels", "rule_cleanable", "summary",
        "evidence", "confidence",
    }
    missing = required - set(result)
    if missing:
        raise ValueError(f"响应缺少字段: {sorted(missing)}")
    if result["decision"] not in DECISIONS:
        raise ValueError("无效 decision")
    if not isinstance(result["dominant_languages"], list) or not result["dominant_languages"]:
        raise ValueError("dominant_languages 必须是非空数组")
    if not isinstance(result["substantial_non_zh_en"], bool):
        raise ValueError("substantial_non_zh_en 必须是布尔值")
    if not isinstance(result["matched_l1_paths"], list):
        raise ValueError("matched_l1_paths 必须是数组")
    result["matched_l1_paths"] = [path for path in result["matched_l1_paths"] if path in l1_paths]
    if result["dictionary_structure"] not in {"clear", "mixed", "absent", "not_applicable"}:
        raise ValueError("无效 dictionary_structure")
    if result["entry_definition_alignment"] not in {"high", "medium", "low", "not_applicable"}:
        raise ValueError("无效 entry_definition_alignment")
    original_year = result.get("original_publication_year")
    if original_year in ("", None):
        result["original_publication_year"] = None
    else:
        try:
            result["original_publication_year"] = int(original_year)
        except (TypeError, ValueError) as exc:
            raise ValueError("original_publication_year 必须是年份整数或 null") from exc
    if not isinstance(result["continuous_prose_dominant"], bool):
        raise ValueError("continuous_prose_dominant 必须是布尔值")
    labels = result["sample_structure_labels"]
    if not isinstance(labels, list) or not labels:
        raise ValueError("sample_structure_labels 必须是非空数组")
    sample_numbers: list[int] = []
    normalized_structure_labels: list[dict[str, Any]] = []
    for index, label in enumerate(labels, start=1):
        if isinstance(label, str):
            label = {"sample_no": index, "structure": label}
        if not isinstance(label, dict) or not {"sample_no", "structure"}.issubset(label):
            raise ValueError("无效 sample_structure_labels 项")
        if not isinstance(label["sample_no"], int):
            raise ValueError("无效逐段结构标签")
        label = {
            "sample_no": label["sample_no"],
            "structure": normalize_structure_label(label["structure"], track),
        }
        normalized_structure_labels.append(label)
        sample_numbers.append(label["sample_no"])
    result["sample_structure_labels"] = normalized_structure_labels
    if len(sample_numbers) != len(set(sample_numbers)):
        raise ValueError("逐段结构标签 sample_no 重复")
    if expected_sample_count is not None and set(sample_numbers) != set(range(1, expected_sample_count + 1)):
        raise ValueError("逐段结构标签未完整覆盖输入片段")
    knowledge_labels = result["sample_knowledge_labels"]
    if not isinstance(knowledge_labels, list) or not knowledge_labels:
        raise ValueError("sample_knowledge_labels 必须是非空数组")
    knowledge_sample_numbers: list[int] = []
    normalized_knowledge_labels: list[dict[str, Any]] = []
    for index, label in enumerate(knowledge_labels, start=1):
        if isinstance(label, str):
            label = {"sample_no": index, "knowledge_type": label}
        if not isinstance(label, dict) or not {"sample_no", "knowledge_type"}.issubset(label):
            raise ValueError("无效 sample_knowledge_labels 项")
        if not isinstance(label["sample_no"], int):
            raise ValueError("无效逐段知识形态标签")
        label = {
            "sample_no": label["sample_no"],
            "knowledge_type": normalize_knowledge_label(label["knowledge_type"]),
        }
        normalized_knowledge_labels.append(label)
        knowledge_sample_numbers.append(label["sample_no"])
    result["sample_knowledge_labels"] = normalized_knowledge_labels
    if len(knowledge_sample_numbers) != len(set(knowledge_sample_numbers)):
        raise ValueError("逐段知识形态标签 sample_no 重复")
    if (
        expected_sample_count is not None
        and set(knowledge_sample_numbers) != set(range(1, expected_sample_count + 1))
    ):
        raise ValueError("知识形态标签未完整覆盖输入片段")
    if result["decision"] != "PASS" and not result["evidence"]:
        raise ValueError("REVIEW/DROP 必须提供证据")
    return result


def apply_track_structure_gates(
    result: dict[str, Any], track: str, expected_sample_count: int
) -> dict[str, Any]:
    if track != "辞海类":
        result["decision_adjusted_by_structure_gate"] = False
        return result
    labels = result.get("sample_structure_labels") or []
    sample_numbers = [label.get("sample_no") for label in labels]
    if set(sample_numbers) != set(range(1, expected_sample_count + 1)):
        raise ValueError("辞海类逐段结构标签未完整覆盖输入片段")
    counts = Counter(label["structure"] for label in labels)
    substantive = len(labels) - counts["frontmatter_index"]
    compact = counts["compact_entries"]
    long_article = counts["long_article"]
    long_dominant = substantive > 0 and long_article >= 3 and long_article * 2 >= substantive
    clear_encyclopedia = (
        result.get("document_structure") == "encyclopedia_entries"
        and result.get("dictionary_structure") == "clear"
        and result.get("entry_definition_alignment") == "high"
    )
    pass_allowed = (
        substantive >= max(4, math.ceil(len(labels) / 2))
        and (
            clear_encyclopedia
            or (compact * 3 >= substantive * 2 and long_article * 5 <= substantive)
        )
        and result.get("dictionary_structure") == "clear"
        and result.get("entry_definition_alignment") == "high"
        and (not result.get("continuous_prose_dominant") or clear_encyclopedia)
    )
    hard_drop = (
        result.get("dictionary_structure") == "absent"
        or result.get("entry_definition_alignment") == "low"
        or (result.get("continuous_prose_dominant") and not clear_encyclopedia)
        or (long_dominant and not clear_encyclopedia)
    )
    original = result["decision"]
    if hard_drop:
        result["decision"] = "DROP"
    elif original == "PASS" and not pass_allowed:
        result["decision"] = "REVIEW"
    result["decision_adjusted_by_structure_gate"] = result["decision"] != original
    result["sample_structure_counts"] = dict(counts)
    if result["decision_adjusted_by_structure_gate"]:
        result["summary"] = (
            f"[辞海结构门槛 {compact}/{substantive} 个有效片段为词条结构，"
            f"{long_article}/{substantive} 个为长篇正文] " + normalize(result.get("summary"))
        )
        if not result.get("evidence"):
            sample_no = next(
                (label["sample_no"] for label in labels if label["structure"] in {"long_article", "unclear"}),
                1,
            )
            result["evidence"] = [{
                "sample_no": sample_no,
                "anchor": "逐段结构标签",
                "issue": "辞海类有效词条片段比例未通过程序门槛",
            }]
    return result


def apply_knowledge_extraction_gates(
    result: dict[str, Any], track: str, expected_sample_count: int
) -> dict[str, Any]:
    if track != "其他重要书籍":
        result["decision_adjusted_by_knowledge_gate"] = False
        return result
    labels = result.get("sample_knowledge_labels") or []
    sample_numbers = [label.get("sample_no") for label in labels]
    if set(sample_numbers) != set(range(1, expected_sample_count + 1)):
        raise ValueError("重要书籍知识形态标签未完整覆盖输入片段")

    counts = Counter(label["knowledge_type"] for label in labels)
    substantive = len(labels) - counts["frontmatter_index"]
    reusable = counts["general_knowledge"] + counts["dictionary_entry"]
    document_structure = result.get("document_structure")
    contextual_low_types = set(LOW_UTILITY_KNOWLEDGE_TYPES)
    contextual_reusable = reusable
    if document_structure == "standard":
        contextual_low_types -= {"catalog_table", "exercise_question"}
        contextual_reusable += counts["catalog_table"]
    elif document_structure == "textbook":
        contextual_low_types.discard("exercise_question")
    low_utility = sum(counts[value] for value in contextual_low_types)
    low_utility_dominant = (
        substantive >= max(4, math.ceil(len(labels) / 2))
        and low_utility * 3 >= substantive * 2
    )
    hard_drop_count = sum(
        counts[value]
        for value in {
            "paper_experiment",
            "exercise_question",
            "biography_news_marketing",
            "invalid_text",
        }
    )
    hard_drop_dominant = (
        substantive >= max(4, math.ceil(len(labels) / 2))
        and hard_drop_count * 3 >= substantive * 2
    )
    strong_reusable = (
        substantive >= max(4, math.ceil(len(labels) / 2))
        and contextual_reusable * 3 >= substantive * 2
        and low_utility * 5 <= substantive
        and result.get("subject_relevance") == "high"
        and result.get("ocr_quality") != "low"
    )

    original = result["decision"]
    pass_retention = (
        substantive >= max(4, math.ceil(len(labels) / 2))
        and contextual_reusable * 5 >= substantive * 3
        and low_utility * 5 <= substantive * 2
        and (counts["software_operation"] + counts["product_specific_procedure"]) * 4
        <= substantive
        and counts["invalid_text"] * 3 <= substantive
        and result.get("subject_relevance") == "high"
        and result.get("ocr_quality") != "low"
    )
    if original != "DROP" and hard_drop_dominant:
        result["decision"] = "DROP"
    elif original != "DROP" and low_utility_dominant:
        # 产品专用或软件操作占优只证明复用性存疑，不能排除书中另有系统理论章节。
        result["decision"] = "REVIEW"
    elif original == "PASS" and not pass_retention:
        result["decision"] = "REVIEW"
    elif original == "REVIEW" and strong_reusable:
        result["decision"] = "PASS"

    result["decision_adjusted_by_knowledge_gate"] = result["decision"] != original
    result["sample_knowledge_counts"] = dict(counts)
    if result["decision_adjusted_by_knowledge_gate"]:
        result["summary"] = (
            f"[知识形态门槛 reusable={contextual_reusable}/{substantive}, "
            f"low_utility={low_utility}/{substantive}] "
            + normalize(result.get("summary"))
        )
        if not result.get("evidence"):
            sample_no = next(
                (
                    label["sample_no"]
                    for label in labels
                    if label["knowledge_type"] in LOW_UTILITY_KNOWLEDGE_TYPES | {"unclear"}
                ),
                1,
            )
            result["evidence"] = [{
                "sample_no": sample_no,
                "anchor": "逐段知识形态标签",
                "issue": "可复用知识与低效内容比例触发程序门槛",
            }]
    return result


def apply_md_hard_gates(
    result: dict[str, Any], *, traditional_chinese_body: bool,
    dominant_languages: list[str], substantial_non_zh_en: bool = False,
) -> dict[str, Any]:
    languages = {normalize(value).casefold() for value in dominant_languages}
    foreign = substantial_non_zh_en or bool(languages - {"zh", "en"})
    if not traditional_chinese_body and not foreign:
        return result
    reason = "繁体正文主体" if traditional_chinese_body else "正文主语言含非中英文"
    result["decision_before_language_gate"] = result.get("decision", "")
    result["decision"] = "DROP"
    result["rule_cleanable"] = False
    result["summary"] = f"[语言硬门槛] {reason}。" + normalize(result.get("summary"))
    evidence = result.setdefault("evidence", [])
    if not evidence:
        evidence.append({"sample_no": 1, "anchor": reason, "issue": reason})
    return result


def title_indicates_non_zh_en_dictionary(title: str) -> bool:
    normalized = normalize(title).casefold()
    dictionary_context = bool(
        re.search(r"词典|辞典|字典|术语|词汇|对照|双解|dictionary|glossary|lexicon", normalized)
    )
    if not dictionary_context:
        return False
    chinese_markers = (
        "德汉", "汉德", "法汉", "汉法", "俄汉", "汉俄", "日汉", "汉日",
        "韩汉", "汉韩", "意汉", "汉意", "葡汉", "汉葡", "阿汉", "汉阿",
    )
    english_markers = (
        "german-chinese", "chinese-german", "french-chinese", "chinese-french",
        "russian-chinese", "chinese-russian", "japanese-chinese", "chinese-japanese",
        "korean-chinese", "chinese-korean",
    )
    return any(marker in normalized for marker in chinese_markers + english_markers)


def apply_final_safety_gates(result: dict[str, Any], row: dict[str, str]) -> dict[str, Any]:
    labels = result.get("sample_knowledge_labels") or []
    counts = Counter(label.get("knowledge_type") for label in labels)
    substantive = len(labels) - counts["frontmatter_index"]
    software_or_product = counts["software_operation"] + counts["product_specific_procedure"]
    hard_drop_count = sum(
        counts[value]
        for value in {
            "paper_experiment",
            "exercise_question",
            "biography_news_marketing",
            "invalid_text",
        }
    )
    year = result.get("original_publication_year")
    year_allowed = year is None or (isinstance(year, int) and year >= 2000)
    recoverable_software_textbook = (
        row.get("book_track") == "其他重要书籍"
        and result.get("decision") == "DROP"
        and result.get("subject_relevance") in {"high", "medium"}
        and result.get("document_structure") == "textbook"
        and substantive >= 4
        and software_or_product * 3 >= substantive * 2
        and hard_drop_count * 3 < substantive * 2
        and year_allowed
        and "decision_before_language_gate" not in result
        and not result.get("full_text_hard_drop_applied")
    )
    if recoverable_software_textbook:
        result["decision"] = "REVIEW"
        result["decision_adjusted_by_final_safety_gate"] = True
        result["summary"] = (
            "[最终安全门槛] 学科相关软件/产品教材可能漏采理论章节，DROP转人工复核。"
            + normalize(result.get("summary"))
        )

    collection_signals = result.get("edited_collection_signals") or {}
    if (
        row.get("book_track") == "其他重要书籍"
        and collection_signals.get("edited_collection_candidate")
        and not result.get("edited_collection_gate_applied")
    ):
        confirmed = result.get("document_structure") == "edited_research_collection"
        if confirmed:
            changed = result.get("decision") != "DROP"
            result["decision"] = "DROP"
            result["decision_adjusted_by_final_safety_gate"] = (
                bool(result.get("decision_adjusted_by_final_safety_gate")) or changed
            )
            result["edited_collection_gate_applied"] = True
            result["summary"] = (
                "[论文合集结构门槛] 全文结构信号与 document_structure 字段均指向"
                "多作者独立研究章节合集，予以排除。"
            )
        elif not result.get("edited_collection_gate_applied"):
            changed = result.get("decision") != "REVIEW"
            result["decision"] = "REVIEW"
            result["decision_adjusted_by_final_safety_gate"] = (
                bool(result.get("decision_adjusted_by_final_safety_gate")) or changed
            )
            result["edited_collection_gate_applied"] = True
            result["summary"] = (
                "[论文合集结构门槛] 全文存在独立研究章节合集信号，模型未确认，转人工复核。"
                + normalize(result.get("summary"))
            )
        if not result.get("evidence"):
            phrase_hits = collection_signals.get("phrase_hits") or []
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": normalize(phrase_hits[0] if phrase_hits else "edited collection")[:20],
                "issue": "全文结构信号显示可能为多作者独立研究章节合集",
            }]

    if (
        title_indicates_non_zh_en_dictionary(row.get("title", ""))
        and not result.get("title_language_gate_applied")
    ):
        changed = result.get("decision") != "DROP"
        result["decision"] = "DROP"
        result["rule_cleanable"] = False
        result["decision_adjusted_by_final_safety_gate"] = (
            bool(result.get("decision_adjusted_by_final_safety_gate")) or changed
        )
        result["title_language_gate_applied"] = True
        result["summary"] = (
            "[标题语言硬门槛] 明确为非中英文双语词典。" + normalize(result.get("summary"))
        )
        if not result.get("evidence"):
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": normalize(row.get("title"))[:20],
                "issue": "书名明确表明主体包含非中英文词条",
            }]
    return result


def apply_subject_gates(
    result: dict[str, Any], *, require_l1: bool = True
) -> dict[str, Any]:
    original = result["decision"]
    relevance = result.get("subject_relevance")
    suitability = result.get("knowledge_extraction_suitability")
    matched = result.get("matched_l1_paths") or []
    if relevance == "low" or suitability == "low":
        result["decision"] = "DROP"
    elif original == "PASS" and (
        relevance != "high" or suitability != "high" or (require_l1 and not matched)
    ):
        result["decision"] = "REVIEW"
    result["decision_adjusted_by_subject_gate"] = result["decision"] != original
    if result["decision_adjusted_by_subject_gate"]:
        result["summary"] = (
            f"[学科挂载门槛 relevance={relevance}, suitability={suitability}, "
            f"matched_l1={len(matched)}] " + normalize(result.get("summary"))
        )
        if not result.get("evidence"):
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": "学科挂载字段",
                "issue": "模型总评与学科相关性、抽取适用性或 L1 命中字段不一致",
            }]
    return result


def apply_original_year_gate(result: dict[str, Any]) -> dict[str, Any]:
    year = result.get("original_publication_year")
    if year is None:
        return result
    try:
        year = int(year)
    except (TypeError, ValueError):
        return result
    result["original_publication_year"] = year
    if year < 2000:
        original = result.get("decision")
        result["decision"] = "DROP"
        result["decision_adjusted_by_original_year_gate"] = original != "DROP"
        result["summary"] = f"[原始出版年份门槛 year={year}] " + normalize(result.get("summary"))
        if not result.get("evidence"):
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": str(year),
                "issue": "原始出版年份早于2000年",
            }]
    else:
        result["decision_adjusted_by_original_year_gate"] = False
    return result


def apply_full_text_signal_gates(
    result: dict[str, Any], row: dict[str, Any], signals: dict[str, Any]
) -> dict[str, Any]:
    """Fuse LLM judgment with high-precision evidence from the complete MD."""
    if result.get("full_text_signal_gate_applied"):
        return result
    original = result.get("decision", "REVIEW")
    hard_reasons = list(dict.fromkeys(signals.get("hard_drop_reasons") or []))
    collection_candidate = bool(signals.get("edited_collection_candidate"))
    collection_confirmed = (
        collection_candidate
        and result.get("document_structure") == "edited_research_collection"
    )
    if (
        collection_confirmed
        and "independent_research_chapter_collection" not in hard_reasons
    ):
        hard_reasons.append("independent_research_chapter_collection")
    unresolved_risks: list[str] = []
    if signals.get("product_specific_candidate"):
        unresolved_risks.append("product_specific_candidate")
    if (
        collection_candidate and not collection_confirmed
    ):
        unresolved_risks.append("edited_collection_candidate")

    if hard_reasons:
        result["decision"] = "DROP"
        result["rule_cleanable"] = False
        result["full_text_hard_drop_applied"] = True
        if "independent_research_chapter_collection" in hard_reasons:
            result["edited_collection_gate_applied"] = True
        reason_text = " | ".join(hard_reasons)
        reason_messages = {
            "original_publication_year_before_2000": "全文识别到明确的原始出版年份早于2000年",
            "exam_question_answer_dominated": "完整MD中题目与答案结构高度重复，正文由考试训练内容主导",
            "single_article_or_conference_paper": "完整MD符合单篇论文或会议论文结构，不属于系统性书籍正文",
            "independent_research_chapter_collection": "完整MD符合多作者独立研究章节合集结构",
        }
        result["summary"] = "[全文硬证据门槛] " + "；".join(
            reason_messages.get(reason, reason) for reason in hard_reasons
        ) + "，予以排除。"
        result["full_text_gate_reason"] = reason_text
    elif unresolved_risks and result.get("decision") == "PASS":
        result["decision"] = "REVIEW"
        reason_text = " | ".join(unresolved_risks)
        result["summary"] = (
            f"[全文风险冲突 {reason_text}] 模型抽样结论不足以直接通过，转人工复核。"
            + normalize(result.get("summary"))
        )
        result["full_text_gate_reason"] = reason_text
        if "edited_collection_candidate" in unresolved_risks:
            result["edited_collection_gate_applied"] = True
    else:
        result["full_text_gate_reason"] = ""

    result["decision_adjusted_by_full_text_gate"] = result.get("decision") != original
    result["full_text_signal_gate_applied"] = True
    if result.get("decision_adjusted_by_full_text_gate") and not result.get("evidence"):
        evidence = signals.get("targeted_evidence") or []
        if evidence:
            first = evidence[0]
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": normalize(first.get("anchor"))[:20],
                "issue": f"全文第{first.get('line', '?')}行命中{first.get('kind', '风险')}信号",
            }]
        else:
            result["evidence"] = [{
                "sample_no": 1,
                "anchor": normalize(row.get("title"))[:20],
                "issue": "完整 MD 的确定性扫描命中高风险信号",
            }]
    return result


def _compact_prompt_text(text: str) -> str:
    text = re.sub(
        r"[A-Za-z0-9+/=]{160,}",
        lambda match: match.group(0)[:20] + f"【连续{len(match.group(0))}字符已压缩】",
        text,
    )
    return re.sub(
        r"([^\w\s])\1{79,}",
        lambda match: match.group(0)[:20] + f"【连续{len(match.group(0))}字符已压缩】",
        text,
    )


def request_llm(
    api_url: str,
    model: str,
    prompt: str,
    payload: dict[str, Any],
    l1_paths: set[str],
    timeout: int,
    retries: int,
    max_tokens: int,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    error = ""
    for attempt in range(1, retries + 2):
        try:
            headers = {"Content-Type": "application/json"}
            api_key = normalize(os.environ.get("KNOWLEDGE_LABELING_API_KEY") or os.environ.get("ZJLAB_API_KEY"))
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            request = urllib.request.Request(api_url, data=encoded, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                obj = json.loads(response.read().decode("utf-8"))
            content = obj["choices"][0]["message"]["content"]
            return {
                "ok": True,
                "result": parse_llm_result(
                    content, l1_paths, len(payload["samples"]), payload["book_track"]
                ),
                "usage": obj.get("usage"),
                "attempt": attempt,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 5))
    return {"ok": False, "error": error, "attempt": retries + 1}


def audit_one(
    row: dict[str, str],
    *,
    subject: str,
    l1_nodes: list[dict[str, str]],
    subject_boundary: dict[str, str] | None = None,
    mappings: dict[str, Path],
    cache_dir: Path,
    ossutil_config: Path | None,
    source_oss_endpoint: str | None,
    api_url: str,
    model: str,
    sample_count: int,
    chunk_chars: int,
    timeout: int,
    retries: int,
    max_tokens: int,
) -> dict[str, Any]:
    uri = normalize(row.get("parsed_path"))
    path = resolve_parsed_path(uri, mappings)
    if path is None:
        path = fetch_oss_path(uri, cache_dir, ossutil_config, source_oss_endpoint)
    base = {"identifier": _stable_identifier(row), "source_row": row}
    if path is None:
        return {
            **base,
            "ok": False,
            "audit_status": "unresolved_md_path",
            "error": "parsed_path 无本地映射且 OSS 获取不可用或失败",
        }
    text = read_text(path)
    periodical_signals = detect_periodical_source(row, text)
    if periodical_signals:
        return {
            **base,
            "ok": True,
            "result": {
                "decision": "DROP",
                "matched_l1_paths": [],
                "dominant_languages": [],
                "substantial_non_zh_en": False,
                "subject_relevance": "",
                "knowledge_extraction_suitability": "",
                "document_structure": "periodical_issue",
                "ocr_quality": "",
                "rule_cleanable": False,
                "summary": "检测到期刊单期或连续出版物特征，按书籍筛选口径排除。",
                "evidence": list(periodical_signals),
            },
            "audit_status": "periodical_excluded",
            "resolved_md_path": str(path),
            "periodical_detected": True,
            "periodical_evidence": list(periodical_signals),
        }
    audit_text = prepare_audit_text(text)
    stats = traditional_chinese_stats(audit_text)
    full_text_signals = analyze_full_text_signals(audit_text, row)
    edited_collection_signals = full_text_signals["edited_collection_signals"]
    samples = distributed_samples(audit_text, sample_count, chunk_chars)
    payload = {
        "identifier": _stable_identifier(row),
        "title": normalize(row.get("title")),
        "book_track": row["book_track"],
        "metadata": {
            key: normalize(row.get(key))
            for key in ("author", "publisher", "publicationyear", "language", "document_type", "content_genre", "description", "keywords")
        },
        "sample_count": len(samples),
        "structural_signals": edited_collection_signals,
        "full_text_signals": {
            key: value for key, value in full_text_signals.items()
            if key != "targeted_evidence"
        },
        "targeted_evidence": full_text_signals["targeted_evidence"],
        "samples": [{**item, "text": _compact_prompt_text(item["text"])} for item in samples],
    }
    prompt = build_system_prompt(
        row["book_track"], subject, l1_nodes, subject_boundary
    )
    response = request_llm(
        api_url, model, prompt, payload, {node["path"] for node in l1_nodes},
        timeout, retries, max_tokens,
    )
    if not response.get("ok"):
        return {**base, **response, "audit_status": "api_failed", "resolved_md_path": str(path)}
    result = response["result"]
    result["edited_collection_signals"] = edited_collection_signals
    result["full_text_signals"] = full_text_signals
    result = apply_original_year_gate(result)
    result = apply_subject_gates(result, require_l1=bool(l1_nodes))
    result = apply_knowledge_extraction_gates(result, row["book_track"], len(samples))
    result = apply_track_structure_gates(result, row["book_track"], len(samples))
    result = apply_md_hard_gates(
        result,
        traditional_chinese_body=bool(stats["traditional_chinese_body"]),
        dominant_languages=result["dominant_languages"],
        substantial_non_zh_en=bool(result["substantial_non_zh_en"]),
    )
    result = apply_full_text_signal_gates(result, row, full_text_signals)
    result = apply_final_safety_gates(result, row)
    return {
        **base,
        **response,
        "result": result,
        "audit_status": "completed",
        "resolved_md_path": str(path),
        **stats,
    }


def load_progress(path: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return output
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                item = json.loads(line)
                output[item["identifier"]] = item
    return output


def run_md_audit(
    pilot_csv: Path,
    l1_config: Path | None,
    output_dir: Path,
    subject: str,
    *,
    subject_config: Path | None = None,
    mappings: dict[str, Path] | None = None,
    cache_dir: Path | None = None,
    ossutil_config: Path | None = None,
    source_oss_endpoint: str | None = None,
    api_url: str = DEFAULT_API_URL,
    model: str = DEFAULT_MODEL,
    workers: int = 50,
    sample_count: int = 16,
    chunk_chars: int = 1200,
    timeout: int = 240,
    retries: int = 2,
    max_tokens: int = 2400,
) -> dict[str, Any]:
    _, rows = read_csv(pilot_csv)
    boundary: dict[str, str] | None = None
    embedded_l1: list[dict[str, str]] = []
    if subject_config is not None:
        loaded_subject = load_subject_config(subject_config)
        subject = loaded_subject["subject_name"]
        boundary = loaded_subject["boundary"]
        embedded_l1 = loaded_subject["l1_nodes"]
    l1_nodes = load_l1_boundaries(l1_config) if l1_config is not None else embedded_l1
    mappings = mappings or {}
    cache_dir = cache_dir or output_dir / "md_cache"
    progress_path = output_dir / "模型原始结果" / "progress.jsonl"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    completed = load_progress(progress_path)
    pending = [
        row for row in rows
        if _stable_identifier(row) not in completed
        or completed[_stable_identifier(row)].get("audit_status")
        in {"api_failed", "unresolved_md_path"}
    ]
    lock = threading.Lock()
    counters = Counter()

    def work(row: dict[str, str]) -> dict[str, Any]:
        return audit_one(
            row,
            subject=subject,
            l1_nodes=l1_nodes,
            subject_boundary=boundary,
            mappings=mappings,
            cache_dir=cache_dir,
            ossutil_config=ossutil_config,
            source_oss_endpoint=source_oss_endpoint,
            api_url=api_url,
            model=model,
            sample_count=sample_count,
            chunk_chars=chunk_chars,
            timeout=timeout,
            retries=retries,
            max_tokens=max_tokens,
        )

    with progress_path.open("a", encoding="utf-8") as progress:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(work, row) for row in pending]
            for future in concurrent.futures.as_completed(futures):
                item = future.result()
                with lock:
                    progress.write(json.dumps(item, ensure_ascii=False) + "\n")
                    progress.flush()
                    counters[item["audit_status"]] += 1
                    done = sum(counters.values())
                    if done % 10 == 0 or done == len(pending):
                        print(json.dumps({"done": done, "pending": len(pending), **counters}, ensure_ascii=False), flush=True)
    all_completed = load_progress(progress_path)
    return {
        "pilot_rows": len(rows),
        "previously_recorded": len(completed),
        "processed_now": len(pending),
        "recorded_total": len(all_completed),
        "status_counts": dict(Counter(item["audit_status"] for item in all_completed.values())),
        "subject": subject,
        "subject_config": str(subject_config) if subject_config is not None else "",
        "l1_node_count": len(l1_nodes),
        "progress_jsonl": str(progress_path),
    }


def flatten_audit(row: dict[str, str], progress: dict[str, dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = dict(row)
    output["periodical_detected"] = False
    output["periodical_evidence"] = ""
    item = progress.get(_stable_identifier(row))
    if item is None:
        output.update({"audit_status": "not_run", "final_decision": "REVIEW", "audit_summary": "尚未运行 MD 审核。"})
        return output
    output["audit_status"] = item["audit_status"]
    output["resolved_md_path"] = item.get("resolved_md_path", "")
    periodical_signals = tuple(item.get("periodical_evidence") or ())
    resolved_path = Path(output["resolved_md_path"]) if output["resolved_md_path"] else None
    if not periodical_signals and resolved_path is not None and resolved_path.is_file():
        periodical_signals = detect_periodical_source(row, read_text(resolved_path))
    if periodical_signals:
        output.update({
            "audit_status": "periodical_excluded",
            "final_decision": "DROP",
            "document_structure": "periodical_issue",
            "rule_cleanable": False,
            "periodical_detected": True,
            "periodical_evidence": " | ".join(periodical_signals),
            "audit_summary": "检测到期刊单期或连续出版物特征，按书籍筛选口径排除。",
            "audit_evidence": json.dumps(list(periodical_signals), ensure_ascii=False),
        })
        return output
    if not item.get("ok"):
        output.update({
            "final_decision": "REVIEW",
            "audit_summary": item.get("error", "运行异常，未形成质量结论。"),
        })
        return output
    result = dict(item["result"])
    full_text_signals = result.get("full_text_signals") or {}
    if full_text_signals:
        result = apply_full_text_signal_gates(result, row, full_text_signals)
    result = apply_final_safety_gates(result, row)
    output.update({
        "final_decision": result["decision"],
        "matched_l1_paths": " | ".join(result.get("matched_l1_paths", [])),
        "dominant_languages": " | ".join(result.get("dominant_languages", [])),
        "substantial_non_zh_en": result.get("substantial_non_zh_en", False),
        "subject_relevance": result.get("subject_relevance", ""),
        "knowledge_extraction_suitability": result.get("knowledge_extraction_suitability", ""),
        "document_structure": result.get("document_structure", ""),
        "sample_knowledge_counts": json.dumps(
            result.get("sample_knowledge_counts", {}), ensure_ascii=False
        ),
        "edited_collection_candidate": bool(
            (result.get("edited_collection_signals") or {}).get("edited_collection_candidate")
        ),
        "edited_collection_signals": json.dumps(
            result.get("edited_collection_signals", {}), ensure_ascii=False
        ),
        "full_text_signals": json.dumps(
            result.get("full_text_signals", {}), ensure_ascii=False
        ),
        "full_text_gate_reason": result.get("full_text_gate_reason", ""),
        "decision_adjusted_by_knowledge_gate": result.get(
            "decision_adjusted_by_knowledge_gate", False
        ),
        "decision_adjusted_by_full_text_gate": result.get(
            "decision_adjusted_by_full_text_gate", False
        ),
        "decision_adjusted_by_final_safety_gate": result.get(
            "decision_adjusted_by_final_safety_gate", False
        ),
        "ocr_quality": result.get("ocr_quality", ""),
        "rule_cleanable": result.get("rule_cleanable", ""),
        "audit_summary": result.get("summary", ""),
        "audit_evidence": json.dumps(result.get("evidence", []), ensure_ascii=False),
    })
    return output


def materialize_results(
    rows: list[dict[str, Any]], output_dir: Path, source_fields: list[str]
) -> dict[str, Any]:
    seen: set[str] = set()
    counts = Counter()
    for row in rows:
        key = _stable_identifier(row)
        if not key or key in seen:
            raise ValueError(f"结果标识符缺失或重复: {key!r}")
        seen.add(key)
        track = normalize(row.get("book_track"))
        decision = normalize(row.get("final_decision"))
        if track not in TRACKS or decision not in DECISIONS:
            raise ValueError(f"无效书型或等级: {track}/{decision}")
        counts[(track, decision)] += 1
    for track in TRACKS:
        for decision in DECISIONS:
            subset = [
                row for row in rows
                if row.get("book_track") == track and row.get("final_decision") == decision
            ]
            write_csv(output_dir / track / decision / "本级0611字段书目.csv", subset, source_fields)
    write_csv(output_dir / "最终审核结果.csv", rows, source_fields)
    summary = {
        "total": len(rows),
        "counts": {
            track: {decision: counts[(track, decision)] for decision in DECISIONS}
            for track in TRACKS
        },
    }
    (output_dir / "运行配置与统计.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def materialize_from_progress(pilot_csv: Path, output_dir: Path) -> dict[str, Any]:
    source_fields, rows = read_csv(pilot_csv)
    progress = load_progress(output_dir / "模型原始结果" / "progress.jsonl")
    flattened = [flatten_audit(row, progress) for row in rows]
    screened_path = output_dir / "书目初筛结果.csv"
    if screened_path.is_file():
        screened_fields, screened_rows = read_csv(screened_path)
        pilot_ids = {_stable_identifier(row) for row in rows}
        full_candidate_ids = {
            _stable_identifier(row)
            for row in screened_rows
            if row.get("metadata_decision") == "KEEP"
            and row.get("reference_screen_decision") != "duplicate"
        }
        if full_candidate_ids == pilot_ids:
            source_fields = list(dict.fromkeys([*screened_fields, *source_fields]))
            for row in screened_rows:
                identifier = _stable_identifier(row)
                if identifier in pilot_ids:
                    continue
                output = dict(row)
                if row.get("metadata_decision") == "DROP":
                    output.update({
                        "audit_status": "metadata_drop",
                        "final_decision": "DROP",
                        "rule_cleanable": False,
                        "audit_summary": (
                            "[元数据硬门槛] "
                            + normalize(row.get("metadata_drop_reasons"))
                        ),
                    })
                elif row.get("reference_screen_decision") == "duplicate":
                    output.update({
                        "audit_status": "metadata_duplicate",
                        "final_decision": "DROP",
                        "rule_cleanable": False,
                        "audit_summary": "[书目去重门槛] 辞海类题名重复项。",
                    })
                else:
                    continue
                flattened.append(output)
            order = {
                _stable_identifier(row): index for index, row in enumerate(screened_rows)
            }
            flattened.sort(key=lambda row: order.get(_stable_identifier(row), len(order)))
    return materialize_results(flattened, output_dir, source_fields)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通用学科书目初筛与 Markdown 质量审核")
    parser.add_argument("action", choices=("metadata", "audit", "materialize", "all"))
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--l1-config", type=Path)
    parser.add_argument("--subject-config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--subject", default="机械工程")
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--audit-track", choices=("全部", *TRACKS), default="全部")
    parser.add_argument("--audit-all", action="store_true")
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument("--workers", type=int, default=50)
    parser.add_argument("--sample-count", type=int, default=16)
    parser.add_argument("--chunk-chars", type=int, default=1200)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument("--path-map", action="append", help="可重复：OSS_PREFIX=LOCAL_ROOT")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--ossutil-config", type=Path)
    parser.add_argument("--source-oss-endpoint")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.action in {"metadata", "all"}:
        print(json.dumps(run_metadata_screen(
            args.input_csv,
            args.output,
            args.sample_size,
            args.seed,
            audit_track=args.audit_track,
            audit_all=args.audit_all,
        ), ensure_ascii=False, indent=2), flush=True)
    pilot_csv = args.output / "MD审核试验样本.csv"
    if args.action in {"audit", "all"}:
        print(json.dumps(run_md_audit(
            pilot_csv,
            args.l1_config,
            args.output,
            args.subject,
            subject_config=args.subject_config,
            mappings=parse_path_mappings(args.path_map),
            cache_dir=args.cache_dir,
            ossutil_config=args.ossutil_config,
            source_oss_endpoint=args.source_oss_endpoint,
            api_url=args.api_url,
            model=args.model,
            workers=args.workers,
            sample_count=args.sample_count,
            chunk_chars=args.chunk_chars,
            timeout=args.timeout,
            retries=args.retries,
            max_tokens=args.max_tokens,
        ), ensure_ascii=False, indent=2), flush=True)
    if args.action in {"materialize", "all"}:
        print(json.dumps(materialize_from_progress(pilot_csv, args.output), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
