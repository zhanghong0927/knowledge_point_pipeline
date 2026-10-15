#!/usr/bin/env python3
"""Explicit, stage-separated 16-card benchmark entrypoint; plan never starts services."""

import argparse
from dataclasses import replace
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from adapters.gateway_control import transition_phase
from batch_pipeline import Gateway, bind_model_routes, execute_job
from benchmark_support import (Recorder, collect_approved, configure, now, read, save, sha,
                               verify_original_rows)
from stage_scheduling import ResourceProfile, run_simple_phase, run_bounded_phase, select_admissions
from pipeline import mounting_review_enabled
from round_integrity import with_verification_session

ROOT = Path(__file__).resolve().parent


def require_executed_code_root(root):
    code = (Path(root) / 'code').resolve(strict=True)
    if not code.is_dir() or ROOT.resolve() != code:
        raise ValueError('Staged entrypoint must execute from RUN/code')
    import batch_pipeline
    import benchmark_support
    import cross_stage_benchmark
    import pipeline
    import stage_scheduling
    import adapters.gateway_control as gateway_control
    for module in (batch_pipeline, benchmark_support, cross_stage_benchmark,
                   pipeline, stage_scheduling, gateway_control):
        if not Path(module.__file__).resolve().is_relative_to(code):
            raise ValueError(f'Imported module outside RUN/code: {module.__name__}')
        if hasattr(module, 'ROOT') and Path(module.ROOT).resolve() != code:
            raise ValueError(f'Module ROOT differs from RUN/code: {module.__name__}')
    return code


def code_snapshot():
    """Hash the actual checkout used by execute_job, including native resources."""
    excluded = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.venv',
                'validation_logs', '.coverage', 'htmlcov'}
    files = {}
    for path in ROOT.rglob('*'):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & excluded):
            relative = path.relative_to(ROOT).as_posix()
            files[relative] = sha(path)
    return {'schema': 1, 'root': str(ROOT.resolve()), 'files': dict(sorted(files.items()))}


def verify_code_snapshot(path):
    saved = read(path)
    current = code_snapshot()
    if (not isinstance(saved, dict) or saved.get('schema') != 1 or
            saved.get('root') != current['root'] or
            saved.get('files') != current['files']):
        raise ValueError('CODE_SNAPSHOT mismatch with the actual executed checkout')
    return saved


def run_phase_sequence(api_url, phases, transition):
    """Enter each exclusive phase only after the preceding pool has joined."""
    results = {}
    for name, limit, items, worker, workers in phases:
        transition(api_url, limit, name)
        results[name] = run_simple_phase(items, worker, workers)
    return results


def technical_retry_refs(rows):
    return sorted(row["ref"] for row in rows if row.get("status") == "technical_failed")


def validate_staged_subject(subject, registry):
    """Require a canonical slug backed by a configured taxonomy file."""
    details = registry.get(subject)
    if not isinstance(details, dict) or not details.get('file'):
        raise ValueError(f'Unsupported staged subject taxonomy: {subject}')
    return subject


def native_round_state(path, returncode):
    path = Path(path)
    if not path.is_file():
        return {'status': 'technical_failure', 'reason': 'missing_status', 'returncode': returncode}
    value = read(path)
    state = value.get('state', value.get('status'))
    if returncode not in (0, 2) or state not in ('awaiting_retry', 'ready_to_finalize', 'technical_failure'):
        return {'status': 'technical_failure', 'reason': 'invalid_status_or_exit', 'returncode': returncode}
    if state == 'awaiting_retry' and value.get('next_round') not in (2, 3):
        return {'status': 'technical_failure', 'reason': 'invalid_next_round', 'returncode': returncode}
    return {'status': state, 'returncode': returncode, 'native': value}


def dictionary_rounds(books, run_round, transition, api_url, limits,
                      workers=(8, 8, 8)):
    return cohort_transport_rounds(books, run_round, transition, api_url,
                                   limits, workers, 'dictionary_extraction')


@with_verification_session
def cohort_transport_rounds(books, run_round, transition, api_url, limits,
                            workers, label):
    """Run all eligible books in a round before any book enters the next one."""
    if len(limits) != 3 or len(workers) != 3:
        raise ValueError("Three native round limits and worker pools are required")
    pending = list(books)
    results = {}
    for number, (limit, count) in enumerate(zip(limits, workers), 1):
        if not pending:
            break
        transition(api_url, limit, f"{label}_round_{number}")
        cohort = pending
        pending = []
        outcomes = run_simple_phase(cohort, lambda book: (book, run_round(book, number)), count)
        for book, result in outcomes:
            if (result['status'] == 'awaiting_retry' and 'native' in result and
                    result['native'].get('next_round') != number + 1):
                result = {'status': 'technical_failure', 'reason': 'next_round_mismatch',
                          'native': result['native']}
            results[book] = result
            if result["status"] == "awaiting_retry":
                pending.append(book)
    return results


def job_id(track, kind, identifier):
    return track + '_' + kind + '_' + hashlib.sha256(identifier.encode()).hexdigest()[:24]


def native_task(job, stage, name):
    from pipeline import load_config, plan
    config, values = load_config(Path(job['config']))
    tasks = next(item['tasks'] for item in plan(config, values) if item['stage'] == stage)
    return next(item for item in tasks if item['name'] == name)


def native_task_names(job, stage):
    from pipeline import load_config, plan
    config, values = load_config(Path(job['config']))
    return [task['name'] for item in plan(config, values) if item['stage'] == stage
            for task in item['tasks']]


