#!/usr/bin/env python3
"""Convert audited book CSV/JSON/JSONL to a traceable extraction books.json."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

TRACKS = {"dictionary": {"dictionary", "辞海类"}, "important": {"important", "其他重要书籍", "重要书籍"}}
FAILURES = {"api_failed", "unresolved_md_path", "not_run", "failed", "missing_audit", "md_unavailable", "technical_failed"}


def load_rows(path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, list):
            raise ValueError("JSON input must be an array")
        return value
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def text(row, field):
    value = row.get(field)
    return "" if value is None else str(value).strip()


class Resolver:
    def __init__(self, base, roots, mappings):
        self.base = base
        self.mappings = sorted(mappings, key=lambda pair: -len(pair[0]))
        self.indices = {}
        for kind, root in roots.items():
            index = defaultdict(list)
            if root:
                if not root.is_dir():
                    raise FileNotFoundError(root)
                extensions = {".md"} if kind == "md" else {".pdf", ".bin"}
                for p in root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in extensions:
                        index[p.name].append(p.resolve())
            self.indices[kind] = index

    def local(self, value):
        if not value:
            return None
        for prefix, root in self.mappings:
            if value.startswith(prefix):
                return (root / unquote(value[len(prefix):])).resolve()
        if "://" in value:
            return None
        p = Path(value).expanduser()
        return (self.base / p).resolve() if not p.is_absolute() else p.resolve()

    def find(self, row, identifier, kind, custom_field=None):
        fields = ([custom_field] if custom_field else []) + (
            ["md_path", "resolved_md_path", "v3_source_path", "source_path", "parsed_path"]
            if kind == "md" else ["pdf_path", "documentpath"])
        extensions = {".md"} if kind == "md" else {".pdf", ".bin"}
        references = [text(row, field) for field in fields if text(row, field)]
        for field in fields:
            value = text(row, field)
            local = self.local(value)
            if local and local.suffix.lower() in extensions and local.is_file():
                return local, {"method": "row_path_or_prefix_map", "field": field, "reference": value}
        # Match filename exactly. Titles and sanitized names are intentionally not fuzzy-matched.
        filenames = [identifier + suffix for suffix in sorted(extensions)]
        candidates = {p for name in filenames for p in self.indices[kind].get(name, [])}
        if not candidates:
            filenames = [unquote(urlsplit(value).path).replace("\\", "/").rsplit("/", 1)[-1] for value in references]
            candidates = {p for name in filenames for p in self.indices[kind].get(name, [])}
        if len(candidates) == 1:
            return next(iter(candidates)), {"method": "exact_filename", "filenames": filenames}
        if len(candidates) > 1:
            return None, {"error": "ambiguous_local_files", "candidates": sorted(map(str, candidates))}
        return None, {"error": "local_file_not_found", "references": references}


def convert(args):
    rows = load_rows(args.input)
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError("Every input record must be an object")
    decision_field = args.decision_column or next((f for f in ("final_decision", "decision") if any(f in row for row in rows)), None)
    if rows and not decision_field:
        raise ValueError("No audit decision column; supply --decision-column explicitly")
    if rows and not any(args.track_column in row for row in rows) and not args.assume_track:
        raise ValueError("Missing book_track; use --assume-track only for an already separated book list")
    maps = []
    for mapping in args.path_map:
        prefix, sep, dest = mapping.partition("=")
        if not sep or not prefix or not dest:
            raise ValueError("--path-map must be PREFIX=/local/root")
        maps.append((prefix.rstrip("/") + "/", Path(dest).expanduser().resolve()))
    resolver = Resolver(args.input.resolve().parent, {"md": args.md_root, "pdf": args.pdf_root}, maps)
    counts = Counter(text(r, args.id_column) for r in rows)
    books, audit, excluded, unresolved = [], [], [], []
    for line, row in enumerate(rows, 1):
        identifier = text(row, args.id_column)
        entry = {"input_row": line, "identifier": identifier, "original": row}
        decision = text(row, decision_field).upper() if decision_field else ""
        if decision not in {"PASS", "REVIEW", "DROP"}:
            unresolved.append({**entry, "reason": "unknown_or_missing_decision", "decision": decision})
            continue
        if decision != "PASS":
            excluded.append({**entry, "reason": "audit_not_pass", "decision": decision})
            continue
        track = text(row, args.track_column)
        if track and track not in set().union(*TRACKS.values()):
            unresolved.append({**entry, "reason": "unknown_track", "book_track": track})
            continue
        if track and track not in TRACKS[args.track]:
            excluded.append({**entry, "reason": "different_track", "book_track": track})
            continue
        if not track and not args.assume_track:
            unresolved.append({**entry, "reason": "missing_track"})
            continue
        if text(row, "pipeline_failure_type") or text(row, "audit_status") in FAILURES:
            unresolved.append({**entry, "reason": "technical_failure_despite_pass"})
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            unresolved.append({**entry, "reason": "invalid_or_missing_identifier"})
            continue
        if counts[identifier] != 1:
            unresolved.append({**entry, "reason": "duplicate_identifier"})
            continue
        if text(row, "subject_slug") and text(row, "subject_slug") != args.subject_slug:
            unresolved.append({**entry, "reason": "subject_mismatch"})
            continue
        md, md_match = resolver.find(row, identifier, "md", args.md_column)
        if not md:
            unresolved.append({**entry, "reason": "md_unresolved", "detail": md_match})
            continue
        book = {"identifier": identifier, "title": text(row, args.title_column) or md.stem,
                "md_path": str(md), "subject_slug": args.subject_slug}
        pdf, pdf_match = resolver.find(row, identifier, "pdf", args.pdf_column)
        if pdf:
            book["pdf_path"] = str(pdf)
        if args.track == "dictionary":
            scope = args.scope_config.resolve() if args.scope_config else resolver.local(text(row, "scope_config"))
            if not scope or not scope.is_file():
                unresolved.append({**entry, "reason": "dictionary_scope_config_missing"})
                continue
            book["scope_config"] = str(scope)
        books.append(book)
        audit.append({**entry, "book": book, "md_match": md_match, "pdf_match": pdf_match,
                      "pdf_quality_assumed_acceptable": True})
    report = {"input": str(args.input.resolve()), "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "input_rows": len(rows), "books": len(books), "excluded": len(excluded), "unresolved": len(unresolved),
              "count_conserved": len(rows) == len(books) + len(excluded) + len(unresolved),
              "subject_slug": args.subject_slug, "track": args.track, "decision_column": decision_field,
              "pdf_attached": sum("pdf_path" in b for b in books), "pdf_quality_checked": False,
              "api_calls": 0, "downloads": 0, "unresolved_reasons": dict(Counter(x["reason"] for x in unresolved)),
              "exclusion_reasons": dict(Counter(x["reason"] for x in excluded)),
              "allow_partial": args.allow_partial, "ready_for_classification": bool(books) and (not unresolved or args.allow_partial)}
    return books, audit, excluded, unresolved, report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--track", choices=tuple(TRACKS), required=True)
    p.add_argument("--subject-slug", required=True)
    p.add_argument("--md-root", type=Path)
    p.add_argument("--pdf-root", type=Path)
    p.add_argument("--path-map", action="append", default=[])
    p.add_argument("--scope-config", type=Path)
    p.add_argument("--decision-column")
    p.add_argument("--track-column", default="book_track")
    p.add_argument("--id-column", default="identifier")
    p.add_argument("--title-column", default="title")
    p.add_argument("--md-column")
    p.add_argument("--pdf-column")
    p.add_argument("--assume-track", action="store_true", help="Input already separated; accept missing book_track")
    p.add_argument("--allow-partial", action="store_true", help="Allow valid subset to continue despite unresolved records")
    return p


def main():
    args = parser().parse_args()
    if args.out.exists():
        raise FileExistsError(f"Use a new output directory: {args.out}")
    books, audit, excluded, unresolved, report = convert(args)
    args.out.mkdir(parents=True)
    for name, obj in (("books.json", books), ("report.json", report)):
        (args.out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, items in (("mapping_audit.jsonl", audit), ("excluded.jsonl", excluded), ("unresolved.jsonl", unresolved)):
        with (args.out / name).open("w", encoding="utf-8") as f:
            for obj in items:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_classification"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
