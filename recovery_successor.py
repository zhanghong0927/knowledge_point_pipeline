#!/usr/bin/env python3
"""Read-only recovery boundary, then isolated calibration and important books."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

from batch_pipeline import Gateway, now, save
from dictionary_handoff import process_identity

ROOT = Path(__file__).resolve().parent
TERMINAL = {'finished', 'finished_with_unresolved', 'finished_with_failures'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def process_inventory():
    values = []
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                value = process_identity(int(path.name))
                if value and value.get('state') != 'Z':
                    values.append(value)
            except (OSError, ValueError, IndexError):
                pass
    return values


def dependency_evidence(config, *, process_reader=process_identity,
                        process_inventory=process_inventory):
    """No HTTP or control requests; only sealed files and process identities."""
    source = Path(config['dependency_root'])
    for name, checksum in config['dependency_hashes'].items():
        if not (source / name).is_file() or sha(source / name) != checksum:
            raise ValueError('Frozen dependency identity changed: ' + name)
    job = read(source / 'JOB.json')
    if job.get('api') != config['api_url'] or job.get('model') != config['model']:
        raise ValueError('Dependency API/model identity differs')
    state = read(source / 'STATUS.json')
    result = {'state': 'WAITING_DEPENDENCY', 'ready': False, 'at': now(),
              'dependency_root': str(source), 'dependency_state': state.get('state'),
              'reason': 'dependency_not_terminal', 'model_calls': 0,
              'semantic_quality_approved': False, 'actual_gpu_cards_verified': False}
    if state.get('state') == 'failed':
        return dict(result, state='HELD_DEPENDENCY_FAILED', reason=state.get('error', 'dependency_failed'))
    if state.get('state') not in TERMINAL:
        return result
    expected = config['dependency_identity']
    current = process_reader(expected['pid'])
    if current and current.get('start_ticks') != expected['start_ticks']:
        raise ValueError('Dependency supervisor identity changed or PID was reused')
    if current and current.get('state') != 'Z':
        return dict(result, reason='supervisor_still_alive', live_processes=[current])
    live = []
    for owned in state.get('owned_processes', []):
        current = process_reader(owned['pid'])
        if current and current.get('start_ticks') == owned['start_ticks'] and current.get('state') != 'Z':
            live.append(current)
    live.extend(row for row in process_inventory()
                if row.get('session') == expected['session'] and row.get('state') != 'Z')
    if live:
        return dict(result, reason='owned_processes_still_alive', live_processes=live)
    required = ['round_3/IDLE_CHECK.json', 'round_3/native/SUMMARY.json',
                'merged/RECONCILIATION.json', 'PRODUCTION_AFTER_RUN.json', 'gateway/STATS.json']
    required += [f'round_{number}/RESULT.json' for number in (1, 2, 3)]
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        return dict(result, reason='final_receipts_missing', missing=missing)
    if state.get('rounds_completed') != 3:
        raise ValueError('Dependency did not complete all three rounds')
    cohort = set(config['dependency_books'])
    if not cohort or len(cohort) != len(config['dependency_books']):
        raise ValueError('Invalid frozen dependency cohort')
    receipts = []
    for number, cap in enumerate(config['round_limits'], 1):
        receipt = read(source / f'round_{number}/RESULT.json')
        if (receipt.get('round') != number or receipt.get('global_limit') != cap or
                receipt.get('finished_books') != len(cohort) or receipt.get('exit_code') not in (0, 2)):
            raise ValueError('Invalid dependency round or cohort receipt')
        receipts.append(receipt)
    native = read(source / 'round_3/native/SUMMARY.json')
    native_books = native.get('books', [])
    if (native.get('finished_books') != len(cohort) or len(native_books) != len(cohort)
            or {row.get('identifier') for row in native_books} != cohort
            or any(row.get('status') not in ('completed', 'partial', 'technical_failure') for row in native_books)):
        raise ValueError('Final native dependency cohort is incomplete')
    reconciled = read(source / 'merged/RECONCILIATION.json')
    merged_books = reconciled.get('books', [])
    if (len(merged_books) != len(cohort) or {row.get('identifier') for row in merged_books} != cohort
            or reconciled.get('semantic_quality_approved') is not False):
        raise ValueError('Dependency final reconciliation cohort is incomplete')
    protected = read(source / 'PRODUCTION_AFTER_RUN.json')
    if (protected.get('production_modified') is not False or not protected.get('frozen_code')
            or not all(row.get('matches') is True for row in protected['frozen_code'])):
        raise ValueError('Dependency original-source preservation check failed')
    idle = read(source / 'round_3/IDLE_CHECK.json')
    stats = read(source / 'gateway/STATS.json')
    if stats.get('upstream') != config['api_url']:
        raise ValueError('Dependency gateway API identity changed')
    counters = [stats, idle.get('gateway', {}), *[row.get('gateway_snapshot', {}) for row in receipts]]
    if (idle.get('zero_samples', 0) < 3 or any(
            row.get('total', {}).get('active') != 0 or row.get('total', {}).get('queued') != 0
            for row in counters)):
        return dict(result, reason='owned_gateway_requests_not_drained')
    return dict(result, state='DEPENDENCY_READY', ready=True, reason='three_rounds_reconciled_and_drained',
                receipt_sha256={name: sha(source / name) for name in required},
                newly_accepted=reconciled.get('newly_accepted'),
                unresolved_items=state.get('unresolved_items'), retired_processes_verified=True,
                idle_scope='owned private recovery gateway and isolated process session')


def wait_for_dependency(home, config, poll_seconds=60, *,
                        inspect=dependency_evidence, sleeper=time.sleep):
    stable = 0
    last_receipts = None
    while True:
        result = inspect(config)
        if result['state'] == 'HELD_DEPENDENCY_FAILED':
            save(Path(home) / 'STATUS.json', result)
            raise RuntimeError('Dependency failed before a complete three-round boundary')
        if result['ready']:
            receipts = result['receipt_sha256']
            stable = stable + 1 if receipts == last_receipts else 1
            last_receipts = receipts
            if stable >= 2:
                save(Path(home) / 'DEPENDENCY_BOUNDARY.json', result)
                return result
            result = dict(result, state='WAITING_DEPENDENCY', ready=False,
                          reason='confirming_stable_drained_boundary', stable_observations=stable)
        else:
            stable, last_receipts = 0, None
        save(Path(home) / 'STATUS.json', result)
        sleeper(poll_seconds)


def status(home, state, **extra):
    value = {'state': state, 'at': now(), 'semantic_quality_approved': False,
             'actual_gpu_cards_verified': False, **extra}
    save(Path(home) / 'STATUS.json', value)
    print(json.dumps(value, ensure_ascii=False), flush=True)
    return value


def verify_registration(home, config):
    from staged_benchmark import verify_code_snapshot
    home = Path(home).resolve(strict=True)
    if ROOT != home / 'code':
        raise ValueError('Successor must execute from its own frozen HOME/code')
    source = Path(config['dependency_root']).resolve(strict=True)
    destination = Path(config['important_root']).resolve()
    if source == home or source.is_relative_to(home) or home.is_relative_to(source):
        raise ValueError('Continuation and dependency roots overlap')
    if destination != home / 'important_run':
        raise ValueError('Important output must be the independent HOME/important_run')
    verify_code_snapshot(home / 'CODE_SNAPSHOT.json')
    for name, checksum in config['frozen_home_files'].items():
        if sha(home / name) != checksum:
            raise ValueError('Continuation frozen input or dependency identity changed: ' + name)
    if config.get('target_books') != 30 or config.get('context') != 32768:
        raise ValueError('Continuation scope differs from authorized target/context')


def prepare_important_root(home, config, service, report):
    destination = Path(config['important_root'])
    if destination.exists():
        raise FileExistsError('Important run already exists; duplicate launch refused')
    destination.mkdir()
    shutil.copytree(ROOT, destination / 'code', ignore=shutil.ignore_patterns(
        '__pycache__', '.git', '.pytest_cache', 'validation_logs'))
    (destination / 'inputs').mkdir()
    shutil.copy2(home / 'inputs/important_candidates_verified.json',
                 destination / 'inputs/important_candidates_verified.json')
    save(destination / 'SERVICE_PREFLIGHT.json', service)
    save(destination / 'CALIBRATION_REPORT.json', report)
    save(destination / 'CONTINUATION_SOURCE.json', {
        'dependency_root': config['dependency_root'], 'home': str(home),
        'source_outputs_reused': False, 'cache_migration_required': False,
        'fresh_root': True, 'dependency_boundary': read(home / 'DEPENDENCY_BOUNDARY.json'),
        'source_code_snapshot_sha256': sha(home / 'CODE_SNAPSHOT.json'),
        'input_sha256': sha(home / 'inputs/important_candidates_verified.json'),
        'semantic_quality_approved': False})
    subprocess.run([sys.executable, '-B', str(destination / 'code/staged_benchmark.py'),
                    'snapshot-code', '--root', str(destination)], check=True)
    return destination


def calibrate_and_continue(home, config, boundary):
    from adapters.gateway_control import transition_phase
    from calibration_packets import context_checked_packets
    from concurrency_calibration import calibrate_stage
    from important_calibration import (important_front_workload, important_cleaning_workload,
                                       validate_workload_coverage)
    from post_dictionary_runner import service_preflight, important_command
    calibration = home / 'calibration'
    calibration.mkdir()
    previous = {'api_url': config['api_url'], 'model': config['model'],
                'configured_context': config['context'], 'declared_gpu_cards': 16,
                'gpu_cards_source': 'user_declared_two_8card_replicas_DP4_TP2',
                'actual_gpu_cards_verified': False}
    status(home, 'CALIBRATION_PREPARING', model_calls=0,
           dependency_state=boundary['dependency_state'], dependency_boundary_verified=True)
    rows, front = important_front_workload(home, calibration, previous)
    save(calibration / 'FRONT_PACKETS_NATIVE.json', front)
    with Gateway(calibration / 'gateway', config['api_url'], limit=128) as gateway:
        service = service_preflight(gateway, previous, calibration)
        front = context_checked_packets(gateway.api_url, front, service, calibration / 'FRONT_CONTEXT.json')
        validate_workload_coverage(front, rows)
        status(home, 'CALIBRATING_IMPORTANT_FRONT', model_calls=None,
               test_levels=[32, 64], fallback_levels=[16, 8], fixed_population=64)
        front_result = calibrate_stage(gateway, 'front', front, [32, 64], calibration,
                                       600, 64, fallback_levels=[16, 8])
        transition_phase(gateway.api_url, front_result['selected_limit'], 'important_calibration_pilot_classification')
        usable = {packet['source_identifier'] for packet in front}
        cleaning = important_cleaning_workload(home, calibration, service, gateway,
            front_result['selected_limit'], [row for row in rows if row['identifier'] in usable])
        save(calibration / 'CLEANING_PACKETS_NATIVE.json', cleaning)
        cleaning = context_checked_packets(gateway.api_url, cleaning, service, calibration / 'CLEANING_CONTEXT.json')
        validate_workload_coverage(cleaning, rows)
        status(home, 'CALIBRATING_IMPORTANT_CLEANING', model_calls=None,
               test_levels=[64, 96, 128], fallback_levels=[32, 16], fixed_population=128)
        cleaning_result = calibrate_stage(gateway, 'important_cleaning', cleaning, [64, 96, 128],
                                          calibration, 900, 128, fallback_levels=[32, 16])
        observed = gateway.stats()
        if observed['total']['active'] or observed['total']['queued']:
            raise RuntimeError('Calibration gateway has requests in flight before important continuation')
        save(calibration / 'FINAL_OBSERVED_STATS.json', observed)
    report = {'schema': 1, 'status': 'completed', 'at': now(), 'service': service,
              'front': front_result, 'extraction': cleaning_result, 'cleaning': cleaning_result,
              'model_limit': cleaning_result['selected_limit'],
              'measurement_scope': ['important_front', 'important_cleaning'],
              'extraction_field_compatibility': 'important N1/N3 extraction is CPU rule routing; this cap is measured cleaning',
              'front_scope': 'native layout classification replays; metadata and MD-screening stages share this conservative cap',
              'downstream_budget_scope': 'native important cleaning calibrated; taxonomy boundary and mounting share conservative cap',
              'mounting_budget_calibrated': False,
              'dependency_state': boundary['dependency_state'], 'calibration_root': str(calibration),
              'test_scope': 'fixed native request replays, native JSON/schema and local source evidence guards',
              'actual_gpu_cards_verified': False, 'semantic_quality_approved': False}
    save(home / 'CALIBRATION_REPORT.json', report)
    verify_registration(home, config)
    destination = prepare_important_root(home, config, service, report)
    command = important_command(destination, report, gpu_cards=16, target_per_track=30, seed=config['seed'])
    status(home, 'IMPORTANT_RUNNING', important_root=str(destination),
           front_limit=front_result['selected_limit'], model_limit=report['model_limit'],
           model_calls=observed['routes'].get('v1/chat/completions', {}).get('received', 0))
    with (destination / 'controller.log').open('w') as log:
        child = subprocess.Popen(command, cwd=destination / 'code', stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT)
        save(destination / 'LAUNCH.json', {'at': now(), 'pid': child.pid,
             'identity': process_identity(child.pid), 'command': command,
             'continuation_root': str(home), 'calibration_report_sha256': sha(home / 'CALIBRATION_REPORT.json')})
        exit_code = child.wait()
    final = destination / 'important/SUMMARY.json'
    if exit_code or not final.is_file():
        raise RuntimeError(f'Important run failed: exit={exit_code}; summary_exists={final.is_file()}')
    summary = read(final)
    status(home, 'FINISHED_NATIVE_STAGES' if summary['status'] == 'completed_native_stages'
           else 'FINISHED_WITH_UNRESOLVED', dependency=boundary, important=summary,
           important_root=str(destination), calibration_report=str(home / 'CALIBRATION_REPORT.json'))


def run(home):
    import fcntl
    home = Path(home).resolve(strict=True)
    with (home / 'CONTINUATION.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = read(home / 'CONFIG.json')
        verify_registration(home, config)
        if (home / 'calibration').exists() or Path(config['important_root']).exists():
            raise FileExistsError('This continuation has already started calibration or important books')
        boundary = wait_for_dependency(home, config)
        verify_registration(home, config)
        save(home / 'MODEL_ACCESS_GRANTED.json', {'at': now(), 'dependency_boundary_sha256': sha(home / 'DEPENDENCY_BOUNDARY.json'),
             'api_url': config['api_url'], 'model': config['model'], 'model_calls_before_boundary': 0,
             'retired_source_modified': False})
        calibrate_and_continue(home, config, boundary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'run'))
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    home = args.home.resolve(strict=True)
    if args.action == 'inspect':
        config = read(home / 'CONFIG.json')
        verify_registration(home, config)
        print(json.dumps(dependency_evidence(config), ensure_ascii=False))
        return 0
    try:
        run(home)
    except Exception as exc:
        save(home / 'FAILURE.json', {'at': now(), 'error': str(exc), 'traceback': traceback.format_exc()})
        status(home, 'HELD_FAILURE', error=f'{type(exc).__name__}: {exc}',
               model_access_granted=(home / 'MODEL_ACCESS_GRANTED.json').exists())
        raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
