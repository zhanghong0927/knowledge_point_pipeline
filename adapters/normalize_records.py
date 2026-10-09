#!/usr/bin/env python3
"""Normalize extractor records and preserve original evidence in a sidecar."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re


TEXT_FIELDS = ("name", "knowledge_point", "definition", "en_definition", "description", "en_description")
CJK = re.compile(r"[\u3400-\u9fff]")


def records(path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)
    elif path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, list):
            raise ValueError("JSON input must be an array")
        yield from value
    else:
        with path.open(encoding="utf-8-sig") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def english(text):
    return bool(re.search(r"[A-Za-z]", text)) and not CJK.search(text)


def source_label(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("book_title") or value.get("title") or value.get("identifier") or "")
    return json.dumps(value, ensure_ascii=False) if value else ""


def standardize(row, mode, subject, slug, line):
    if not isinstance(row, dict):
        raise ValueError(f"Record {line} must be an object")
    tagged_subject = row.get("tag")
    if tagged_subject and tagged_subject not in (subject, slug):
        raise ValueError(f"Record {line}: mixed subjects; split before processing")
    old_id = row.get("id", row.get("record_id"))
    book = str(row.get("identifier") or row.get("book_id") or "")
    if old_id is None or old_id == "":
        seed = json.dumps([slug, mode, book, row, line], ensure_ascii=False, sort_keys=True)
        record_id = slug + ":" + mode + ":" + hashlib.sha256(seed.encode()).hexdigest()[:24]
    else:
        if isinstance(old_id, (bool, dict, list)):
            raise ValueError(f"Record {line}: invalid id type")
        record_id = str(old_id)
    values = {}
    for field in TEXT_FIELDS:
        value = row.get(field) or ""
        if not isinstance(value, str):
            raise ValueError(f"Record {line}: {field} must be text")
        values[field] = value
    if mode == "important":
        # This extractor's knowledge_point field holds an original-language heading.
        heading = values["knowledge_point"] or values["name"]
        values["name"] = heading if CJK.search(heading) else ""
        values["knowledge_point"] = heading if not CJK.search(heading) else ""
    moves = []
    for zh, en in (("name", "knowledge_point"), ("definition", "en_definition"), ("description", "en_description")):
        if english(values[zh]) and not values[en]:
            values[en], values[zh] = values[zh], ""
            moves.append(f"{zh}->{en}")
        elif not values[zh] and CJK.search(values[en]) and not re.search(r"[A-Za-z]", values[en]):
            values[zh], values[en] = values[en], ""
            moves.append(f"{en}->{zh}")
        elif english(values[zh]) and CJK.search(values[en]) and not re.search(r"[A-Za-z]", values[en]):
            values[zh], values[en] = values[en], values[zh]
            moves.append(f"swap:{zh},{en}")
    raw_source = row.get("source") or row.get("title") or book
    result = {"id": record_id, **values, "main_tags": row.get("main_tags") or "",
              "related_tags": row.get("related_tags") or [], "tag": subject,
              "source": source_label(raw_source)}
    # Raw extraction text is context, not a fabricated definition.
    explanation = (row.get("raw_content") or row.get("explanation") or row.get("source_quote") or "")
    if explanation:
        if not isinstance(explanation, str):
            raise ValueError(f"Record {line}: extraction context must be text")
        result["explanation"] = explanation
    trace = {"id": record_id, "original_id": old_id, "subject": subject, "subject_slug": slug,
             "input_line": line, "mode": mode, "moves": moves, "original_record": row}
    return result, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--format", choices=("standard", "dictionary", "important"), default="standard")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(f"Use a new output directory: {args.out}")
    args.out.mkdir(parents=True)
    traces = {row["id"]: row for row in records(args.trace)} if args.restore and args.trace else {}
    seen = set()
    count = moved = 0
    temporary = args.out / "records.jsonl.tmp"
    with temporary.open("w", encoding="utf-8") as output, (args.out / "trace.jsonl.tmp").open("w", encoding="utf-8") as evidence:
        for line, row in enumerate(records(args.input), 1):
            if args.restore:
                record_id = str(row["id"])
                if record_id not in traces:
                    raise ValueError(f"Missing original trace for {record_id}")
                trace = traces[record_id]
                if trace["subject"] != args.subject:
                    raise ValueError(f"Subject mismatch for {record_id}")
                row["id"] = record_id
                row["tag"] = args.subject
                original = trace["original_record"]
                if "source" in original:
                    row["source"] = original["source"]
                trace["post_cleaning_text_requires_grounding_recheck"] = True
            else:
                row, trace = standardize(row, args.format, args.subject, args.slug, line)
                record_id = row["id"]
            if record_id in seen:
                raise ValueError(f"Duplicate ID: {record_id}; resolve input conflict first")
            seen.add(record_id)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            evidence.write(json.dumps(trace, ensure_ascii=False) + "\n")
            count += 1
            moved += bool(trace.get("moves"))
    temporary.replace(args.out / "records.jsonl")
    (args.out / "trace.jsonl.tmp").replace(args.out / "trace.jsonl")
    report = {"records": count, "language_moved_records": moved, "subject": args.subject,
              "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "ids_preserved_as_strings_or_generated": True, "api_calls": 0}
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
