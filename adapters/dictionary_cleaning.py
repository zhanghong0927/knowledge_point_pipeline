#!/usr/bin/env python3
"""Run the native dictionary cleaner and export only passed standard records."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from cleaning_handoff import seal_export, verify_export

ROOT = Path(__file__).resolve().parents[1]
TEXT_FIELDS = ('knowledge_point', 'name', 'definition', 'en_definition', 'description', 'en_description')
CONTENT_TRACE_BASIS = 'raw_content in frozen cleaning input, zero-based Unicode offsets; reconstruct via body_locations'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def rows(path):
    with path.open(encoding='utf-8-sig') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources(originals, books, slug):
    by_book = {b['identifier']: b for b in books}
    texts, ids = {}, set()
    if not isinstance(originals, list):
        raise ValueError('Dictionary source input must be an array')
    for book in books:
        if book.get('subject_slug') != slug or not book.get('scope_config'):
            raise ValueError('Dictionary source book needs matching subject and scope configuration')
        md = Path(book['md_path'])
        text = md.read_bytes().decode('utf-8-sig')
        texts[book['identifier']] = (md, sha(md), text, text.splitlines())
    for row in originals:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in ids:
            raise ValueError('Invalid or duplicate dictionary source ID')
        ids.add(row['id'])
        source = row.get('source')
        if not isinstance(source, dict) or source.get('identifier') not in by_book or row.get('subject_slug') != slug:
            raise ValueError('Dictionary source-book/subject identity mismatch')
        md, digest, text, lines = texts[source['identifier']]
        if Path(source.get('md_path', '')).resolve() != md.resolve() or source.get('md_sha256') != digest:
            raise ValueError('Dictionary source path/hash mismatch')
        for key in ('head_spans', 'body_spans'):
            spans = source.get(key)
            if not isinstance(spans, list) or any(not isinstance(v, list) or len(v) != 2
                    or any(type(n) is not int for n in v) or not 0 <= v[0] < v[1] <= len(text) for v in spans):
                raise ValueError('Invalid dictionary source spans')
            if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
                raise ValueError('Unordered dictionary source spans')
        if not source['head_spans'] or '\n\n'.join(text[a:b] for a, b in source['body_spans']) != row.get('raw_content'):
            raise ValueError('Dictionary body differs from original source spans')
        for key in ('head_context', 'body_edges'):
            for item in row.get('source_context', {}).get(key, []):
                n = item.get('line')
                if type(n) is not int or not 1 <= n <= len(lines) or item.get('text') != lines[n-1]:
                    raise ValueError('Dictionary context differs from original source')


def verify_snapshot(input_path, books, source_hashes):
    try:
        prepared = read(input_path.parent / 'PREPARED.json')
        manifest = read(input_path.parent / 'MANIFEST.json')
        if (input_path.name != 'INPUT.json' or prepared.get('input_sha256') != sha(input_path)
                or manifest.get('kind') != 'clean_preparation' or manifest.get('books') != books
                or manifest.get('source_hashes') != source_hashes):
            raise ValueError('Dictionary verified input snapshot mismatch')
        if any(sha(Path(p)) != digest for p, digest in manifest['extraction_files'].items()):
            raise ValueError('Dictionary extraction evidence changed after input snapshot')
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Missing or invalid dictionary verified input snapshot') from exc


def export_records(input_path, native, out, subject, slug):
    state = read(native / 'STATE.json')
    summary = read(native / 'SUMMARY.json')
    if state.get('status') not in ('finished', 'finished_with_pending') or summary.get('source_hashes_verified') is not True:
        raise ValueError('Native dictionary cleaning is not finished with verified sources')
    if read(native / 'MANIFEST.json').get('input_sha256') != sha(input_path):
        raise ValueError('Native cleaning input changed')
    originals = read(input_path)
    by_id = {r['id']: r for r in originals}
    ledger = rows(native / 'DISPOSITIONS.jsonl')
    if len(by_id) != len(originals) or len(ledger) != len(originals) or {r['id'] for r in ledger} != set(by_id):
        raise ValueError('Native cleaning disposition ID coverage differs from input')
    def passed(vote):
        return isinstance(vote, dict) and vote.get('decision') == 'keep' and vote.get('api_status') in ('ok', 'local')
    passed_ids = {r['id'] for r in ledger if passed(r.get('name')) and passed(r.get('scope')) and r.get('content_status') == 'keep'}
    final = rows(native / 'FINAL_RECORDS.jsonl')
    if len({r['id'] for r in final}) != len(final) or {r['id'] for r in final} != passed_ids:
        raise ValueError('Native final records must match all and only passed dispositions')
    if summary.get('input') != len(originals) or summary.get('final_keep') != len(final):
        raise ValueError('Native cleaning summary count mismatch')
    records, traces = [], []
    for row in final:
        original = by_id[row['id']]
        if original.get('subject_slug', slug) != slug:
            raise ValueError('Mixed subjects in dictionary cleaning export')
        source, original_source = row.get('source'), original.get('source')
        if (not isinstance(source, dict) or not isinstance(original_source, dict)
                or any(key not in source or source[key] != value for key, value in original_source.items())
                or set(source) - set(original_source) - {'content_trace', 'content_trace_basis'}):
            raise ValueError('Native cleaner changed original source identity')
        if set(source) - set(original_source):
            if not isinstance(source.get('content_trace'), dict) or source.get('content_trace_basis') != CONTENT_TRACE_BASIS:
                raise ValueError('Invalid native cleaning content trace')
        values = {field: row.get(field, '') for field in TEXT_FIELDS}
        if any(not isinstance(v, str) for v in values.values()) or not any(values[f] for f in ('knowledge_point', 'name')):
            raise ValueError('Invalid native standard name/text fields')
        # The native trace remains in trace.jsonl; published provenance keeps its original identity.
        records.append({'id': row['id'], **values, 'source': original_source,
                        'tag': subject, 'main_tags': '', 'related_tags': []})
        traces.append({'id': row['id'], 'subject': subject, 'subject_slug': slug,
                       'mode': 'dictionary', 'original_record': original, 'cleaned_record': row})
    identity = {'input_sha256': sha(input_path), 'subject': subject, 'subject_slug': slug,
                'native_hashes': {name: sha(native / name) for name in
                    ('MANIFEST.json', 'STATE.json', 'SUMMARY.json', 'DISPOSITIONS.jsonl', 'FINAL_RECORDS.jsonl')}}
    report_path = out / 'report.json'
    if report_path.exists():
        saved = read(report_path)
        verify_export(out / 'records.jsonl')
        if saved.get('identity') != identity or any(sha(out / name) != value for name, value in saved['output_sha256'].items()):
            raise ValueError('Dictionary cleaning export changed; use a new output directory')
        return saved
    out.mkdir(parents=True, exist_ok=True)
    for name, values in (('records.jsonl', records), ('trace.jsonl', traces)):
        target = out / name
        tmp = target.with_suffix(target.suffix + '.tmp')
        with tmp.open('w', encoding='utf-8') as stream:
            for value in values:
                stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        tmp.replace(target)
    report = {'identity': identity, 'records': len(records), 'input_records': len(originals),
              'review': summary.get('review', 0), 'technical_failures': summary.get('technical_failures', 0),
              'source_hashes_verified': True, 'semantic_quality_approved': False,
              'output_sha256': {name: sha(out / name) for name in ('records.jsonl', 'trace.jsonl')}}
    manifest = read(native / 'MANIFEST.json')
    for name, digest in manifest.get('sources', {}).items():
        if sha(Path(name)) != digest:
            raise ValueError('Native dictionary source changed before export')
    snapshot = [input_path.parent / name for name in ('MANIFEST.json', 'PREPARED.json')]
    report.update(seal_export(out, [input_path, *[p for p in snapshot if p.is_file()],
                                  *[native / name for name in identity['native_hashes']],
                                  *map(Path, manifest.get('sources', {}))], 'dictionary', subject))
    tmp = report_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(report_path)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--books', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--slug', required=True)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument('--context-limit', type=int, default=32768)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'modules/dictionary'))
    import portable_pipeline as native
    books = native.validate_books(read(args.books))
    original = read(args.input)
    verify_sources(original, books, args.slug)
    verify_snapshot(args.input, books, native.source_hashes(books))
    config = {'api_url': args.api_url, 'model': args.model, 'workers': args.workers,
              'context_limit': args.context_limit, 'no_auth': True}
    native.run_cleaning(args.input, config, args.out, books)
    print(json.dumps(export_records(args.input, args.out, args.export, args.subject, args.slug), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
