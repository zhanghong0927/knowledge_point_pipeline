"""Hash-bound, pass-only cleaning exports for scheduler handoffs."""

import hashlib
import json
from pathlib import Path
import re

RECORD_FINGERPRINT = '_normalization_record_sha256'
IMPORTANT_REBUILD = ('Important cleaning export requires initial normalization record fingerprints and source binding; '
                     'rerun local normalize, clean and export from original extraction records in a new output directory; '
                     'do not hand-write proof upgrades')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seal_export(out, evidence, track, subject, normalization_report=None, cleaning_report=None):
    if track == 'important' and (normalization_report is None or cleaning_report is None):
        raise ValueError(IMPORTANT_REBUILD)
    proof = {'version': 3 if track == 'important' else 1, 'track': track, 'subject': subject,
             'passed_only': True, 'evidence_sha256': {str(Path(p).resolve()): sha(p) for p in evidence}}
    if track == 'important':
        proof['source_binding'] = {'contract': 'initial_normalization', 'version': 2,
                                  'record_fingerprint_field': RECORD_FINGERPRINT,
                                  'normalization_report': str(Path(normalization_report).resolve()),
                                  'cleaning_report': str(Path(cleaning_report).resolve())}
    return {'cleaning_export': proof,
            'output_sha256': {name: sha(out / name) for name in ('records.jsonl', 'trace.jsonl')}}


def verify_record_fingerprints(normalized_path, upstream_path):
    mappings = []
    for path in (normalized_path, upstream_path):
        mapping = {}
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                record_id, fingerprint = str(row['id']), row.get(RECORD_FINGERPRINT)
                if (not record_id or record_id in mapping or not isinstance(fingerprint, str)
                        or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
                    raise ValueError(IMPORTANT_REBUILD)
                mapping[record_id] = fingerprint
        mappings.append(mapping)
    normalized, upstream = mappings
    if any(normalized.get(record_id) != fingerprint for record_id, fingerprint in upstream.items()):
        raise ValueError(IMPORTANT_REBUILD)


def verify_export(records):
    records = Path(records).resolve()
    report_path = records.parent / 'report.json'
    try:
        report = json.loads(report_path.read_text(encoding='utf-8-sig'))
        proof = report['cleaning_export']
        if (records.name != 'records.jsonl' or proof['passed_only'] is not True
                or proof['track'] not in ('dictionary', 'important') or not proof['evidence_sha256']):
            raise ValueError('Invalid cleaning export proof')
        if proof['track'] == 'dictionary':
            if type(proof['version']) is not int or proof['version'] != 1:
                raise ValueError('Invalid dictionary cleaning export proof')
        else:
            binding = proof.get('source_binding')
            if (type(proof['version']) is not int or proof['version'] != 3 or not isinstance(binding, dict)
                    or binding.get('contract') != 'initial_normalization'
                    or type(binding.get('version')) is not int or binding['version'] != 2
                    or binding.get('record_fingerprint_field') != RECORD_FINGERPRINT
                    or not isinstance(binding.get('normalization_report'), str)
                    or not isinstance(binding.get('cleaning_report'), str)):
                raise ValueError(IMPORTANT_REBUILD)
            initial_path = Path(binding['normalization_report'])
            if not initial_path.is_absolute() or str(initial_path.resolve()) not in proof['evidence_sha256']:
                raise ValueError(IMPORTANT_REBUILD)
            initial_path = initial_path.resolve()
            initial = json.loads(initial_path.read_text(encoding='utf-8-sig'))
            if not isinstance(initial, dict):
                raise ValueError(IMPORTANT_REBUILD)
            scope = initial.get('normalization')
            outputs = initial.get('output_sha256')
            if (not isinstance(scope, dict) or type(scope.get('version')) is not int or scope['version'] != 2
                    or scope.get('record_fingerprint_field') != RECORD_FINGERPRINT
                    or scope.get('subject') != proof['subject'] or not isinstance(outputs, dict)
                    or set(outputs) != {'records.jsonl', 'trace.jsonl'}):
                raise ValueError(IMPORTANT_REBUILD)
            initial_files = {initial['input']: initial['input_sha256'],
                             **{str(initial_path.parent / name): value for name, value in outputs.items()}}
            if any(not Path(p).is_absolute() or proof['evidence_sha256'].get(str(Path(p).resolve())) != digest
                   for p, digest in initial_files.items()):
                raise ValueError(IMPORTANT_REBUILD)
            cleaning_path = Path(binding['cleaning_report'])
            if not cleaning_path.is_absolute() or str(cleaning_path.resolve()) not in proof['evidence_sha256']:
                raise ValueError(IMPORTANT_REBUILD)
            cleaning = json.loads(cleaning_path.read_text(encoding='utf-8-sig'))
            if (not isinstance(cleaning, dict) or cleaning.get('stage') != 'model_clean'
                    or cleaning.get('subject') != proof['subject'] or not cleaning.get('finished_at')):
                raise ValueError(IMPORTANT_REBUILD)
            upstream_path = Path(cleaning['input'])
            if not upstream_path.is_absolute() or str(upstream_path.resolve()) not in proof['evidence_sha256']:
                raise ValueError(IMPORTANT_REBUILD)
            verify_record_fingerprints(initial_path.parent / 'records.jsonl', upstream_path)
        if set(report['output_sha256']) != {'records.jsonl', 'trace.jsonl'}:
            raise ValueError('Invalid cleaning output manifest')
        checks = {str(records.parent / name): value for name, value in report['output_sha256'].items()}
        checks.update(proof['evidence_sha256'])
        if any(not Path(p).is_absolute() or sha(p) != value for p, value in checks.items()):
            raise ValueError('changed cleaning export or evidence')
        count = sum(bool(line.strip()) for line in records.read_text(encoding='utf-8-sig').splitlines())
        if count != report['records']:
            raise ValueError('Cleaning record count mismatch')
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Missing or invalid cleaning export evidence') from exc
    return report
