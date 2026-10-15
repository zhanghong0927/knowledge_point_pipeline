"""Compressed, immutable previous-round bytes for NEW migrated dictionary runs."""
from contextlib import ExitStack
import gzip
import hashlib
import json
from pathlib import Path
import uuid

import resume_integrity as integrity


ROOT_OUTPUTS = {'MANIFEST.json', 'FINAL_RECORDS.jsonl', 'STANDARD_RECORDS.json', 'DISPOSITIONS.jsonl',
                'TECHNICAL_FAILURES.jsonl', 'REVIEW.jsonl', 'SUMMARY.json'}


def capture(out, round_number, previous, receipt_path):
    out = Path(out).resolve(strict=True)
    integrity._authorized_writer_receipt(out, receipt_path)
    state = integrity.read(out / 'STATE.json')
    if (round_number not in (2, 3) or state.get('status') != 'round_complete' or
            state.get('round') != round_number - 1 or previous.get('round') != round_number - 1 or
            previous.get('state') != 'awaiting_retry' or previous.get('next_round') != round_number or
            integrity.read(out / 'ORCHESTRATION_STATUS.json') != previous):
        raise ValueError('Round baseline requires the verified completed previous round, never a running mixture')
    body_ids = {row['id'] for row in integrity._journal_rows(out / 'DISPOSITIONS.jsonl')
                if row.get('content_status') == 'failed' and row.get('content_failure_kind') == 'transport'}
    selected = {}
    for relative, checksum in previous['checkpoint_hashes'].items():
        path = Path(relative)
        if (relative in ROOT_OUTPUTS or relative.startswith(('name/', 'scope/')) and
                path.name in ('FIRST.jsonl', 'FINAL.jsonl', 'SUMMARY.json') or
                relative.startswith('content/') and
                (path.stem in body_ids or path.name.endswith('_SUMMARY.json'))):
            selected[relative] = checksum
    folder = out.parent / (out.name + '_ROUND_BASELINES') / ('round_' + str(round_number)) / uuid.uuid4().hex
    folder.mkdir(parents=True)
    def compress(pair):
        relative, checksum = pair
        source = integrity._safe_relative(out, relative)
        if source.is_symlink():
            raise ValueError('Round baseline cannot capture symlinks')
        if relative.startswith('content/calls/'):
            call = integrity.read(source)
            if call.get('status') != 'failed' or call.get('failure_kind') != 'transport':
                return None
        target = integrity._safe_relative(folder, relative + '.gz')
        target.parent.mkdir(parents=True, exist_ok=True)
        hashed, size = hashlib.sha256(), 0
        with source.open('rb') as stream, target.open('xb') as compressed:
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, compresslevel=1, mtime=0) as output:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hashed.update(block)
                    size += len(block)
                    output.write(block)
        if hashed.hexdigest() != checksum:
            raise ValueError('Previous completed bytes changed before round baseline capture: ' + relative)
        return relative, {'path': str(target), 'sha256': checksum, 'size': size,
                          'gzip_sha256': integrity.digest(target)}
    files = dict(value for value in integrity._bounded_io(selected.items(), compress) if value is not None)
    if integrity.read(out / 'STATE.json') != state or integrity.read(out / 'ORCHESTRATION_STATUS.json') != previous:
        raise ValueError('Completed round changed during baseline capture')
    path = folder / 'BASELINE.json'
    integrity.save(path, {'schema': 1, 'target': str(out), 'round': round_number,
        'previous': previous, 'files': files, 'retryable_body_ids': sorted(body_ids),
        'manifest_identity': integrity._semantic_identity(integrity.read(out / 'MANIFEST.json')),
        'compression': 'gzip_level_1', 'captured_before_round': True,
        'normal_success_calls_copied': False, 'semantic_quality_approved': False})
    return {'path': str(path), 'sha256': integrity.digest(path)}


def _load(out, current_round):
    token = integrity.read(out / 'STATE.json').get('round_baseline')
    if not token:
        return None
    path = Path(token['path']).resolve(strict=True)
    allowed = out.parent / (out.name + '_ROUND_BASELINES')
    if not path.is_relative_to(allowed) or integrity.digest(path) != token['sha256']:
        raise ValueError('Round baseline receipt path/hash differs')
    value = integrity.read(path)
    if (value.get('schema') != 1 or value.get('target') != str(out) or value.get('round') != current_round or
            value.get('previous') != integrity.read(out / 'ORCHESTRATION_STATUS.json') or
            value.get('manifest_identity') != integrity._semantic_identity(integrity.read(out / 'MANIFEST.json')) or
            not value.get('captured_before_round')):
        raise ValueError('Round baseline previous-prefix or semantic identity differs')
    return value, token


def _baseline_entry(baseline, relative, expected):
    entry = baseline['files'].get(relative)
    if (not entry or entry.get('sha256') != expected or
            baseline['previous']['checkpoint_hashes'].get(relative) != expected or
            integrity.digest(entry['path']) != entry['gzip_sha256']):
        raise ValueError('Compressed round baseline does not prove the expected old prefix: ' + relative)
    hashed, ids, size = hashlib.sha256(), set(), 0
    with gzip.open(entry['path'], 'rb') as stream:
        for line in stream:
            hashed.update(line)
            size += len(line)
            if Path(relative).name in ('FIRST.jsonl', 'FINAL.jsonl'):
                sid = integrity._line_id(line)
                if sid in ids:
                    raise ValueError('Duplicate old baseline aggregate ID')
                ids.add(sid)
    if hashed.hexdigest() != expected or size != entry['size']:
        raise ValueError('Decompressed round baseline old SHA256 differs')
    return entry, ids