def run_command(home, label, command, *, env_extra=None, cwd=ROOT):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / 'staged_native.log').open('a', encoding='utf-8') as log:
        log.write(f'\n[{now()}] {label}\n')
        log.flush()
        env = os.environ.copy()
        env.update(env_extra or {})
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
        completed = subprocess.run([str(part) for part in command], cwd=cwd,
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
    return completed.returncode


def selected_job(job, stage, *tasks, retry_failed=False):
    return execute_job({**job, 'stages': [stage], 'tasks': list(tasks),
                        'retry_failed': retry_failed})


def screening_status_path(job, number, phase=None):
    base = Path(job['home']) / 'run/screening'
    if number == 3:
        return base / '03_important_metadata/ORCHESTRATION_STATUS.json'
    if number == 5:
        return base / '05_important_md/ORCHESTRATION_STATUS.json'
    if number == 6 and phase in ('primary', 'challenge'):
        return base / '05_dictionary_md' / (phase.upper() + '_ORCHESTRATION_STATUS.json')
    raise ValueError('Unknown screening model phase')


def run_screening_native(job, number, *, round_number=None, finalize=False,
                         workers=None, phase=None):
    stage = '01' if number == 3 else '02'
    task = native_task(job, stage, ('screening_' if number == 3 else 'md_audit_') + str(number))
    env = {**task['env']}
    if round_number is not None:
        env['STAGED_SCREENING_ROUND'] = str(round_number)
    if finalize:
        env['STAGED_SCREENING_FINALIZE'] = '1'
    if phase:
        env['STAGED_SCREENING_PHASE'] = phase
    if workers:
        env['STAGED_SCREENING_WORKERS'] = str(workers)
    code = run_command(job['home'], f'screening_{number}_{phase or "model"}_'
                       f'{"finalize" if finalize else round_number}', task['command'],
                       env_extra=env, cwd=task['cwd'])
    status_path = screening_status_path(job, number, phase)
    if finalize:
        status = read(status_path) if status_path.is_file() else {}
        return {'status': 'completed' if code == 0 and status.get('state') == 'finalized'
                else 'failed', 'returncode': code, 'native': status}
    return native_round_state(status_path, code)


def screening_model_cohort(root, track, jobs, gateway, profile, recorder,
                           number, phase=None):
    by_id = {job['job_id']: job for job in jobs}
    label = f'{track}_screening_{number}' + ('_' + phase if phase else '')
    rounds = cohort_transport_rounds(list(by_id),
        lambda key, n: run_screening_native(by_id[key], number, round_number=n,
                                            workers=profile.job_workers('front', n),
                                            phase=phase),
        transition_phase, gateway.api_url, profile.round_limits_for('front'),
        tuple(profile.phase_workers('front', n) for n in (1, 2, 3)), label)
    save(root / track / (label.upper() + '_ROUNDS.json'), rounds)
    finalizable = [job for job in jobs if rounds[job['job_id']]['status'] in
                   ('ready_to_finalize', 'technical_failure') and
                   'native' in rounds[job['job_id']]]
    if number != 6 or phase == 'challenge':
        final = run_simple_phase(finalizable,
            lambda job: run_screening_native(job, number, finalize=True,
                                             workers=profile.job_workers('front'), phase=phase),
            profile.cpu_workers)
        finalizable = [job for job, result in zip(finalizable, final)
                       if result['status'] == 'completed']
    return finalizable, rounds


def recorded_phase_transition(recorder, api_url, limit, name):
    """Record the same phase entered by the gateway without rewriting job manifests."""
    gateway = transition_phase(api_url, limit, name)
    status = recorder.root / 'STATUS.json'
    if status.is_file():
        with recorder.lock:
            recorder.state.update(read(status))
    recorder.update(phase=name, phase_limit=limit)
    event = {'phase': name, 'phase_limit': limit, 'status': 'started', 'at': now(),
             'gateway': gateway}
    # Recovery JOBS.json is a frozen list, whereas Recorder.event owns a dict.
    # Phase events have their own journal so neither manifest format is replaced.
    with recorder.lock:
        with (recorder.root / 'PHASE_EVENTS.jsonl').open('a', encoding='utf-8') as out:
            out.write(json.dumps(event, ensure_ascii=False) + '\n')
    save(recorder.root / 'PHASES' / (name + '.json'), event)
    return gateway


def phase_jobs(api_url, name, limit, jobs, task, workers, recorder):
    recorded_phase_transition(recorder, api_url, limit, name)
    results = run_simple_phase(jobs, task, workers)
    save(recorder.root / 'PHASES' / (name + '.json'),
         {'phase': name, 'limit': limit, 'finished_at': now(), 'results': results})
    return results


def run_structure_jobs(jobs, profile):
    """Bound global CPU preparation independently of the model book slots."""
    def prepare(pair):
        index, job = pair
        result = selected_job(job, '02', 'convert_audited_books', 'prepare_structure')
        return index, job, result

    def classify(prepared):
        index, job, result = prepared
        if result['status'] == 'completed':
            result = selected_job(job, '02', 'classify')
        return index, result

    outcomes = run_bounded_phase(enumerate(jobs), prepare, classify,
        prepare_workers=profile.cpu_workers, process_workers=profile.phase_workers('front'),
        max_pending=max(profile.max_pending, profile.cpu_workers))
    return [value for _, value in sorted(outcomes, key=lambda pair: pair[0])]


def classification_failures(job):
    folder = Path(job['home']) / 'run/02_structure/classification/results'
    if not folder.is_dir():
        return []
    rows = [read(path) for path in folder.glob('*.json')]
    return technical_retry_refs(rows)


def stream_reconciliation_ready(job):
    base = Path(job['home']) / 'run/02_structure'
    prepared = base / 'prepared/manifest.json'
    classified = base / 'classification/manifest.json'
    if not prepared.is_file() or not classified.is_file():
        return False
    targets = read(prepared).get('targets')
    if not isinstance(targets, list) or len(set(targets)) != len(targets):
        return False
    results = base / 'classification/results'
    return {path.stem for path in results.glob('*.json')} == set(targets)


def retryable_classification_failures(job):
    prepared = Path(job['home']) / 'run/02_structure/prepared/prepared'
    results = Path(job['home']) / 'run/02_structure/classification/results'
    return [ref for ref in classification_failures(job)
            if (prepared / (ref + '.json')).is_file() and
            read(results / (ref + '.json')).get('failure_kind') == 'transport']


def retry_classification(job, track, workers):
    home = Path(job['home'])
    base = home / 'run/02_structure/prepared'
    output = home / 'run/02_structure/classification'
    config = home / 'inputs/model.json'
    if not retryable_classification_failures(job):
        return {'job_id': job['job_id'], 'status': 'no_technical_failures'}
    if track == 'dictionary':
        command = [sys.executable, '-B', ROOT / 'modules/dictionary/classification/scripts/classify_books.py',
                   'retry-technical', '--base', base, '--out', output, '--config', config,
                   '--books', home / 'run/02_structure/book_manifest/books.json', '--workers', workers]
    else:
        command = [sys.executable, '-B', ROOT / 'modules/important_books/scripts/layout_model_validation.py',
                   'run', '--base', base, '--out', output, '--config', config, '--workers', workers]
    code = run_command(home, 'classification_technical_retry', command)
    return {'job_id': job['job_id'], 'status': 'completed' if code == 0 else 'failed',
            'returncode': code, 'remaining': classification_failures(job)}


def prepare_front(root, service, gateway, profile, track, candidates):
    rows = read(candidates)
    registry = read(root / 'code/configs/taxonomy_registry.json')
    for row in rows:
        validate_staged_subject(row['subject_slug'], registry)
    ids = [row['identifier'] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate candidate identifiers')
    def prepare(row):
        identifier = row['identifier']
        subject = row['subject_slug']
        home = root / track / 'front' / subject / identifier
        job = configure(root, service, track, subject, [row], home,
                        job_id(track, 'front', identifier),
                        gateway.job_url(job_id(track, 'front', identifier)),
                        workers=profile.job_workers('front'),
                        global_limit=profile.global_limit, defer_transport_retry=True,
                        structure_streaming=False,
                        cleaning_workers=profile.job_workers('cleaning'),
                        mounting_workers=profile.job_workers('mounting'))
        return {**job, 'row': row}
    return run_simple_phase(rows, prepare, profile.cpu_workers)


def prepare_production(root, service, gateway, profile, track, selected):
    jobs = []
    for item in selected:
        from cross_stage_benchmark import revalidate_admission
        revalidate_admission(item)
        book, parent = item['book'], item['parent_job']
        identifier = book['identifier']
        if track == 'dictionary':
            home = root / track / 'production' / item['subject'] / identifier
            native_id = job_id(track, 'production', identifier)
            job = configure(root, service, track, item['subject'],
                            [next(row for row in read(root / 'inputs/dictionary_candidates.json')
                                  if row['identifier'] == identifier)],
                            home, native_id, gateway.job_url(native_id),
                            workers=profile.job_workers('production'), selected=[book],
                            global_limit=profile.global_limit,
                            round_workers=[profile.job_workers('production', n) for n in (1, 2, 3)],
                            defer_transport_retry=True)
        else:
            job = dict(parent)
            home = Path(job['home'])
            save(home / 'inputs/selected_books.json', [book])
        save(home / 'FRESH_APPROVAL_BINDING.json',
             {'approval_proof': item['approval_proof'], 'approval_sha256': item['approval_sha256'],
              'selected_book': book, 'source_revalidated': True})
        jobs.append({**job, 'admission': item})
    return jobs


def run_dictionary_round(job, number):
    from cross_stage_benchmark import revalidate_admission
    revalidate_admission(job['admission'])
    task = native_task(job, '03', 'extract_fullbook')
    command = list(task['command'])
    adapter_index = next(i for i, part in enumerate(command) if str(part).endswith('dictionary_extraction.py'))
    native_args = [part for part in command[adapter_index + 1:] if part != '--allow-partial']
    command = [sys.executable, '-B', ROOT / 'modules/dictionary/src/run_fullbook_v5.py',
               *native_args, '--only-round', str(number)]
    code = run_command(job['home'], f'extraction_round_{number}', command)
    status_path = Path(job['home']) / 'run/03_extraction/raw/ORCHESTRATION_STATUS.json'
    return {**native_round_state(status_path, code), 'job_id': job['job_id']}


def finalize_dictionary(job):
    task = native_task(job, '03', 'extract_fullbook')
    code = run_command(job['home'], 'extraction_finalize', [*task['command'], '--finalize-only'])
    return {'job_id': job['job_id'], 'status': 'completed' if code == 0 else 'failed',
            'returncode': code}


def extraction_handoff_totals(jobs):
    partial_books = unresolved_items = 0
    for job in jobs:
        report = read(Path(job['home']) / 'run/03_extraction/raw/HANDOFF.json')
        partial_books += report['partial_books']
        unresolved_items += report['unresolved_items']
    return partial_books, unresolved_items


def count_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return sum(bool(line.strip()) for line in stream)


def final_output_totals(jobs):
    counts, missing = [], []
    for job in jobs:
        retained = Path(job['home']) / 'run/06_dedup/retained.jsonl'
        if retained.is_file():
            counts.append(count_jsonl(retained))
        else:
            missing.append(job['job_id'])
    return {'final_retained_count': sum(counts) if counts and not missing else None,
            'final_exports_available': bool(counts) and not missing,
            'missing_exports': missing}


def mounting_dispositions(jobs):
    transport_unresolved = 0
    counts = defaultdict(int, {key: 0 for key in
                              ('review', 'technical_failure', 'unconfigured', 'missing_summary')})
    format_or_semantic_unresolved = 0
    for job in jobs:
        mount_dir = Path(job['home']) / 'run/05_mounting'
        export_summary = mount_dir / 'SUMMARY.json'
        summary = read(export_summary) if export_summary.is_file() else {}
        review_enabled = summary.get('mounting_review_enabled', True)
        for task in ('boundary', 'route', *(['review'] if review_enabled else [])):
            status = mount_dir / task / 'ORCHESTRATION_STATUS.json'
            if status.is_file():
                current = read(status)
                transport_unresolved += current.get(
                    'transport_unresolved', current.get('pending_transport', 0))
        if not export_summary.is_file():
            counts['missing_summary'] += 1
        else:
            for disposition, count in summary.get('counts', {}).items():
                counts[disposition] += count
        reviewed_summary = mount_dir / 'REVIEWED.json'
        if review_enabled and reviewed_summary.is_file():
            format_or_semantic_unresolved += read(reviewed_summary).get(
                'format_or_semantic_unresolved', 0)
    return transport_unresolved, dict(counts), format_or_semantic_unresolved


def with_workers(command, workers):
    command = list(command)
    command[command.index('--workers') + 1] = str(workers)
    return command


def run_dictionary_clean_round(job, number, workers):
    from cross_stage_benchmark import revalidate_admission
    revalidate_admission(job['admission'])
    task = native_task(job, '04', 'clean_dictionary')
    command = [*with_workers(task['command'], workers), '--only-round', str(number)]
    code = run_command(job['home'], f'cleaning_round_{number}', command)
    status = Path(job['home']) / 'run/04_cleaning/dictionary/ORCHESTRATION_STATUS.json'
    return {**native_round_state(status, code), 'job_id': job['job_id']}


def finalize_dictionary_clean(job):
    task = native_task(job, '04', 'clean_dictionary')
    code = run_command(job['home'], 'cleaning_finalize', [*task['command'], '--finalize-only'])
    if code:
        return {'job_id': job['job_id'], 'status': 'failed', 'returncode': code}
    result = selected_job(job, '04', 'clean_dictionary')
    return {'job_id': job['job_id'], 'status': result['status'], 'returncode': code}


def freeze_important_clean_identity(job, command):
    command = [str(part) for part in command]
    semantic_args = []
    skip = False
    for part in command[2:]:
        if skip:
            skip = False
            continue
        if part == '--workers':
            skip = True
            continue
        semantic_args.append(part)
    paths = [Path(job['config']), Path(command[1])]
    for flag in ('--input', '--taxonomy'):
        paths.append(Path(command[command.index(flag) + 1]))
    identity = {'semantic_argv': semantic_args,
                'sha256': {str(path.resolve()): sha(path) for path in paths}}
    frozen = Path(job['home']) / 'FROZEN_MODEL_CLEAN_IDENTITY.json'
    if frozen.is_file():
        if read(frozen) != identity:
            raise ValueError('Important model cleaning retry identity changed')
    else:
        save(frozen, identity)
    return identity


def important_clean_state(job, result=None):
    """Separate transport retries and unfinished exports from valid empty/drop results."""
    home = Path(job['home'])
    output = home / 'run/04_cleaning/current/02_model_clean'
    names = ('clean_standard.jsonl', 'model_clean_report.json', 'model_judgments.jsonl')
    issues = ['missing_' + name for name in names if not (output / name).is_file()]
    decisions = defaultdict(int)
    transport = judgments = 0
    path = output / 'model_judgments.jsonl'
    if path.is_file():
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    value = json.loads(line)['model_cleaning']
                    decisions[value['decision']] += 1
                    judgments += 1
                    transport += (value['decision'] == 'error' and
                                  value.get('failure_kind') == 'transport')
        except (OSError, ValueError, KeyError, TypeError):
            issues.append('invalid_model_judgments')
    report_path = output / 'model_clean_report.json'
    if report_path.is_file():
        try:
            report = read(report_path)
            source = home / 'run/04_cleaning/current/01_rule_clean/rule_pass.jsonl'
            if (report.get('processed') != judgments or report.get('counts') != dict(decisions) or
                    (source.is_file() and report.get('processed') != count_jsonl(source)) or
                    report.get('missing_checkpoint_result', 0)):
                issues.append('incomplete_model_report')
            clean = output / 'clean_standard.jsonl'
            if clean.is_file() and count_jsonl(clean) != decisions.get('keep', 0):
                issues.append('incomplete_clean_standard')
        except (OSError, ValueError, AttributeError, TypeError):
            issues.append('invalid_model_report')
    state_path = home / 'run/pipeline_state.json'
    receipt = read(state_path).get('04.model_clean', {}) if state_path.is_file() else {}
    # An explicit child result supersedes the previous native receipt while a retry
    # is being validated; the receipt is reconciled only after those checks pass.
    attempt = result if result is not None else receipt
    if attempt and (attempt.get('status') != 'completed' or
                    attempt.get('returncode', 0) not in (0, None)):
        issues.append('incomplete_child_receipt')
    return {'transport_errors': transport, 'issues': issues,
            'pending': max(transport, int(bool(issues))), 'receipt': receipt}


def reconcile_important_clean_receipt(job, task, retry):
    path = Path(job['home']) / 'run/pipeline_state.json'
    state = read(path) if path.is_file() else {}
    previous = state.get('04.model_clean', {})
    signature = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
    if previous.get('signature') not in (None, signature):
        raise ValueError('Important model cleaning task receipt signature changed')
    state['04.model_clean'] = {**previous, 'status': 'completed', 'returncode': 0,
        'signature': signature, 'command': task['command'], 'native_transport_retry': retry}
    save(path, state)


def verify_important_clean_checkpoint(job):
    """Refuse to repeat resolved judgments if their native resume cache is lost."""
    import sqlite3
    from contextlib import closing
    from modules.cleaning.scripts.model_clean import MODEL_POLICY_VERSION
    output = Path(job['home']) / 'run/04_cleaning/current/02_model_clean'
    judgments = output / 'model_judgments.jsonl'
    if not judgments.is_file():
        return
    retained = []
    with judgments.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            result = row['model_cleaning']
            if result.get('decision') == 'error' and result.get('failure_kind') == 'transport':
                continue
            key = row.get('cleaning_record_id')
            if not key or result.get('policy_version') != MODEL_POLICY_VERSION:
                raise ValueError('Important cleaning checkpoint judgment identity or policy changed')
            retained.append((str(key), result))
    if not retained:
        return
    checkpoint = output / 'model_clean_checkpoint.sqlite'
    if not checkpoint.is_file():
        raise ValueError('Important cleaning checkpoint missing for resolved judgments')
    try:
        with closing(sqlite3.connect(checkpoint.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            for key, result in retained:
                saved = connection.execute(
                    'SELECT decision, payload, policy_version FROM results WHERE record_key = ?',
                    (key,)).fetchone()
                if (saved is None or saved[0] != result.get('decision') or
                        saved[2] != MODEL_POLICY_VERSION or json.loads(saved[1]) != result):
                    raise ValueError('Important cleaning checkpoint differs from resolved judgments')
    except sqlite3.Error as exc:
        raise ValueError('Important cleaning checkpoint is unreadable') from exc


def retry_important_clean(job, workers, previous_result=None):
    task = native_task(job, '04', 'model_clean')
    freeze_important_clean_identity(job, task['command'])
    if not important_clean_transport_errors(job, previous_result):
        return {'job_id': job['job_id'], 'status': 'no_technical_failures'}
    verify_important_clean_checkpoint(job)
    command = with_workers(task['command'], workers)
    code = run_command(job['home'], 'model_clean_technical_retry',
                       [*command, '--resume', '--retry-decisions', 'error',
                        '--retry-failure-kind', 'transport'])
    checked = important_clean_state(job, {'status': 'completed' if code == 0 else 'failed',
                                         'returncode': code})
    result = {'job_id': job['job_id'],
              'status': 'completed' if code == 0 and not checked['issues'] else 'failed',
              'remaining': checked['pending'], 'returncode': code,
              'issues': checked['issues'], 'finished_at': now()}
    save(Path(job['home']) / 'IMPORTANT_MODEL_CLEAN_RETRY.json', result)
    if result['status'] == 'completed':
        reconcile_important_clean_receipt(job, task, result)
    return result


def important_clean_transport_errors(job, result=None):
    """Return pending technical work; missing/incomplete outputs count as unfinished."""
    return important_clean_state(job, result)['pending']


def generate_boundaries(root, subjects, service, gateway, profile, recorder=None):
    """Validate/import the project release, without a boundary model request."""
    from project_taxonomy import binding,DEFAULT_LOCK
    pins=binding(subjects)  # Validate all required subjects before producing a partial run.
    bridge=ROOT/'adapters/mounting_bridge.py'
    results={}
    for subject,pin in pins['subjects'].items():
        home=root/'boundaries'/subject
        home.mkdir(parents=True,exist_ok=True)
        output=home/'assets'
        common=['--out',output,'--project-assets-lock',DEFAULT_LOCK]
        commands=[('boundary_prepare',[sys.executable,'-B',bridge,'prepare','--boundary-only',
                   '--require-boundaries','--subject-slug',subject,*common]),
                  ('boundary_activate',[sys.executable,'-B',bridge,'activate-project-assets',*common]),
                  ('boundary_verify',[sys.executable,'-B',bridge,'verify-boundaries',*common])]
        codes=[]
        for label,command in commands:
            code=run_command(home,label,command);codes.append(code)
            if code:break
        verified=read(output/'BOUNDARIES_VERIFIED.json') if codes==[0,0,0] else {}
        results[subject]={'subject':subject,'status':'completed' if codes==[0,0,0] else 'failed',
            'returncodes':codes,'taxonomy_sha256':pin['source_sha256'],
            'project_assets':pins,'model_calls':0,'transport_unresolved':0,
            'semantic_quality_degraded':bool(verified.get('fallback_nodes')),
            'verification':str(output/'BOUNDARIES_VERIFIED.json')}
    return results,DEFAULT_LOCK.parent


def prepare_mounts(root, track, jobs, service, gateway, profile, boundaries, boundary_cache):
    by_subject = defaultdict(list)
    for job in jobs:
        output = Path(job['home']) / 'run/04_cleaning/export/records.jsonl'
        if output.is_file() and output.stat().st_size:
            by_subject[job['subject']].append((job, output))
    mounts = []
    for subject, values in sorted(by_subject.items()):
        if boundaries.get(subject, {}).get('status') != 'completed':
            continue
        native_id = job_id(track, 'mount', subject)
        home = root / track / 'mounting' / subject
        home.mkdir(parents=True, exist_ok=True)
        config = bind_model_routes(read(values[0][0]['config']), gateway.job_url(native_id))
        config['paths']['run'] = str(home / 'run')
        config['mounting']['inputs'] = [str(output) for _, output in values]
        config['mounting']['workers'] = profile.job_workers('mounting')
        config['mounting']['boundaries']={'source':'project','enabled':True}
        from project_taxonomy import subject as project_subject
        config['paths']['taxonomy']=project_subject(subject,full=False)['taxonomy']
        save(home / 'pipeline.json', config)
        save(home / 'CLEAN_INPUTS.json', {'inputs': [{'path': str(output), 'sha256': sha(output)}
                                                    for _, output in values],
                                          'configuration_sha256': sha(home / 'pipeline.json'),
                                          'failed_books_are_not_inputs': True})
        mounts.append({'job_id': native_id, 'subject': subject, 'home': str(home),
                       'config': str(home / 'pipeline.json')})
    return mounts


def run_mount_round(job, task_name, number, workers):
    task = native_task(job, '05', task_name)
    code = run_command(job['home'], f'{task_name}_round_{number}',
                       [*with_workers(task['command'], workers), '--only-round', str(number)])
    kind = {'generate_mounting_boundaries': 'boundary',
            'route_mounting': 'route', 'review_mounting': 'review'}[task_name]
    status = Path(job['home']) / 'run/05_mounting' / kind / 'ORCHESTRATION_STATUS.json'
    return {**native_round_state(status, code), 'job_id': job['job_id']}


def finalize_mount_task(job, task_name):
    task = native_task(job, '05', task_name)
    code = run_command(job['home'], task_name + '_finalize',
                       [*task['command'], '--finalize-only'])
    if code:
        return {'job_id': job['job_id'], 'status': 'failed', 'returncode': code}
    result = selected_job(job, '05', task_name)
    return {'job_id': job['job_id'], 'status': result['status'], 'returncode': code}


def run_mount_transport(root, track, gateway, profile, recorder, mounts, task_name):
    by_id = {job['job_id']: job for job in mounts}
    rounds = cohort_transport_rounds(list(by_id),
        lambda key, number: run_mount_round(by_id[key], task_name, number,
                                            profile.job_workers('mounting', number)),
        lambda api, limit, phase: recorded_phase_transition(recorder, api, limit, phase),
        gateway.api_url, profile.round_limits_for('mounting'),
        tuple(profile.phase_workers('mounting', n) for n in (1, 2, 3)),
        track + '_' + task_name)
    save(root / track / (task_name.upper() + '_ROUNDS.json'), rounds)
    finalizable = [job for job in mounts if rounds[job['job_id']]['status'] == 'ready_to_finalize']
    results = run_simple_phase(finalizable,
                               lambda job: finalize_mount_task(job, task_name),
                               profile.mount_workers)
    return [job for job, result in zip(finalizable, results) if result['status'] == 'completed'], rounds


def run_track(root, track, candidates, service, gateway, profile, recorder, seed=20261012):
    front = prepare_front(root, service, gateway, profile, track, candidates)
    prep = phase_jobs(gateway.api_url, track + '_screening_cpu_prepare', profile.round_limits_for('front')[0],
                      front, lambda job: selected_job(job, '01',
                                                     'screening_1', 'screening_2'),
                      profile.cpu_workers, recorder)
    prepared = [job for job, result in zip(front, prep) if result['status'] == 'completed']
    metadata, metadata_rounds = screening_model_cohort(root, track, prepared, gateway,
        profile, recorder, 3)
    metadata_done = run_simple_phase(metadata,
        lambda job: selected_job(job, '01', 'screening_3'), profile.cpu_workers)
    metadata_ready = [job for job, result in zip(metadata, metadata_done)
                      if result['status'] == 'completed']
    stage4 = run_simple_phase(metadata_ready,
        lambda job: selected_job(job, '01', 'screening_4'), profile.cpu_workers)
    screening_ready = [job for job, result in zip(metadata_ready, stage4)
                       if result['status'] == 'completed']
    md5, md5_rounds = screening_model_cohort(root, track, screening_ready,
        gateway, profile, recorder, 5)
    md5_done = run_simple_phase(md5, lambda job: selected_job(job, '02', 'md_audit_5'),
                                profile.cpu_workers)
    md5_ready = [job for job, result in zip(md5, md5_done) if result['status'] == 'completed']
    md6_primary, md6_primary_rounds = screening_model_cohort(root, track, md5_ready,
        gateway, profile, recorder, 6, phase='primary')
    md6_challenge, md6_challenge_rounds = screening_model_cohort(root, track,
        md6_primary, gateway, profile, recorder, 6, phase='challenge')
    md6_done = run_simple_phase(md6_challenge,
        lambda job: selected_job(job, '02', 'md_audit_6'), profile.cpu_workers)
    md6_ready = [job for job, result in zip(md6_challenge, md6_done)
                 if result['status'] == 'completed']
    md7 = run_simple_phase(md6_ready, lambda job: selected_job(job, '02', 'md_audit_7'),
                           profile.cpu_workers)
    successful = [job for job, result in zip(md6_ready, md7)
                  if result['status'] == 'completed']
    name = track + '_structure_round_1'
    limit = profile.round_limits_for('front')[0]
    transition_phase(gateway.api_url, limit, name)
    recorder.update(phase=name, phase_limit=limit)
    structure = run_structure_jobs(successful, profile)
    save(root / 'PHASES' / (name + '.json'),
         {'phase': name, 'limit': limit, 'finished_at': now(), 'results': structure})
    for number in (2, 3):
        retry_jobs = [job for job in successful if retryable_classification_failures(job)]
        if not retry_jobs:
            break
        phase_jobs(gateway.api_url, track + f'_structure_round_{number}',
                   profile.round_limits_for('front')[number - 1], retry_jobs,
                   lambda job: retry_classification(job, track,
                                                    profile.job_workers('front', number)),
                   profile.phase_workers('front', number), recorder)
    admissions = []
    for job in successful:
        # A first-round streaming task may have exited with technical failures.
        # Reconcile its frozen outputs after native retries, without a new model call.
        if not stream_reconciliation_ready(job):
            continue
        if selected_job(job, '02', 'classify', retry_failed=True)['status'] != 'completed':
            continue
        if classification_failures(job):
            continue
        tasks = ('verify_classification', 'approve_dictionary_books') if track == 'dictionary' else ('verify_classification',)
        result = selected_job(job, '02', *tasks, retry_failed=True)
        if result['status'] == 'completed':
            admissions.extend(collect_approved(job, track))
    selected, cohort = select_admissions(admissions, profile.target_per_track, seed,
                                         maximum=profile.admission_limit)
    save(root / track / 'COHORT.json', {**cohort, 'selected': selected,
                                      'candidate_books': len(front), 'admitted': len(admissions)})
    fresh_source = verify_original_rows(candidates)
    if fresh_source != read(root / 'inputs' / (track + '_SOURCE_0611_VERIFIED.json')):
        raise ValueError('Original 0611 source changed between screening and production')
    save(root / track / 'SOURCE_RECHECK.json', fresh_source)
    production = prepare_production(root, service, gateway, profile, track, selected)
    subjects = [job['subject'] for job in production]
    if subjects:
        boundaries, boundary_cache = generate_boundaries(root, subjects, service, gateway, profile)
    else:
        boundaries, boundary_cache = {}, root / 'taxonomy_boundary_assets'
    save(root / track / 'BOUNDARY_RESULTS.json', boundaries)
    if track == 'dictionary':
        prep = run_simple_phase(production,
                                lambda job: selected_job(job, '03', 'prepare_fullbook'),
                                profile.production_workers)
        ready = [job for job, result in zip(production, prep) if result['status'] == 'completed']
        by_id = {job['job_id']: job for job in ready}
        rounds = dictionary_rounds(list(by_id),
                                   lambda key, number: run_dictionary_round(by_id[key], number),
                                   transition_phase, gateway.api_url, profile.round_limits_for('production'),
                                   workers=tuple(profile.phase_workers('production', n)
                                                 for n in (1, 2, 3)))
        finalize = [job for job in ready if rounds[job['job_id']]['status'] == 'ready_to_finalize']
        final = run_simple_phase(finalize, finalize_dictionary, profile.production_workers)
        prepared = [job for job, result in zip(finalize, final) if result['status'] == 'completed']
        reconciled = run_simple_phase(prepared,
            lambda job: selected_job(job, '03', 'extract_fullbook'), profile.production_workers)
        prepared = [job for job, result in zip(prepared, reconciled)
                    if result['status'] == 'completed']
        source_results = run_simple_phase(prepared,
            lambda job: selected_job(job, '03', 'verify_extraction_sources'),
            profile.production_workers)
        cleanable = [job for job, result in zip(prepared, source_results) if result['status'] == 'completed']
        partial_extraction_books, extraction_unresolved_items = extraction_handoff_totals(prepared)
        save(root / track / 'EXTRACTION_ROUNDS.json', rounds)
    else:
        rules = run_simple_phase(production,
                                 lambda job: selected_job(job, '03', 'extract_N1_N3'),
                                 profile.production_workers)
        cleanable = [job for job, result in zip(production, rules) if result['status'] == 'completed']
        partial_extraction_books = extraction_unresolved_items = 0
    if track == 'important':
        cpu = run_simple_phase(cleanable,
                               lambda job: selected_job(job, '04', 'normalize_fields', 'rules'),
                               profile.production_workers)
        cleanable = [job for job, result in zip(cleanable, cpu) if result['status'] == 'completed']
    if track == 'dictionary':
        by_id = {job['job_id']: job for job in cleanable}
        clean_rounds = cohort_transport_rounds(list(by_id),
            lambda key, number: run_dictionary_clean_round(by_id[key], number,
                profile.job_workers('cleaning', number)),
            transition_phase, gateway.api_url, profile.round_limits_for('cleaning'),
            tuple(profile.phase_workers('cleaning', n) for n in (1, 2, 3)),
            'dictionary_cleaning')
        save(root / track / 'CLEANING_ROUNDS.json', clean_rounds)
        finalize = [job for job in cleanable if clean_rounds[job['job_id']]['status'] == 'ready_to_finalize'
                    or (clean_rounds[job['job_id']]['status'] == 'technical_failure'
                        and 'native' in clean_rounds[job['job_id']])]
        final_results = run_simple_phase(finalize, finalize_dictionary_clean,
                                         profile.production_workers)
        cleaned = [job for job, result in zip(finalize, final_results)
                   if result['status'] == 'completed']
    else:
        for job in cleanable:
            freeze_important_clean_identity(job, native_task(job, '04', 'model_clean')['command'])
        first_clean = phase_jobs(gateway.api_url, track + '_cleaning_round_1',
                                 profile.round_limits_for('cleaning')[0], cleanable,
                                 lambda job: selected_job(job, '04', 'model_clean'),
                                 profile.production_workers, recorder)
        clean_attempts = {job['job_id']: result for job, result in zip(cleanable, first_clean)}
        for number in (2, 3):
            pending = [job for job in cleanable if important_clean_transport_errors(
                job, clean_attempts[job['job_id']])]
            if not pending:
                break
            retried = phase_jobs(gateway.api_url, track + f'_cleaning_round_{number}',
                       profile.round_limits_for('cleaning')[number - 1], pending,
                       lambda job: retry_important_clean(job,
                           profile.job_workers('cleaning', number), clean_attempts[job['job_id']]),
                       profile.phase_workers('cleaning', number), recorder)
            clean_attempts.update({job['job_id']: result for job, result in zip(pending, retried)})
        clean_results = run_simple_phase(cleanable,
                                         lambda job: selected_job(job, '04', 'restore_trace'),
                                         profile.production_workers)
        cleaned = [job for job, result in zip(cleanable, clean_results) if result['status'] == 'completed']
    mounts = prepare_mounts(root, track, cleaned, service, gateway, profile, boundaries, boundary_cache)
    mount_prep = run_simple_phase(mounts,
        lambda job: selected_job(job, '05', 'prepare_mounting'), profile.mount_workers)
    prepared_mounts = [job for job, result in zip(mounts, mount_prep)
                       if result['status'] == 'completed']
    with_boundaries, boundary_rounds = run_mount_transport(
        root, track, gateway, profile, recorder, prepared_mounts,
        'generate_mounting_boundaries')
    verified = run_simple_phase(with_boundaries,
        lambda job: selected_job(job, '05', 'verify_mounting_boundaries'),
        profile.mount_workers)
    mount_ready = [job for job, result in zip(with_boundaries, verified)
                   if result['status'] == 'completed']
    routed, route_rounds = run_mount_transport(root, track, gateway, profile, recorder,
                                                mount_ready, 'route_mounting')
    review_jobs, direct_jobs = [], []
    for job in routed:
        (review_jobs if mounting_review_enabled(read(job['config'])) else direct_jobs).append(job)
    reviewed, review_rounds = [], {}
    if review_jobs:
        reviewed, review_rounds = run_mount_transport(root, track, gateway, profile, recorder,
                                                       review_jobs, 'review_mounting')
    exportable = [*direct_jobs, *reviewed]
    exported = run_simple_phase(exportable,
        lambda job: selected_job(job, '05', 'export_mounting'), profile.mount_workers)
    ready_mounts = [job for job, result in zip(exportable, exported)
                    if result['status'] == 'completed']
    dedup = run_simple_phase(ready_mounts,
                             lambda job: selected_job(job, '06', 'dedup', 'verify_dedup'),
                             profile.mount_workers)
    final_outputs = final_output_totals(ready_mounts)
    cleaned_record_count = sum(count_jsonl(Path(job['home']) /
                              'run/04_cleaning/export/records.jsonl') for job in cleaned)
    (mount_transport_unresolved, mount_dispositions,
     review_format_semantic_unresolved) = mounting_dispositions(ready_mounts)
    remaining_cleaning_errors = 0
    if track == 'important':
        for job in cleanable:
            report = Path(job['home']) / 'run/04_cleaning/current/02_model_clean/model_clean_report.json'
            if report.is_file():
                remaining_cleaning_errors += read(report).get('counts', {}).get('error', 0)
    else:
        for job in cleaned:
            report = Path(job['home']) / 'run/04_cleaning/dictionary/TECHNICAL_FAILURES.jsonl'
            if report.is_file():
                remaining_cleaning_errors += sum(bool(line.strip()) for line in report.open(encoding='utf-8'))
    failed_boundaries = [subject for subject, result in boundaries.items()
                         if result['status'] != 'completed']
    degraded_boundaries = [subject for subject, result in boundaries.items()
                           if result.get('transport_unresolved') or result.get('semantic_quality_degraded')]
    unresolved_stages = []
    if cohort['shortfall']:
        unresolved_stages.append('cohort_shortfall')
    if len(successful) != len(front):
        unresolved_stages.append('screening')
    if any(classification_failures(job) for job in successful):
        unresolved_stages.append('classification')
    if partial_extraction_books or extraction_unresolved_items:
        unresolved_stages.append('extraction_partial')
    if failed_boundaries or degraded_boundaries:
        unresolved_stages.append('taxonomy_boundaries')
    if len(cleaned) != len(selected) or remaining_cleaning_errors:
        unresolved_stages.append('cleaning_or_prior_stage')
    if len(with_boundaries) != len(prepared_mounts) or any(
            item['status'] == 'technical_failure' for item in boundary_rounds.values()):
        unresolved_stages.append('mounting_boundaries')
    if (mount_transport_unresolved or review_format_semantic_unresolved or
            any(mount_dispositions.get(key, 0) for key in ('review', 'technical_failure',
                                                    'unconfigured', 'missing_summary')) or
            len(ready_mounts) != len(mounts) or
            any(item['status'] == 'technical_failure' for item in
                [*route_rounds.values(), *review_rounds.values()])):
        unresolved_stages.append('mounting_route_review')
    if selected and not mounts:
        unresolved_stages.append('no_mounting_input')
    if final_outputs['missing_exports'] or any(result['status'] != 'completed' for result in dedup):
        unresolved_stages.append('dedup_export')
    unresolved = bool(unresolved_stages)
    summary = {'track': track, 'status': 'finished_with_unresolved' if unresolved else 'completed_native_stages',
               'candidate_books': len(front), 'admitted_books': len(admissions),
               'selected_books': len(selected), 'shortfall': cohort['shortfall'],
               'partial_extraction_books': partial_extraction_books,
               'extraction_unresolved_items': extraction_unresolved_items,
               'completed_cleaning_books': len(cleaned),
               'cleaned_record_count': cleaned_record_count,
               'mounting_jobs': len(mounts),
               'completed_mounting_subjects': len(ready_mounts),
               'mounting_review_enabled': any(mounting_review_enabled(read(job['config'])) for job in mounts),
               'model_review_performed': bool(review_rounds),
               **final_outputs,
               'front_technical_failed_books': sum(bool(classification_failures(job)) for job in successful),
               'cleaning_technical_errors': remaining_cleaning_errors,
               'mounting_transport_unresolved': mount_transport_unresolved,
               'mounting_dispositions': dict(mount_dispositions),
               'mounting_format_or_semantic_unresolved': review_format_semantic_unresolved,
               'failed_boundary_subjects': failed_boundaries,
               'degraded_boundary_subjects': degraded_boundaries,
               'unresolved_stages': unresolved_stages,
               'dedup_completed': sum(result['status'] == 'completed' for result in dedup),
               'semantic_quality_approved': False, 'actual_gpu_cards_verified': False}
    save(root / track / 'SUMMARY.json', summary)
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "snapshot-code", "run"),
                        help="run explicitly starts a new gateway and native jobs")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--dictionary", type=Path)
    parser.add_argument("--important", type=Path)
    parser.add_argument("--gpu-cards", type=int, default=16)
    parser.add_argument("--requests-per-card", type=int, default=32)
    parser.add_argument("--limit", type=int)
    parser.add_argument('--front-limit', type=int, default=64)
    parser.add_argument('--extraction-limit', type=int, default=128)
    parser.add_argument('--model-limit', type=int, default=128)
    parser.add_argument('--calibration-report', type=Path)
    parser.add_argument('--tracks', nargs='+', choices=('dictionary', 'important'),
                        default=['dictionary', 'important'])
    parser.add_argument("--target-per-track", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261012)
    return parser


def plan_document(profile, tracks=('dictionary', 'important')):
    shared = ["screening_recall_metadata_cpu", "screening_metadata_round_1",
              "screening_metadata_round_2", "screening_metadata_round_3",
              "screening_metadata_finalize", "screening_track_split_cpu",
              "important_md_round_1", "important_md_round_2", "important_md_round_3",
              "important_md_finalize", "dictionary_md_primary_round_1",
              "dictionary_md_primary_round_2", "dictionary_md_primary_round_3",
              "dictionary_md_challenge_round_1", "dictionary_md_challenge_round_2",
              "dictionary_md_challenge_round_3", "dictionary_md_finalize",
              "screening_merge_cpu", "structure_round_1", "structure_round_2", "structure_round_3",
              "taxonomy_boundary_round_1", "taxonomy_boundary_round_2",
              "taxonomy_boundary_round_3", "taxonomy_boundary_finalize"]
    mounting = ["mounting_boundary_round_1", "mounting_boundary_round_2",
                "mounting_boundary_round_3", "mounting_boundary_finalize",
                "mounting_route_round_1", "mounting_route_round_2",
                "mounting_route_round_3", "mounting_route_finalize", "dedup"]
    document = {"resource_profile": profile.as_dict(),
            "tracks": [
                {"name": "dictionary", "phases": [*shared, "extraction_round_1",
                    "extraction_round_2", "extraction_round_3", "extraction_finalize",
                    "source_verify", "cleaning_round_1", "cleaning_round_2",
                    "cleaning_round_3", "cleaning_finalize", *mounting]},
                {"name": "important", "phases": [*shared, "N1_N3_rule_extraction",
                    "cleaning_round_1", "cleaning_round_2", "cleaning_round_3",
                    *mounting]}],
            "phase_order": "exclusive_global_gateway", "mode": "plan_only",
            "mounting_review_enabled": False,
            "model_calls": 0, "semantic_quality_approved": False}
    document['tracks'] = [item for item in document['tracks'] if item['name'] in tracks]
    return document


def main(argv=None):
    args = build_parser().parse_args(argv)
    profile = ResourceProfile(gpu_cards=args.gpu_cards,
                              requests_per_card=args.requests_per_card,
                              limit=args.limit,
                              front_limit=args.front_limit,
                              extraction_limit=args.extraction_limit,
                              model_limit=args.model_limit,
                              front_workers=max(64, args.front_limit),
                              target_per_track=args.target_per_track,
                              admission_limit=100 if args.tracks == ["important"] else 30)
    if args.calibration_report:
        profile = replace(profile, calibration_evidence=calibration_evidence(args.calibration_report, profile))
    if args.action == "plan":
        print(json.dumps(plan_document(profile, args.tracks), ensure_ascii=False, indent=2))
        return 0
    if args.action == 'snapshot-code':
        if args.root is None:
            raise ValueError('snapshot-code requires --root')
        require_executed_code_root(args.root)
        destination = args.root / 'CODE_SNAPSHOT.json'
        if destination.exists():
            raise FileExistsError(destination)
        save(destination, code_snapshot())
        return 0
    if len(set(args.tracks)) != len(args.tracks):
        raise ValueError('Duplicate tracks')
    paths = {'dictionary': args.dictionary, 'important': args.important}
    selected_tracks = [(track, paths[track]) for track in ('dictionary', 'important') if track in args.tracks]
    if args.root is None or any(path is None for _, path in selected_tracks):
        raise ValueError('run requires --root and inputs for each selected track')
    root = args.root.resolve(strict=True)
    require_executed_code_root(root)
    if (root / 'STATUS.json').exists() or (root / 'gateway').exists():
        raise FileExistsError('Use a new attempt directory; never overwrite an existing run')
    service = read(root / 'SERVICE_PREFLIGHT.json')
    if not isinstance(service.get('api_url'), str) or not service.get('model'):
        raise ValueError('SERVICE_PREFLIGHT.json requires api_url and model')
    if args.calibration_report:
        calibration_evidence(args.calibration_report, profile, service)
    if not (root / 'CODE_SNAPSHOT.json').is_file():
        raise FileNotFoundError('CODE_SNAPSHOT.json is required for frozen provenance')
    verify_code_snapshot(root / 'CODE_SNAPSHOT.json')
    recorder = Recorder(root, profile)
    save(root / 'RESOURCE_PROFILE.json', profile.as_dict())
    inputs = root / 'inputs'
    inputs.mkdir(exist_ok=True)
    try:
        for track, path in selected_tracks:
            proof = verify_original_rows(path)
            save(inputs / (track + '_SOURCE_0611_VERIFIED.json'), proof)
            save(inputs / (track + '_candidates.json'), read(path))
        with Gateway(root / 'gateway', service['api_url'], limit=profile.global_limit) as gateway:
            recorder.update(status='running', resource_profile=profile.as_dict(),
                            scheduling='stage_separated', gateway_hard_limit=profile.global_limit)
            for track, path in selected_tracks:
                summary = run_track(root, track, path, service, gateway, profile, recorder,
                                    seed=args.seed)
                recorder.state['tracks'][track] = summary
                recorder.update(last_completed_track=track)
        unresolved = any(item['status'] != 'completed_native_stages'
                         for item in recorder.state['tracks'].values())
        recorder.update(status='finished_with_unresolved' if unresolved else 'finished',
                        ended_at=now(), semantic_quality_approved=False,
                        actual_gpu_cards_verified=False)
    except BaseException as exc:
        recorder.update(status='failed', error=f'{type(exc).__name__}: {exc}', ended_at=now())
        raise
    return 0


def calibration_evidence(path, profile, service=None):
    from concurrency_calibration import choose_level
    report = read(path)
    if (report.get('schema') != 1 or report.get('status') != 'completed' or
            report.get('semantic_quality_approved') is not False):
        raise ValueError('Completed calibration report is required')
    for kind, applied in (('front', profile.front_limit), ('extraction', profile.extraction_limit)):
        result = report[kind]
        rows = result['levels']
        selected = choose_level(rows)
        if (selected is None or result['selected_limit'] != applied or
                selected['concurrency'] != applied or
                len({row['population_sha256'] for row in rows}) != 1):
            raise ValueError('Applied cap differs from healthy fixed-population calibration evidence')
    if report['model_limit'] != profile.model_limit or profile.model_limit != profile.extraction_limit:
        raise ValueError('Downstream conservative budget differs from calibration receipt')
    if profile.global_limit != max(profile.front_limit, profile.extraction_limit, profile.model_limit):
        raise ValueError('Effective global cap differs from measured stage limits')
    if service is not None:
        if any(report.get('service', {}).get(key) != service.get(key)
               for key in ('api_url', 'model', 'configured_context')):
            raise ValueError('Calibration service/model/context differs from current run preflight')
    return {'path': str(Path(path).resolve()), 'sha256': sha(Path(path)),
            'scope': report.get('measurement_scope', ['front', 'dictionary_extraction']),
            'semantic_quality_approved': False}


if __name__ == "__main__":
    raise SystemExit(main())
