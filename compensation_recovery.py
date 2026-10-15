"""One evidenced lost opportunity in a private cache, followed by a sealed commit.

Regular round receipts keep their original numbers. No inference writes into the
regular cache; a write-ahead commit makes the selected backfill restartable.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import sys

import resume_integrity as integrity


def home(out):
    out = Path(out)
    return out.parent / (out.name + '_COMPENSATION')


def _jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')


def recover_commit(out):
    """Replay only old/new bytes named by the compensation write-ahead receipt."""
    out = Path(out).resolve(strict=True)
    active = home(out) / 'ACTIVE.json'
    if active.exists() and integrity.read(active).get('state') == 'committing':
        with integrity.writer_guard(out):
            return _recover_commit_locked(out)
    integrity.verify_writer_idle(out, allow_current=True)
    return None


def _recover_commit_locked(out):
    integrity.verify_writer_idle(out, allow_current=True)
    active = home(out) / 'ACTIVE.json'
    if not active.exists():
        return None
    token = integrity.read(active)
    if token.get('state') != 'committing':
        return None
    path = Path(token['commit']['path'])
    if integrity.digest(path) != token['commit']['sha256']:
        raise ValueError('Compensation commit receipt hash changed')
    commit = integrity.read(path)
    if commit.get('out') != str(out) or commit.get('plan_sha256') != token['plan_sha256']:
        raise ValueError('Compensation commit identity differs')
    integrity.load_compensation_plan(commit['plan_token'])
    for relative, change in commit['mutations'].items():
        target = integrity._safe_relative(out, relative)
        actual = integrity.digest(target) if target.is_file() else None
        if actual not in (change['old_sha256'], change['new_sha256']):
            raise ValueError('Compensation commit found an unrelated cache change: ' + relative)
        if integrity.digest(change['replacement']) != change['new_sha256']:
            raise ValueError('Compensation replacement hash changed')
    # ORCHESTRATION_STATUS is written after all its data; STATE follows it.
    ordered = sorted(commit['mutations'], key=lambda p: (p in ('ORCHESTRATION_STATUS.json', 'STATE.json'),
                                                        p == 'STATE.json', p))
    for relative in ordered:
        change = commit['mutations'][relative]
        target = integrity._safe_relative(out, relative)
        if target.is_file() and integrity.digest(target) == change['new_sha256']:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.compensation.tmp')
        with Path(change['replacement']).open('rb') as source, temporary.open('wb') as stream:
            shutil.copyfileobj(source, stream, 1024 * 1024)
        if integrity.digest(temporary) != change['new_sha256']:
            raise ValueError('Compensation replacement copy changed')
        temporary.replace(target)
    result = commit['result']
    integrity.save(path.parent / 'RESULT.json', result)
    integrity.save(active, {**token, 'state': 'committed',
                            'result': str(path.parent / 'RESULT.json')})
    return result


def _subset(path, wanted):
    if not Path(path).is_file():
        return []
    result = []
    with Path(path).open('rb') as stream:
        for line in stream:
            if integrity._line_id(line) in wanted:
                result.append(json.loads(line))
    return result


def _copy(path, target, checksum):
    if integrity.digest(path) != checksum:
        raise ValueError('Compensation seed cache changed')
    target.parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('rb') as source, target.open('wb') as stream:
        shutil.copyfileobj(source, stream, 1024 * 1024)
    if integrity.digest(target) != checksum:
        raise ValueError('Compensation seed copy changed')


def _prepare(cache, out, rows, files, native):
    marker = cache.parent / 'PREPARED.json'
    if marker.exists():
        return integrity.read(marker)
    wanted = {row['id'] for row in rows}
    old_votes = {}
    filters = {Path(key).parent.as_posix() for key in files
               if key.startswith(('name/', 'scope/')) and Path(key).name == 'FINAL.jsonl'}
    filters.add('name')
    for group in sorted(filters):
        annotation = native.NAME if group == 'name' else native.SCOPE
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            values = _subset(out / group / name, wanted)
            _jsonl(cache / group / name, values)
            if name == 'FINAL.jsonl':
                old_votes[group] = {row['id']: row[annotation] for row in values}
    body_files = {relative: (out / relative, data['sha256']) for relative, data in files.items()
                  if relative.startswith('content/') and Path(relative).suffix == '.json'
                  and Path(relative).stem in wanted}
    provenance = out / 'SOURCE_CACHE.json'
    if provenance.exists():
        sealed = integrity.read(provenance)
        for relative, checksum in sealed['files'].items():
            if (relative.startswith('content/') and Path(relative).suffix == '.json' and
                    Path(relative).stem in wanted and relative not in body_files):
                body_files[relative] = (integrity._safe_relative(sealed['source'], relative), checksum)
    for relative, (source, checksum) in body_files.items():
        _copy(source, cache / relative, checksum)
    result = {'ids': sorted(wanted), 'old_filter_votes': old_votes}
    integrity.save(marker, result)
    return result


def _merge_jsonl(source, target, replacements):
    """Preserve every unaffected original line byte for byte."""
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = dict(replacements)
    with target.open('wb') as output:
        if Path(source).exists():
            with Path(source).open('rb') as stream:
                for line in stream:
                    sid = integrity._line_id(line)
                    if sid in pending:
                        value = pending.pop(sid)
                        if value is not None:
                            output.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
                    else:
                        output.write(line)
        for value in pending.values():
            if value is not None:
                output.write((json.dumps(value, ensure_ascii=False) + '\n').encode())


def _technical(row):
    return (row['name'].get('api_status') not in ('ok', 'local') or
            row.get('scope') and row['scope'].get('api_status') not in ('ok', 'local') or
            row['content_status'] == 'failed')


def _review(row):
    return (row['name']['decision'] == 'review' or
            row.get('scope') and row['scope']['decision'] == 'review' or row['content_status'] == 'review')


def _make_backfill(out, cache, work, rows, names, scoped, reports, prepared, base, old_status, native, token):
    backfill = work / 'backfill'
    selected = {row['id'] for row in rows}
    nm, sm, cm = ({r['id']: r[native.NAME] for r in names},
                  {r['id']: r[native.SCOPE] for r in scoped}, {r['id']: r for r in reports})
    for aggregate in sorted(cache.glob('**/FINAL.jsonl')):
        group = aggregate.parent.relative_to(cache).as_posix()
        if not group.startswith(('name', 'scope/')):
            continue
        annotation = native.NAME if group == 'name' else native.SCOPE
        values = native.loadl(aggregate)
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            changes = {r['id']: r for r in native.loadl(aggregate.parent / name)}
            _merge_jsonl(out / group / name, backfill / group / name, changes)
        summary_path = out / group / 'SUMMARY.json'
        if summary_path.exists():
            old = integrity.read(summary_path)
            decisions, statuses = Counter(old['decisions']), Counter(old['api_status'])
            old_values = prepared['old_filter_votes'].get(group, {})
            for row in values:
                previous = old_values.get(row['id'])
                if previous:
                    decisions[previous['decision']] -= 1
                    statuses[previous['api_status']] -= 1
                decisions[row[annotation]['decision']] += 1
                statuses[row[annotation]['api_status']] += 1
            integrity.save(backfill / group / 'SUMMARY.json', {**old,
                'input': old['input'] + sum(r['id'] not in old_values for r in values),
                'decisions': dict(+decisions), 'api_status': dict(+statuses), 'compensation_applied': True})
    # New requests use their own namespace; no r1/r2/r3 request journal is replaced.
    for source in cache.rglob('*'):
        if source.is_file() and ('compensation_' in source.relative_to(cache).as_posix() or
                source.relative_to(cache).as_posix().startswith('content/') and source.suffix == '.json'
                and source.stem in selected):
            relative = source.relative_to(cache)
            _copy(source, backfill / relative, integrity.digest(source))
    def failure_kind(sid):
        if cm.get(sid, {}).get('status') != 'failed':
            return None
        return ('transport' if any(integrity.read(p).get('failure_kind') == 'transport'
                                  for p in (cache / 'content/calls').glob('*/' + sid + '.json')) else 'other')
    ledger = {r['id']: {'id': r['id'], 'name': nm[r['id']], 'scope': sm.get(r['id']),
              'content_status': cm.get(r['id'], {}).get('status', 'not_reached'),
              'content_failure_kind': failure_kind(r['id'])} for r in rows}
    _merge_jsonl(out / 'DISPOSITIONS.jsonl', backfill / 'DISPOSITIONS.jsonl', ledger)
    final_changes = {sid: cm[sid]['record'] if cm.get(sid, {}).get('status') == 'keep' else None for sid in selected}
    _merge_jsonl(out / 'FINAL_RECORDS.jsonl', backfill / 'FINAL_RECORDS.jsonl', final_changes)
    counts = Counter()
    for row in integrity._journal_rows(backfill / 'DISPOSITIONS.jsonl'):
        counts['input'] += 1
        counts['name_keep'] += native.is_passed(row['name'])
        counts['scope_keep'] += bool(row.get('scope') and native.is_passed(row['scope']))
        counts['technical_failures'] += bool(_technical(row))
        counts['retryable_technical_failures'] += bool(_technical(row) and (
            row['name'].get('failure_kind') == 'transport' or
            (row.get('scope') or {}).get('failure_kind') == 'transport' or
            row.get('content_failure_kind') == 'transport'))
        counts['review'] += bool(_review(row))
        if row['content_status'] != 'not_reached':
            counts['content:' + row['content_status']] += 1
    _jsonl(backfill / 'TECHNICAL_FAILURES.jsonl', (row for row in integrity._journal_rows(
        backfill / 'DISPOSITIONS.jsonl') if _technical(row)))
    _jsonl(backfill / 'REVIEW.jsonl', (row for row in integrity._journal_rows(
        backfill / 'DISPOSITIONS.jsonl') if _review(row)))
    final_count, name_only = 0, 0
    with (backfill / 'STANDARD_RECORDS.json').open('w', encoding='utf-8', newline='\n') as stream:
        stream.write('[')
        for row in integrity._journal_rows(backfill / 'FINAL_RECORDS.jsonl'):
            if final_count:
                stream.write(',\n')
            stream.write(json.dumps({key: row.get(key, '') for key in native.STANDARD}, ensure_ascii=False))
            final_count += 1
            name_only += not any(row.get(key) for key in native.STANDARD[3:7])
        stream.write(']\n')
    consumed = []
    plan = integrity.load_compensation_plan(token)
    for row in plan['eligible']:
        if integrity._claim_path(token, row).exists():
            consumed.append({key: row.get(key) for key in ('id', 'stage', 'group', 'vote_phase', 'history_round')})
    result = {'schema': 1, 'plan_sha256': token['sha256'], 'regular_round': old_status['round'],
        'eligible_requests': len(plan['eligible']), 'opportunities_consumed': len(consumed),
        'consumed': consumed, 'not_consumed': len(plan['eligible']) - len(consumed),
        'final_keep': final_count, 'technical_failures': counts['technical_failures'],
        'retryable_technical_failures': counts['retryable_technical_failures'],
        'review': counts['review'], 'round_numbers_rewritten': False, 'semantic_quality_approved': False}
    old_summary = integrity.read(out / 'SUMMARY.json')
    summary = {**old_summary, **{key: counts[key] for key in
        ('input', 'name_keep', 'scope_keep', 'technical_failures', 'review')},
        'final_keep': final_count, 'name_only': name_only,
        'content_statuses': {key[8:]: value for key, value in counts.items() if key.startswith('content:')},
        'round': old_status['round'], 'compensation': result, 'semantic_quality_approved': False}
    integrity.save(backfill / 'SUMMARY.json', summary)
    integrity.save(backfill / 'content/results_SUMMARY.json', {'input': sum(summary['content_statuses'].values()),
                                                            'statuses': summary['content_statuses']})
    return backfill, result, summary


def run(input_path, cfg, out, books, plan_path, plan_sha256, native):
    out = Path(out).resolve(strict=True)
    integrity.verify_writer_idle(out, clear_stale=True)
    recovered = recover_commit(out)
    token = {'path': str(Path(plan_path).resolve(strict=True)), 'sha256': plan_sha256}
    plan = integrity.load_compensation_plan(token)
    if Path(plan['current']).resolve() != out or plan['revocation'].get('event') != 'service_deployment_deregistered':
        raise ValueError('Compensation needs the exact new cache and verified upstream deployment retirement')
    integrity._verify_deployment_proof(plan['revocation'], Path(plan['original']).resolve(strict=True))
    expected = native.staged_manifest(input_path, cfg, books)
    if expected != integrity.read(out / 'MANIFEST.json'):
        raise ValueError('Compensation input/model/context/source/code identity changed')
    work = home(out) / plan_sha256
    if (work / 'RESULT.json').exists():
        return integrity.read(work / 'RESULT.json')
    receipt = out.parent / (out.name + '_RESUME_RECEIPT.json')
    sealed = integrity.read(receipt)
    if Path(sealed['source']).resolve() == out or sealed.get('new_manifest') != integrity.read(out / 'MANIFEST.json'):
        raise ValueError('Compensation must run in an explicitly migrated independent cache')
    integrity.verify_retained_passes(out, receipt)
    base = integrity.snapshot_files(out)
    status = integrity.read(out / 'ORCHESTRATION_STATUS.json')
    if (status.get('round') != 3 or status.get('state') not in ('ready_to_finalize', 'technical_failure') or
            integrity.read(out / 'STATE.json').get('status') != 'round_complete' or
            integrity.stable_checkpoint_hashes(base) != status.get('checkpoint_hashes')):
        raise ValueError('Compensation requires a complete verified final regular round')
    for row in plan['eligible']:
        path = row.get('current_artifact') or integrity._safe_relative(out, row['request_file'])
        integrity._verify_compensation_artifact(path, row.get('expected_hash') or row['current_sha256'])
    selected = {row['id'] for row in plan['eligible']}
    rows = [row for row in integrity._array_rows(input_path) if row['id'] in selected]
    if {row['id'] for row in rows} != selected:
        raise ValueError('Compensation plan contains foreign input IDs')
    cache = work / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    session_path = work / 'SESSION.json'
    identity = {'semantic': integrity._semantic_identity(expected), 'code': expected['code']}
    session = integrity.read(session_path) if session_path.exists() else {
        'identity': identity, 'api_url': cfg['api_url'], 'gateway_migrations': []}
    if session['identity'] != identity:
        raise ValueError('Compensation subset input/model/context/source/prompt code identity changed')
    if session['api_url'] != cfg['api_url']:
        session['gateway_migrations'].append({'from': session['api_url'], 'to': cfg['api_url'],
                                             'plan_sha256': plan_sha256})
        session['api_url'] = cfg['api_url']
    integrity.save(session_path, session)
    prepared = _prepare(cache, out, rows, base, native)
    config = {key: value for key, value in cfg.items() if key not in ('recovery_source', 'recovery_provenance')}
    config.update(staged_round=status['round'], compensation_mode=True, resume_current_round=True,
        compensation_cache=str(cache), compensation_token=token, compensation_ids=sorted(selected))
    integrity.save(home(out) / 'ACTIVE.json', {'schema': 1, 'state': 'executing', 'plan_sha256': plan_sha256})
    if selected:
        integrity.models_preflight(config, home(out) / 'SERVICE_PAUSE.json', status['round'],
                                   open_fn=native.urllib.request.urlopen)
    import clean_boundary_v46 as content
    with integrity.resumed_book_lock(out, receipt):
        names = native.filter_stage(rows, 'name', config, cache / 'name')
        groups = defaultdict(list)
        scope_map = {book['identifier']: str(Path(book['scope_config'])) for book in books}
        for row in names:
            if native.is_passed(row[native.NAME]):
                groups[scope_map[row['source']['identifier']]].append(row)
        scoped = []
        for scope_path, values in sorted(groups.items()):
            group = 'scope/group_' + hashlib.sha256(scope_path.encode()).hexdigest()[:12]
            if not (cache / group / 'FIRST.jsonl').exists():
                _jsonl(cache / group / 'FIRST.jsonl', [])
                _jsonl(cache / group / 'FINAL.jsonl', [])
            scoped.extend(native.filter_stage(values, 'scope', config, cache / group, scope_path))
        passed = [row for row in scoped if native.is_passed(row[native.SCOPE])]
        for row in passed:
            row['head'] = ' '.join(value for value in (row.get('knowledge_point'), row.get('name')) if value)
        (cache / 'content').mkdir(exist_ok=True)
        runner = content.Runner(cache / 'content', config)
        reports = runner.batch(passed, 'results', runner.content)
        backfill, result, summary = _make_backfill(out, cache, work, rows, names, scoped, reports,
                                                  prepared, base, status, native, token)
        # No original-cache byte changes are permitted while the isolated work ran.
        if integrity.snapshot_files(out) != base:
            raise ValueError('Regular cache changed while compensation was isolated')
        baseline = integrity.retained_passed_baseline(out)
        integrity.verify_retained_passes(backfill, receipt)
        after_passed = integrity.retained_passed_baseline(backfill)
        if any(after_passed['records'].get(sid) != checksum for sid, checksum in baseline['records'].items()):
            raise ValueError('Compensation attempted to change a previously passed record')
        hashes = integrity.stable_checkpoint_hashes(base)
        for path in backfill.rglob('*'):
            if (path.is_file() and path.name not in ('STATE.json', 'ORCHESTRATION_STATUS.json', 'PROGRESS.json', '.running')
                    and path.suffix not in ('.log', '.tmp', '.lock')):
                hashes[path.relative_to(backfill).as_posix()] = integrity.digest(path)
        state = 'technical_failure' if summary['technical_failures'] else 'ready_to_finalize'
        integrity.save(backfill / 'ORCHESTRATION_STATUS.json', {**status, 'state': state, 'status': state,
            'next_round': None, 'technical_failures': summary['technical_failures'], 'review': summary['review'],
            'retryable_technical_failures': result['retryable_technical_failures'],
            'final_keep': summary['final_keep'], 'compensation': result, 'checkpoint_hashes': hashes})
        integrity.save(backfill / 'STATE.json', {'status': 'round_complete', **summary})
        mutations = {}
        for path in backfill.rglob('*'):
            if path.is_file():
                relative = path.relative_to(backfill).as_posix()
                mutations[relative] = {'old_sha256': base.get(relative, {}).get('sha256'),
                    'new_sha256': integrity.digest(path), 'replacement': str(path)}
        commit_path = work / 'COMMIT.json'
        integrity.save(commit_path, {'schema': 1, 'out': str(out), 'plan_sha256': plan_sha256,
            'plan_token': token, 'mutations': mutations, 'result': result})
        integrity.save(home(out) / 'ACTIVE.json', {'schema': 1, 'state': 'committing',
            'plan_sha256': plan_sha256, 'commit': {'path': str(commit_path), 'sha256': integrity.digest(commit_path)}})
        return recover_commit(out)
