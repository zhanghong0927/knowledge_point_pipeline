from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, TextIO

from general_book_screening_pipeline import metadata_screen_row


SUBJECT_FIELDS = ("subject1", "subject2", "subject3")
CONTENT_FIELDS = ("title", "keywords", "abstract", "description")
RECALL_FIELDS = (
    "recall_strength",
    "recall_channels",
    "recall_matched_fields",
    "recall_matched_terms",
    "metadata_decision",
    "metadata_drop_reasons",
    "book_track",
    "reference_screen_decision",
    "reference_type",
    "important_screen_decision",
)
REQUIRED_0611_FIELDS = {
    "identifier", "title", "publicationyear", "pdf_detail_type",
    "distributionformat", "language", "parsed_path", *SUBJECT_FIELDS,
    "keywords", "abstract", "description",
}


def _raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while limit > 131_072:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"boundary.{name} 必须是字符串数组")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


@dataclass(frozen=True)
class SubjectRecallConfig:
    subject_name: str
    subject_slug: str
    aliases: tuple[str, ...]
    direct_subject_labels: tuple[str, ...]
    adjacent_subject_labels: tuple[str, ...]
    strong_terms: tuple[str, ...]
    weak_terms: tuple[str, ...]
    cooccurrence_groups: tuple[tuple[str, ...], ...]
    exclude_phrases: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubjectRecallConfig":
        if not isinstance(data, dict):
            raise ValueError("学科配置必须是 JSON 对象")
        subject_name = str(data.get("subject_name") or "").strip()
        subject_slug = str(data.get("subject_slug") or "").strip()
        boundary = data.get("boundary")
        if not subject_name or not subject_slug:
            raise ValueError("配置缺少 subject_name 或 subject_slug")
        if not isinstance(boundary, dict):
            raise ValueError("配置缺少 boundary 对象")
        groups_raw = boundary.get("cooccurrence_groups") or []
        if not isinstance(groups_raw, list):
            raise ValueError("boundary.cooccurrence_groups 必须是数组")
        groups: list[tuple[str, ...]] = []
        for index, group in enumerate(groups_raw, 1):
            if not isinstance(group, list) or len(group) < 2 or not all(
                isinstance(item, str) and item.strip() for item in group
            ):
                raise ValueError(f"第 {index} 个共同出现词组必须至少包含两个非空词")
            groups.append(tuple(dict.fromkeys(item.strip() for item in group)))
        config = cls(
            subject_name=subject_name,
            subject_slug=subject_slug,
            aliases=_string_tuple(boundary.get("aliases"), "aliases"),
            direct_subject_labels=_string_tuple(
                boundary.get("direct_subject_labels"), "direct_subject_labels"
            ),
            adjacent_subject_labels=_string_tuple(
                boundary.get("adjacent_subject_labels"), "adjacent_subject_labels"
            ),
            strong_terms=_string_tuple(boundary.get("strong_terms"), "strong_terms"),
            weak_terms=_string_tuple(boundary.get("weak_terms"), "weak_terms"),
            cooccurrence_groups=tuple(groups),
            exclude_phrases=_string_tuple(boundary.get("exclude_phrases"), "exclude_phrases"),
        )
        if not config.direct_subject_labels and not config.strong_terms and not config.aliases:
            raise ValueError("boundary 至少需要直接学科标签、别名或强领域词")
        return config


@dataclass(frozen=True)
class RecallMatch:
    matched: bool
    strength: str
    channels: tuple[str, ...]
    matched_fields: tuple[str, ...]
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedConfig:
    all_terms: tuple[str, ...]
    direct_terms: frozenset[str]
    adjacent_terms: frozenset[str]
    strong_terms: frozenset[str]
    weak_terms: frozenset[str]
    exclude_terms: frozenset[str]


@lru_cache(maxsize=64)
def _prepare_config(config: SubjectRecallConfig) -> _PreparedConfig:
    strong = (*config.aliases, *config.strong_terms)
    all_terms = tuple(dict.fromkeys((
        *config.direct_subject_labels,
        *config.adjacent_subject_labels,
        *strong,
        *config.weak_terms,
        *(term for group in config.cooccurrence_groups for term in group),
        *config.exclude_phrases,
    )))
    return _PreparedConfig(
        all_terms=all_terms,
        direct_terms=frozenset(normalize(term) for term in config.direct_subject_labels),
        adjacent_terms=frozenset(normalize(term) for term in config.adjacent_subject_labels),
        strong_terms=frozenset(normalize(term) for term in strong),
        weak_terms=frozenset(normalize(term) for term in config.weak_terms),
        exclude_terms=frozenset(normalize(term) for term in config.exclude_phrases),
    )


