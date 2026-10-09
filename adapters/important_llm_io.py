#!/usr/bin/env python3
"""Bridge verified N1/N3 books and the unmodified book-extractor handoff."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_lines(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def validate_classification(prepared, classification, book):
    sys.path.insert(0, str(ROOT / "modules/important_books/scripts"))
    from layout_model_validation import load_manifest, load_prepared, check_saved, sha256
    original = load_manifest(prepared)
    classified = read(classification / "manifest.json")
    if Path(classified["base"]).resolve() != prepared.resolve():
        raise ValueError("Classification preparation directory mismatch")
    if classified["base_manifest_sha256"] != sha256(prepared / "manifest.json"):
        raise ValueError("Classification preparation manifest changed")
    if classified["targets"] != original["targets"]:
        raise ValueError("Classification target IDs changed")
    ref = book["identifier"]
    result = read(classification / "results" / (ref + ".json"))
    if result.get("ref") != ref:
        raise ValueError("Classification ref mismatch")
    if result.get("status") == "technical_failed":
        return result, None
    prep = load_prepared(prepared, original, ref)
    if Path(prep["md_path"]).resolve() != Path(book["md_path"]).resolve():
        raise ValueError("MD differs from classified book")
    check_saved(result, prep, classification)
    return result, prep


def prepare(books_path, prepared, classification, output, subject):
    if output.exists():
        raise FileExistsError(f"Use a new manifest directory: {output}")
    books = read(books_path)
    if not isinstance(books, list) or not books:
        raise ValueError("Expected nonempty books.json array")
    refs = [b.get("identifier") for b in books]
    if any(not isinstance(r, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", r) for r in refs):
        raise ValueError("Book identifiers must be filename-safe strings")
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate book IDs")
    if set(refs) != set(read(prepared / "manifest.json")["targets"]):
        raise ValueError("Books differ from the classified batch")
    selected, excluded, trace, paths = [], [], [], set()
    for book in books:
        ref = book["identifier"]
        entry = {"identifier": ref, "original_book": book}
        try:
            if book.get("subject_slug") and book["subject_slug"] != subject:
                raise ValueError("Book belongs to a different subject")
            row, prep = validate_classification(prepared, classification, book)
            if row.get("status") != "classified" or row.get("primary_class") not in {"N1", "N3"}:
                excluded.append({**entry, "reason": "not_classified_N1_N3", "classification": row})
                continue
            path = Path(prep["md_path"]).resolve()
            if path in paths:
                raise ValueError("Repeated local MD source path")
            if sha(path) != prep["md_sha256"]:
                raise ValueError("MD changed after classification")
            language = book.get("language")
            if language and language not in {"zh", "en", "zh-en"}:
                excluded.append({**entry, "reason": "unsupported_explicit_language", "language": language})
                continue
            item = {"book_id": ref, "local_name": str(path), "title": book.get("title") or path.stem,
                    "subject": subject, "sha256": sha(path)}
            if language:
                item["language"] = language
            selected.append(item)
            paths.add(path)
            trace.append({**entry, "llm_book": item, "primary_class": row["primary_class"]})
        except (ValueError, KeyError, OSError) as exc:
            excluded.append({**entry, "reason": "classification_or_source_validation_failed", "error": str(exc)})
    output.mkdir(parents=True)
    dump(output / "manifest.json", {"books": selected})
    write_lines(output / "excluded_books.jsonl", excluded)
    write_lines(output / "book_mapping.jsonl", trace)
    report = {"input_books": len(books), "selected_N1_N3": len(selected), "excluded": len(excluded),
              "input_sha256": sha(books_path), "subject": subject, "api_calls": 0,
              "count_conserved": len(books) == len(selected) + len(excluded)}
    dump(output / "report.json", report)
    return report


def consolidate(delivery, work, output, subject, slug):
    """Consume a verified native delivery snapshot; keep all native evidence on disk."""
    if output.exists():
        raise FileExistsError(f"Use a new output directory: {output}")
    checksums = read(delivery / "checksums.json")
    if not {"books.json", "summary.json"} <= set(checksums):
        raise ValueError("Missing native snapshot metadata checksums")
    for name, digest in checksums.items():
        path = (delivery / name).resolve()
        if not path.is_relative_to(delivery.resolve()) or sha(path) != digest:
            raise ValueError(f"Native delivery snapshot checksum mismatch: {name}")
    books = read(delivery / "books.json")
    formal = read(work / "inputs/manifest.json")["books"]
    formal_by_id = {b["book_id"]: b for b in formal}
    if len(formal_by_id) != len(formal) or len({b['book_id'] for b in books}) != len(books):
        raise ValueError("Duplicate book identities in delivery")
    if set(formal_by_id) != {b["book_id"] for b in books}:
        raise ValueError("Delivery does not cover selected input books")
    records, audit, seen = [], [], set()
    for book in books:
        bid, rid = book["book_id"], book["run_id"]
        raw_manifest_path = work / "runs" / rid / "manifest.json"
        manifest = read(raw_manifest_path)
        if manifest["book_id"] != bid or manifest["run_id"] != rid:
            raise ValueError("Run identity mismatch")
        if manifest["status"] not in {"complete", "partial"} or (raw_manifest_path.parent / ".running").exists():
            raise ValueError("Run is not a stopped complete/partial snapshot")
        if formal_by_id[bid].get("subject") != slug:
            raise ValueError("Mixed subjects; export per subject")
        md_path = Path(formal_by_id[bid]["local_name"])
        if not md_path.is_absolute() or sha(md_path) != formal_by_id[bid]["sha256"]:
            raise ValueError("Input MD changed since extraction preparation")
        if manifest["source_sha256"] != formal_by_id[bid]["sha256"]:
            raise ValueError("Run MD hash differs from input")
        if not book["records"]:
            if book["accepted_records"]:
                raise ValueError("Nonzero record count without file")
            continue
        record_path = (delivery / book["records"]).resolve()
        unit_path = (delivery / book["units"]).resolve()
        if not record_path.is_relative_to(delivery.resolve()) or not unit_path.is_relative_to(delivery.resolve()):
            raise ValueError("Delivery path escapes snapshot")
        if book["records"] not in checksums or book["units"] not in checksums:
            raise ValueError("Unhashed native delivery file")
        frozen_manifest = delivery / "books" / bid / "manifest.json"
        if not frozen_manifest.resolve().is_relative_to(delivery.resolve()) or sha(frozen_manifest) != sha(raw_manifest_path):
            raise ValueError("Source run changed after delivery export")
        unit_ids = {u["id"] for u in read(unit_path)}
        count = 0
        for line in record_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["book_id"] != bid or row["run_id"] != rid or not set(row["evidence_ids"]) <= unit_ids:
                raise ValueError("Record source attribution mismatch")
            # Native record_id is run-local. Namespace the integration ID without replacing it.
            stable = "llm:" + hashlib.sha256(json.dumps([bid, rid, row["record_id"]]).encode()).hexdigest()
            if stable in seen:
                raise ValueError("Duplicate extracted record identity")
            seen.add(stable)
            source = {"identifier": bid, "book_title": book["title"], "md_path": str(md_path),
                      "md_sha256": manifest["source_sha256"], "run_id": rid,
                      "record_id": row["record_id"], "evidence_ids": row["evidence_ids"],
                      "candidate_ids": row["candidate_ids"], "units_path": str(unit_path),
                      "run_manifest_path": str(raw_manifest_path.resolve()),
                      "candidates_path": str((raw_manifest_path.parent / "candidates.jsonl").resolve()),
                      "book_status": manifest["status"], "definition_policy": "same-book evidence-based synthesis"}
            records.append({**row, "id": stable, "tag": subject, "source": source})
            audit.append({"id": stable, "record_id": row["record_id"], "book_id": bid, "run_id": rid})
            count += 1
        if count != book["accepted_records"]:
            raise ValueError("Delivery book count mismatch")
    native_summary = read(delivery / "summary.json")
    if len(records) != native_summary["counts"]["accepted"]:
        raise ValueError("Delivery aggregate count mismatch")
    output.mkdir(parents=True)
    write_lines(output / "records.jsonl", records)
    write_lines(output / "id_mapping.jsonl", audit)
    report = {"records": len(records), "books": len(books), "subject": subject,
              "native_delivery": str(delivery.resolve()), "native_summary": native_summary,
              "api_calls": 0, "native_records_unchanged_except_added_integration_fields": True}
    dump(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--books", type=Path, required=True)
    prep.add_argument("--prepared", type=Path, required=True)
    prep.add_argument("--classification", type=Path, required=True)
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--subject-slug", required=True)
    export = sub.add_parser("consolidate")
    export.add_argument("--delivery", type=Path, required=True)
    export.add_argument("--work", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--subject", required=True)
    export.add_argument("--subject-slug", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        report = prepare(args.books, args.prepared, args.classification, args.out, args.subject_slug)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["selected_N1_N3"] else 2
    report = consolidate(args.delivery, args.work, args.out, args.subject, args.subject_slug)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
