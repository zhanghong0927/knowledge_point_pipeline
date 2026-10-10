"""Hash-bound, pass-only cleaning exports for scheduler handoffs."""

import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seal_export(out, evidence, track, subject):
    return {'cleaning_export': {'version': 1, 'track': track, 'subject': subject,
            'passed_only': True, 'evidence_sha256': {str(Path(p).resolve()): sha(p) for p in evidence}},
            'output_sha256': {name: sha(out / name) for name in ('records.jsonl', 'trace.jsonl')}}


def verify_export(records):
    records = Path(records).resolve()
    report_path = records.parent / 'report.json'
    try:
        report = json.loads(report_path.read_text(encoding='utf-8-sig'))
        proof = report['cleaning_export']
        if (records.name != 'records.jsonl' or proof['version'] != 1 or proof['passed_only'] is not True
                or proof['track'] not in ('dictionary', 'important') or not proof['evidence_sha256']):
            raise ValueError('Invalid cleaning export proof')
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