def _manifest_chain(out, old):
    receipt = integrity.read(out.parent / (out.name + '_RESUME_RECEIPT.json'))
    current = integrity.read(out / 'MANIFEST.json')
    if receipt.get('new_manifest') != current or integrity._semantic_identity(old) != integrity._semantic_identity(current):
        raise ValueError('Round baseline gateway manifest migration is not authorized')
    if old['code'] != current['code']:
        tokens = [item.get('code_migration_proof') for item in receipt.get('gateway_migrations', [])]
        for token in tokens:
            if token:
                try:
                    integrity.verify_code_migration(token, old['code'], current['code'])
                    break
                except ValueError:
                    continue
        else:
            raise ValueError('Round baseline code migration proof is missing')
    url = old['config']['api_url']
    for item in receipt.get('gateway_migrations', []):
        if item.get('from') == url:
            url = item['to']
    if url != current['config']['api_url']:
        raise ValueError('Round baseline gateway epoch chain is incomplete')


def _filter_merge(out, relative, entry, baseline_ids, current_round, native):
    folder = out / Path(relative).parent
    name = relative.startswith('name/')
    stem = 'llm_name_title_format' if name else 'subject_scope'
    script = native.ROOT / 'name_filters' / ('01_name_title_quality.py' if name else '02_subject_scope_filter.py')
    formatter = integrity._filter_module(script)
    primary, contexts = {}, {}
    for contextual, destination in ((False, primary), (True, contexts)):
        target = folder / ('round_' + str(current_round) + ('_review_context' if contextual else ''))
        for path in sorted(target.rglob(stem + '_judgments.jsonl')) if target.exists() else ():
            index = integrity._line_index(path.parent / 'input.jsonl')
            for vote in integrity._journal_rows(path):
                if vote['id'] in destination or vote['id'] not in index or not integrity._policy_matches(vote, formatter):
                    raise ValueError('Current round baseline merge has a foreign/duplicate/unverified receipt')
                destination[vote['id']] = (vote, path.parent / 'input.jsonl', index[vote['id']])
    if Path(relative).name == 'FIRST.jsonl':
        contexts = {}
    wanted, seen = baseline_ids | set(primary), set()
    with ExitStack() as stack, gzip.open(entry['path'], 'rb') as old, (out / relative).open('rb') as current:
        prior = old.readline()
        inputs = {}
        for line in current:
            sid = integrity._line_id(line)
            if sid not in wanted or sid in seen:
                raise ValueError('Current aggregate ID coverage differs from round baseline+journal')
            seen.add(sid)
            if sid in baseline_ids:
                if not prior or integrity._line_id(prior) != sid:
                    raise ValueError('Current aggregate reordered its previous-round IDs')
                previous_line, prior = prior, old.readline()
            else:
                previous_line = None
            update = contexts.get(sid) or primary.get(sid)
            if update:
                vote, input_file, (offset, number) = update
                if input_file not in inputs:
                    inputs[input_file] = stack.enter_context(input_file.open('rb'))
                stream = inputs[input_file]
                stream.seek(offset)
                expected = integrity._project_filter(formatter, json.loads(stream.readline()), vote, number)
                if json.loads(line) != expected:
                    raise ValueError('Current aggregate differs from round baseline+journal projection')
            elif previous_line != line:
                raise ValueError('Current aggregate changed without a current-round request receipt')
        if prior or old.read() or seen != wanted:
            raise ValueError('Current aggregate lost a previous-round/current-journal ID')


def history(out, relative, expected, current_round, native):
    out = Path(out).resolve(strict=True)
    loaded = _load(out, current_round)
    if loaded is None:
        return None
    baseline, token = loaded
    entry, ids = _baseline_entry(baseline, relative, expected)
    path = Path(relative)
    if relative == 'MANIFEST.json':
        with gzip.open(entry['path'], 'rt', encoding='utf-8-sig') as stream:
            _manifest_chain(out, json.load(stream))
    elif relative.startswith(('name/', 'scope/')) and path.name in ('FIRST.jsonl', 'FINAL.jsonl'):
        _filter_merge(out, relative, entry, ids, current_round, native)
    elif relative.startswith('content/calls/'):
        with gzip.open(entry['path'], 'rt', encoding='utf-8-sig') as stream:
            old = json.load(stream)
        current = integrity.read(out / relative)
        if (old.get('status') != 'failed' or old.get('failure_kind') != 'transport' or
                current.get('round') != current_round or current.get('status') not in ('ok', 'failed') or
                any(old.get(key) != current.get(key) for key in ('packet', 'prompt'))):
            raise ValueError('Current body receipt differs from an eligible same-round baseline retry')
    elif relative in ROOT_OUTPUTS or path.name == 'SUMMARY.json' or path.name.endswith('_SUMMARY.json'):
        # Derived summaries are incomplete and are recomputed by the same round;
        # they never supply production PASS or a completed-round claim here.
        pass
    elif relative.startswith('content/') and path.stem in baseline['retryable_body_ids']:
        value = integrity.read(out / relative)
        if relative.startswith('content/results/') and (value.get('id') != path.stem or
                value.get('status') not in ('failed', 'keep', 'drop', 'review')):
            raise ValueError('Current body result has a foreign identity/status')
    else:
        raise ValueError('Changed file is not covered by the previous-round mutable baseline')
    return {'file': relative, 'historical_sha256': expected, 'round_baseline': token,
            'method': 'compressed round baseline; exact old SHA256; current request journal verification',
            'current_round': current_round, 'round_complete': False}
