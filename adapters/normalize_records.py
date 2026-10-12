#!/usr/bin/env python3
"""Normalize extractor records and preserve original evidence in a sidecar."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
from cleaning_handoff import RECORD_FINGERPRINT, seal_export, sha


TEXT_FIELDS = ("name", "knowledge_point", "definition", "en_definition", "description", "en_description")
CJK = re.compile(r"[\u3400-\u9fff]")
NORMALIZATION_REBUILD = ('rerun local normalize, clean and export from the original extraction records '
                         'in a new output directory; do not hand-write hashes or fingerprints')


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
    fingerprint_input = json.dumps({'original_record': row, 'format': mode,
                                    'subject_slug': slug, 'input_line': line},
                                   ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    result[RECORD_FINGERPRINT] = hashlib.sha256(fingerprint_input).hexdigest()
    trace = {"id": record_id, "original_id": old_id, "subject": subject, "subject_slug": slug,
             "input_line": line, "mode": mode, "moves": moves, "original_record": row}
    return result, trace


def verify_normalization(report_path, trace_path, subject, slug):
    try:
        if report_path is None or trace_path is None:
            raise ValueError('Strict cleaning export requires --normalization-report and --trace')
        report_path, trace_path = report_path.resolve(), trace_path.resolve()
        report_digest = sha(report_path)
        report = json.loads(report_path.read_text(encoding='utf-8-sig'))
        scope = report['normalization']
        if (not isinstance(scope, dict) or type(scope.get('version')) is not int or scope['version'] != 2
                or scope.get('record_fingerprint_field') != RECORD_FINGERPRINT
                or scope.get('format') not in ('standard', 'dictionary', 'important')
                or scope.get('subject') != subject or scope.get('subject_slug') != slug
                or report.get('subject') != subject):
            raise ValueError('Initial normalization scope mismatch')
        normalized_path = report_path.parent / 'records.jsonl'
        if trace_path != report_path.parent / 'trace.jsonl':
            raise ValueError('Trace path differs from initial normalization output')
        if (not isinstance(report['output_sha256'], dict)
                or set(report['output_sha256']) != {'records.jsonl', 'trace.jsonl'}):
            raise ValueError('Invalid initial normalization output manifest')
        original_path = Path(report['input'])
        if not original_path.is_absolute():
            raise ValueError('Initial normalization input path must be absolute')
        hashes = {report_path: report_digest, original_path: report['input_sha256'],
                  **{report_path.parent / name: value for name, value in report['output_sha256'].items()}}
        for path, digest in hashes.items():
            if sha(path) != digest:
                raise ValueError(f'Initial normalization evidence changed: {path.name}')
        normalized = list(records(normalized_path))
        traces = list(records(trace_path))
        for label, values in (('normalized records', normalized), ('trace', traces)):
            if any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] for row in values):
                raise ValueError(f'Invalid initial {label} ID')
            if len({row['id'] for row in values}) != len(values):
                raise ValueError(f'Duplicate initial {label} ID')
        if len(normalized) != report['records'] or {r['id'] for r in traces} != {r['id'] for r in normalized}:
            raise ValueError('Initial trace/record ID coverage mismatch')
        if any(r.get('subject') != subject or r.get('subject_slug') != slug
               or r.get('mode') != scope['format'] for r in traces):
            raise ValueError('Initial trace scope mismatch')
        # Rebuild the local mapping to verify full original dict sources, even when labels coincide.
        original_count = 0
        for original_count, original in enumerate(records(original_path), 1):
            row, trace = standardize(original, scope['format'], subject, slug, original_count)
            if (original_count > len(normalized) or row != normalized[original_count - 1]
                    or trace != traces[original_count - 1]):
                raise ValueError('Initial records or trace differ from original normalization')
        if original_count != len(normalized):
            raise ValueError('Initial record coverage differs from original normalization')
        if any(sha(path) != digest for path, digest in hashes.items()):
            raise ValueError('Initial normalization evidence changed during verification')
        return {r['id']: r for r in normalized}, {r['id']: r for r in traces}, hashes
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Invalid initial normalization proof: {exc}; {NORMALIZATION_REBUILD}') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--format", choices=("standard", "dictionary", "important"), default="standard")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--cleaning-report", type=Path, help="Bind restored output to native model keep decisions")
    parser.add_argument("--normalization-report", type=Path, help="Initial normalization report required for strict cleaning export")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(f"Use a new output directory: {args.out}")
    cleaning_evidence = []
    normalization_hashes = {}
    traces = {}
    if args.cleaning_report:
        if not args.restore:
            raise ValueError('--cleaning-report requires --restore')
        normalized, traces, normalization_hashes = verify_normalization(
            args.normalization_report, args.trace, args.subject, args.slug)
        cleaning_evidence = verify_model_keep(args.input, args.cleaning_report, args.subject, normalized)
    elif args.normalization_report:
        raise ValueError('--normalization-report requires --restore and --cleaning-report')
    elif args.restore and args.trace:
        traces = {row['id']: row for row in records(args.trace)}
    input_digest = sha(args.input)
    args.out.mkdir(parents=True)
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
                row.pop(RECORD_FINGERPRINT, None)
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
    if sha(args.input) != input_digest:
        raise ValueError('Input changed during normalization; use a new output directory')
    if any(sha(path) != digest for path, digest in normalization_hashes.items()):
        raise ValueError(f'Initial normalization evidence changed; {NORMALIZATION_REBUILD}')
    temporary.replace(args.out / "records.jsonl")
    (args.out / "trace.jsonl.tmp").replace(args.out / "trace.jsonl")
    report = {"records": count, "language_moved_records": moved, "subject": args.subject,
              "input_sha256": input_digest,
              "ids_preserved_as_strings_or_generated": True, "api_calls": 0}
    if not args.restore:
        report.update(input=str(args.input.resolve()),
                      normalization={'version': 2, 'format': args.format,
                                     'subject': args.subject, 'subject_slug': args.slug,
                                     'record_fingerprint_field': RECORD_FINGERPRINT},
                      output_sha256={name: sha(args.out / name) for name in ('records.jsonl', 'trace.jsonl')})
    if cleaning_evidence:
        sealed = seal_export(args.out, [args.input, *normalization_hashes, *cleaning_evidence], 'important', args.subject,
                             normalization_report=args.normalization_report, cleaning_report=args.cleaning_report)
        if any(sealed['cleaning_export']['evidence_sha256'][str(path.resolve())] != digest
               for path, digest in normalization_hashes.items()):
            raise ValueError(f'Initial normalization evidence changed while sealing; {NORMALIZATION_REBUILD}')
        report.update(sealed)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


def verify_model_keep(input_path, report_path, subject, normalized=None):
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    if report.get('stage') != 'model_clean' or report.get('subject') != subject or not report.get('finished_at'):
        raise ValueError('Incomplete or mismatched native cleaning report')
    outputs = report['outputs']
    if Path(outputs['clean']).resolve() != input_path.resolve():
        raise ValueError('Clean records path differs from native cleaning report')
    judgments_path = Path(outputs['judgments'])
    judgments = list(records(judgments_path))
    upstream = Path(report['input'])
    originals = list(records(upstream))
    if normalized is not None:
        for row in originals:
            record_id = str(row['id'])
            if record_id not in normalized:
                raise ValueError(f'Cleaning upstream ID is absent from initial normalization: {record_id}')
            if row.get('source') != normalized[record_id]['source']:
                raise ValueError(f'Cleaning upstream source differs from initial normalization: {record_id}')
            fingerprint = row.get(RECORD_FINGERPRINT)
            if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                    or fingerprint != normalized[record_id][RECORD_FINGERPRINT]):
                raise ValueError(f'Cleaning upstream record fingerprint is missing or mismatched: {record_id}; '
                                 f'{NORMALIZATION_REBUILD}')
    keys = {str(r.get('cleaning_record_id') or r.get('record_id') or r.get('global_id') or r['id']): str(r['id'])
            for r in originals}
    by_id = {r['cleaning_record_id']: r['model_cleaning'] for r in judgments}
    if (len(keys) != len(originals) or len(set(keys.values())) != len(originals)
            or len(by_id) != len(judgments) or set(by_id) != set(keys)):
        raise ValueError('Native cleaning judgment coverage mismatch')
    by_id = {keys[key]: vote for key, vote in by_id.items()}
    final = list(records(input_path))
    kept = {rid for rid, vote in by_id.items() if vote.get('decision') == 'keep'}
    if (len({str(r['id']) for r in final}) != len(final) or {str(r['id']) for r in final} != kept
            or report.get('counts', {}).get('keep', 0) != len(final)):
        raise ValueError('Clean records must match all and only native keep judgments')
    for row in final:
        if any(row.get(field, '') != by_id[str(row['id'])].get(field, '') for field in TEXT_FIELDS):
            raise ValueError('Clean text differs from native keep judgment')
    return [report_path, judgments_path, upstream]


if __name__ == "__main__":
    main()