def load_subject_config(path: Path) -> SubjectRecallConfig:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return SubjectRecallConfig.from_dict(data)


@lru_cache(maxsize=256)
def _compile_terms(raw_terms: tuple[str, ...]) -> re.Pattern[str] | None:
    normalized = sorted(
        {normalize(term) for term in raw_terms if normalize(term)},
        key=lambda value: (-len(value), value),
    )
    if not normalized:
        return None
    parts: list[str] = []
    for term in normalized:
        escaped = re.escape(term)
        if re.fullmatch(r"[a-z0-9][a-z0-9 ._&/+()-]*", term):
            parts.append(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
        else:
            parts.append(escaped)
    return re.compile("|".join(parts))


@lru_cache(maxsize=256)
def _compile_overlapping_terms(raw_terms: tuple[str, ...]) -> re.Pattern[str] | None:
    pattern = _compile_terms(raw_terms)
    if pattern is None:
        return None
    return re.compile(rf"(?=({pattern.pattern}))")


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    pattern = _compile_terms(terms)
    return bool(pattern and pattern.search(text))


def _matched_term_set(text: str, terms: tuple[str, ...]) -> set[str]:
    pattern = _compile_overlapping_terms(terms)
    if pattern is None:
        return set()
    return {normalize(match.group(1)) for match in pattern.finditer(text)}


def _contains(text: str, raw_term: str) -> bool:
    return _contains_any(text, (raw_term,))


def _hits(fields: dict[str, str], terms: Iterable[str]) -> list[tuple[str, str]]:
    term_tuple = tuple(terms)
    pattern = _compile_terms(term_tuple)
    if pattern is None:
        return []
    output: list[tuple[str, str]] = []
    for field, text in fields.items():
        output.extend((field, match.group(0)) for match in pattern.finditer(text))
    return output


def _normalized_fields(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: normalize(row.get(field))
        for field in (*SUBJECT_FIELDS, *CONTENT_FIELDS)
    }


def _without_nul(lines: Iterable[str]) -> Iterable[str]:
    for line in lines:
        yield line.replace("\x00", "")


def _match_normalized_subject(
    normalized_fields: dict[str, str], config: SubjectRecallConfig
) -> RecallMatch:
    subject_text = {field: normalized_fields[field] for field in SUBJECT_FIELDS}
    content_text = {field: normalized_fields[field] for field in CONTENT_FIELDS}
    all_text = {**subject_text, **content_text}
    prepared = _prepare_config(config)
    subject_combined = "\n".join(subject_text.values())
    title_text = content_text["title"]
    other_content_text = "\n".join(
        content_text[field] for field in CONTENT_FIELDS if field != "title"
    )
    subject_present = _matched_term_set(subject_combined, prepared.all_terms)
    title_present = _matched_term_set(title_text, prepared.all_terms)
    other_present = _matched_term_set(other_content_text, prepared.all_terms)
    all_present = subject_present | title_present | other_present
    direct_terms = subject_present & prepared.direct_terms
    if direct_terms:
        direct_hits = _hits(subject_text, tuple(direct_terms))
        return RecallMatch(
            matched=True,
            strength="strong_recall",
            channels=("direct_subject",),
            matched_fields=tuple(dict.fromkeys(field for field, _ in direct_hits)),
            matched_terms=tuple(dict.fromkeys(term for _, term in direct_hits)),
        )

    title_strong_terms = title_present & prepared.strong_terms
    if title_strong_terms:
        return RecallMatch(
            matched=True,
            strength="strong_recall",
            channels=("title_strong_term",),
            matched_fields=("title",),
            matched_terms=tuple(sorted(title_strong_terms)),
        )

    non_title_text = {key: value for key, value in all_text.items() if key != "title"}
    excluded = bool(all_present & prepared.exclude_terms)
    title_domain_terms = title_present & prepared.weak_terms
    if title_domain_terms and not excluded:
        return RecallMatch(
            matched=True,
            strength="weak_recall",
            channels=("title_domain_term",),
            matched_fields=("title",),
            matched_terms=tuple(sorted(title_domain_terms)),
        )
    metadata_strong_terms = (subject_present | other_present) & prepared.strong_terms
    metadata_strong_hits = _hits(non_title_text, tuple(metadata_strong_terms))
    adjacent_terms = subject_present & prepared.adjacent_terms
    adjacent_hits = _hits(subject_text, tuple(adjacent_terms))
    weak_terms = all_present & prepared.weak_terms
    weak_hits = _hits(all_text, tuple(weak_terms))
    group_hits: list[tuple[str, str]] = []
    for group in config.cooccurrence_groups:
        if all(normalize(term) in all_present for term in group):
            group_hits.extend(("combined", term) for term in group)

    channels: list[str] = []
    selected_hits: list[tuple[str, str]] = []
    strength = ""
    if metadata_strong_hits and not excluded:
        channels.append("metadata_strong_term")
        selected_hits.extend(metadata_strong_hits)
        strength = "weak_recall"
    if not strength and group_hits and not excluded:
        channels.append("cooccurrence_group")
        selected_hits.extend(group_hits)
        strength = "weak_recall"
    if not strength and adjacent_hits and weak_hits and not excluded:
        channels.append("adjacent_plus_domain")
        selected_hits.extend(adjacent_hits)
        selected_hits.extend(weak_hits)
        strength = "weak_recall"
    if not strength and len({normalize(term) for _, term in weak_hits}) >= 2 and not excluded:
        channels.append("multiple_weak_terms")
        selected_hits.extend(weak_hits)
        strength = "weak_recall"

    return RecallMatch(
        matched=bool(strength),
        strength=strength,
        channels=tuple(dict.fromkeys(channels)),
        matched_fields=tuple(dict.fromkeys(field for field, _ in selected_hits)),
        matched_terms=tuple(dict.fromkeys(term for _, term in selected_hits)),
    )


def match_subject(row: dict[str, Any], config: SubjectRecallConfig) -> RecallMatch:
    return _match_normalized_subject(_normalized_fields(row), config)


@dataclass
class _SubjectWriters:
    config: SubjectRecallConfig
    handles: tuple[TextIO, TextIO, TextIO]
    writers: tuple[csv.DictWriter, csv.DictWriter, csv.DictWriter]
    temp_paths: tuple[Path, Path, Path]
    final_paths: tuple[Path, Path, Path]
    counters: Counter
    channels: Counter
    drop_reasons: Counter


def _open_subject_writers(
    output_root: Path, config: SubjectRecallConfig, fieldnames: list[str]
) -> _SubjectWriters:
    subject_root = output_root / config.subject_slug
    subject_root.mkdir(parents=True, exist_ok=True)
    names = (
        f"{config.subject_name}_宽召回全集.csv",
        f"{config.subject_name}_满足后续处理条件.csv",
        f"{config.subject_name}_硬规则排除清单.csv",
    )
    final_paths = tuple(subject_root / name for name in names)
    temp_paths = tuple(path.with_suffix(path.suffix + ".tmp") for path in final_paths)
    handles = tuple(path.open("w", encoding="utf-8-sig", newline="") for path in temp_paths)
    writers = tuple(csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore") for handle in handles)
    for writer in writers:
        writer.writeheader()
    return _SubjectWriters(
        config=config,
        handles=handles,
        writers=writers,
        temp_paths=temp_paths,
        final_paths=final_paths,
        counters=Counter(),
        channels=Counter(),
        drop_reasons=Counter(),
    )


def _annotate_row(row: dict[str, str], match: RecallMatch) -> dict[str, str]:
    metadata = metadata_screen_row(row)
    output = dict(row)
    output.update({
        "recall_strength": match.strength,
        "recall_channels": " | ".join(match.channels),
        "recall_matched_fields": " | ".join(match.matched_fields),
        "recall_matched_terms": " | ".join(match.matched_terms),
        "metadata_decision": metadata.decision,
        "metadata_drop_reasons": " | ".join(metadata.drop_reasons),
        "book_track": metadata.track,
        "reference_screen_decision": metadata.reference_screen_decision,
        "reference_type": metadata.reference_type,
        "important_screen_decision": metadata.important_screen_decision,
    })
    return output


def run_streaming_recall(
    input_csv: Path,
    configs: list[SubjectRecallConfig],
    output_root: Path,
    *,
    limit: int | None = None,
    progress_every: int = 200_000,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    if not configs:
        raise ValueError("至少需要一份学科配置")
    _raise_csv_field_limit()
    slugs = [config.subject_slug for config in configs]
    if len(slugs) != len(set(slugs)):
        raise ValueError("subject_slug 不能重复")
    if shard_count < 1 or shard_index < 0 or shard_index >= shard_count:
        raise ValueError("分片参数要求 shard_count >= 1 且 0 <= shard_index < shard_count")
    output_root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    state: list[_SubjectWriters] = []
    total_rows = 0
    processed_rows = 0
    try:
        with input_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(_without_nul(handle))
            source_fields = list(reader.fieldnames or [])
            missing = REQUIRED_0611_FIELDS - set(source_fields)
            if missing:
                raise ValueError(f"0611 CSV 缺少字段: {sorted(missing)}")
            output_fields = list(dict.fromkeys([*source_fields, *RECALL_FIELDS]))
            state = [
                _open_subject_writers(output_root, config, output_fields)
                for config in configs
            ]
            for row in reader:
                total_rows += 1
                if (total_rows - 1) % shard_count != shard_index:
                    if progress_every and total_rows % progress_every == 0:
                        elapsed = max(0.001, time.time() - started)
                        print(
                            json.dumps({
                                "rows": total_rows,
                                "processed_rows": processed_rows,
                                "rows_per_second": round(total_rows / elapsed, 1),
                                "shard_index": shard_index,
                                "shard_count": shard_count,
                                "recalled": {
                                    item.config.subject_slug: item.counters["recalled"]
                                    for item in state
                                },
                            }, ensure_ascii=False),
                            file=sys.stderr,
                            flush=True,
                        )
                    if limit is not None and total_rows >= limit:
                        break
                    continue
                processed_rows += 1
                normalized_fields = _normalized_fields(row)
                for current in state:
                    match = _match_normalized_subject(normalized_fields, current.config)
                    if not match.matched:
                        continue
                    annotated = _annotate_row(row, match)
                    current.writers[0].writerow(annotated)
                    current.counters["recalled"] += 1
                    current.counters[match.strength] += 1
                    current.channels.update(match.channels)
                    if annotated["metadata_decision"] == "KEEP":
                        current.writers[1].writerow(annotated)
                        current.counters["eligible"] += 1
                    else:
                        current.writers[2].writerow(annotated)
                        current.counters["hard_rule_excluded"] += 1
                        reasons = [
                            item for item in annotated["metadata_drop_reasons"].split(" | ") if item
                        ]
                        current.drop_reasons.update(reasons)
                if progress_every and total_rows % progress_every == 0:
                    elapsed = max(0.001, time.time() - started)
                    print(
                        json.dumps({
                            "rows": total_rows,
                            "processed_rows": processed_rows,
                            "rows_per_second": round(total_rows / elapsed, 1),
                            "shard_index": shard_index,
                            "shard_count": shard_count,
                            "recalled": {
                                item.config.subject_slug: item.counters["recalled"] for item in state
                            },
                        }, ensure_ascii=False),
                        file=sys.stderr,
                        flush=True,
                    )
                if limit is not None and total_rows >= limit:
                    break
        for current in state:
            for output in current.handles:
                output.flush()
                output.close()
            for temp_path, final_path in zip(current.temp_paths, current.final_paths):
                os.replace(temp_path, final_path)
    except Exception:
        for current in state:
            for output in current.handles:
                if not output.closed:
                    output.close()
        raise

    summary: dict[str, Any] = {
        "input_csv": str(input_csv),
        "total_rows": total_rows,
        "source_rows_seen": total_rows,
        "processed_rows": processed_rows,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "elapsed_seconds": round(time.time() - started, 3),
        "subjects": {},
    }
    for current in state:
        subject_summary = {
            "subject_name": current.config.subject_name,
            **dict(current.counters),
            "channel_counts": dict(current.channels),
            "hard_rule_reason_counts": dict(current.drop_reasons),
            "outputs": {
                "all_recalled": str(current.final_paths[0]),
                "eligible": str(current.final_paths[1]),
                "hard_rule_excluded": str(current.final_paths[2]),
            },
        }
        summary["subjects"][current.config.subject_slug] = subject_summary
        summary_path = output_root / current.config.subject_slug / f"{current.config.subject_name}_宽召回统计.json"
        summary_path.write_text(
            json.dumps(subject_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    summary_path = output_root / "全库宽召回汇总.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 0611 总表流式召回一个或多个学科书目")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--progress-every", type=int, default=200_000)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configs = [load_subject_config(path) for path in args.config]
    summary = run_streaming_recall(
        args.input_csv,
        configs,
        args.output,
        limit=args.limit,
        progress_every=args.progress_every,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
