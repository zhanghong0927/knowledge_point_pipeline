#!/usr/bin/env python3
"""Generate a dictionary extraction manifest from verified classification outputs."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

FAMILIES = {"entry_prose", "fixed_fields", "text_commentary"}
POLICY = "dictionary_classification_admission_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def disposition(result):
    status = result.get("status")
    if status == "technical_failed":
        return "technical_failed", "classifier_technical_failed"
    if status == "other" and result.get("routing_status") == "other":
        return "other", "unsupported_structure"
    if status != "sample_supported" or result.get("routing_status") != "sample_supported":
        return "review", "classification_not_supported"
    if result.get("family") not in FAMILIES:
        return "review", "unknown_family"
    if result.get("head_position") not in {"inline", "standalone", "both"}:
        return "review", "head_position_uncertain"
    if result.get("md_quality_status") != "no_problem_reported_in_samples":
        return "review", "md_quality_requires_review"
    if result.get("applicability_status") != "model_considered_compatible":
        return "review", "applicability_not_confirmed"
    return "approved", "supported_structure_and_compatible_samples"


def convert(books_path, classification):
    books = read(books_path)
    if not isinstance(books, list) or not books:
        raise ValueError("books must be a nonempty JSON array")
    refs = [book.get("identifier") for book in books]
    if any(not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", ref) for ref in refs):
        raise ValueError("Invalid book identifier")
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate book identifiers")
    manifest = read(classification / "manifest.json")
    done = read(classification / "DONE.json")
    if set(manifest["targets"]) != set(refs) or len(manifest["targets"]) != len(refs):
        raise ValueError("Classification and book manifest ID sets differ")
    if done.get("id_sets_equal") is not True:
        raise ValueError("Classification completion check has not passed")
    files = {p.stem: p for p in (classification / "results").glob("*.json")}
    if set(files) - set(refs):
        raise ValueError("Unexpected classification result IDs")
    base = Path(manifest["base"])
    original_books = read(base / "manifest.json")["books"]
    frozen = {book["identifier"]: book for book in original_books}
    if len(frozen) != len(original_books) or set(frozen) != set(refs):
        raise ValueError("Frozen preparation manifest ID mismatch")

    scripts = Path(__file__).resolve().parents[1] / "modules/dictionary/classification/scripts"
    sys.path.insert(0, str(scripts))
    from run_unseen20_classification import validate

    approved = []
    audit = []
    queues = {status: [] for status in ("other", "review", "technical_failed")}
    for book in books:
        ref = book["identifier"]
        entry = {"identifier": ref, "book": book, "policy": POLICY}
        try:
            if book != frozen[ref]:
                raise ValueError("Book metadata differs from classified manifest")
            result = read(files[ref])
            if result.get("ref") != ref:
                raise ValueError("Result ref differs from filename/book identifier")
            if result.get("status") != "technical_failed":
                prep_path = base / "prepared" / (ref + ".json")
                if digest(prep_path) != manifest["input_sha256"].get(ref):
                    raise ValueError("Frozen classification input changed")
                prep = read(classification / "prepared" / (ref + ".json"))
                original_prep = read(prep_path)
                md = Path(book["md_path"])
                if not md.is_absolute() or digest(md) != original_prep["md_sha256"]:
                    raise ValueError("MD changed since classification")
                if prep.get("md_sha256") != original_prep["md_sha256"]:
                    raise ValueError("Classified evidence source hash mismatch")
                # Validate every sampled line against the same frozen original MD.
                lines = md.read_text(encoding="utf-8-sig").splitlines()
                for window in prep["windows"]:
                    for line in window["lines"]:
                        match = re.fullmatch(r"md:(\d+)", line["id"])
                        if not match or not 1 <= int(match[1]) <= len(lines) or lines[int(match[1])-1] != line["text"]:
                            raise ValueError("Sampled line does not match original MD")
                responses = sorted((classification / "raw").glob(ref + "_*.json"))
                if not responses:
                    raise ValueError("Missing raw classification response")
                response = read(responses[-1])["choices"][0]
                if response.get("finish_reason") != "stop":
                    raise ValueError("Incomplete classification response")
                recomputed = validate(json.loads(response["message"]["content"]), prep["windows"])
                if any(result.get(k) != v for k, v in recomputed.items()):
                    raise ValueError("Saved classification differs from evidence revalidation")
            status, reason = disposition(result)
            entry.update(status=status, reason=reason, classification=result,
                         classification_result_sha256=digest(files[ref]))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            status = "technical_failed"
            entry.update(status=status, reason="classification_or_source_validation_failed", error=str(exc))
        audit.append(entry)
        if status == "approved":
            approved.append(book)
        else:
            queues[status].append(entry)
    report = {"policy": POLICY, "input_books": len(books), "approved": len(approved),
              **{k: len(v) for k, v in queues.items()}, "count_conserved": len(audit) == len(books),
              "books_sha256": digest(books_path), "classification": str(classification.resolve()),
              "api_calls": 0, "pdf_quality_checked": False,
              "approval_scope": "permission to attempt fullbook extraction; not whole-book or human quality approval",
              "reasons": dict(Counter(entry["reason"] for entry in audit))}
    return approved, queues, audit, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books", type=Path, required=True)
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(f"Use a new output directory: {args.out}")
    approved, queues, audit, report = convert(args.books, args.classification)
    args.out.mkdir(parents=True)
    for name, value in (("approved_books.json", approved), ("report.json", report)):
        (args.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, values in {**queues, "approval_audit": audit}.items():
        with (args.out / (name + ".jsonl")).open("w", encoding="utf-8") as f:
            for value in values:
                f.write(json.dumps(value, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if approved else 2


if __name__ == "__main__":
    raise SystemExit(main())
