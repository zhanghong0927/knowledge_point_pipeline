#!/usr/bin/env python3
"""One-shot background continuation: dictionary summary -> calibration -> important."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from batch_pipeline import Gateway, save, now
from dictionary_handoff import (boundary_summary, process_identity, validate_identity,
                                protected_hashes, retire_launch, HandoffRecoveryError)
from calibration_packets import (dictionary_front_packets, important_front_packets,
                                 dictionary_extraction_packets, context_checked_packets)
from concurrency_calibration import calibrate_stage

ROOT = Path(__file__).resolve().parent


def validate_roots(source, important, guardian=None):
    source, important = Path(source).resolve(), Path(important).resolve()
    roots = [source, important] + ([Path(guardian).resolve()] if guardian else [])
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise ValueError('Source, guardian and continuation roots must be independent')
    if important.exists():
        raise FileExistsError('Important continuation root already exists')


def registration_hashes(source):
    return {key: value for key, value in protected_hashes(source).items()
            if key != 'dictionary/SUMMARY.json'}


def inspect_boundary(config, *, verify_hashes=True):
    source = Path(config['source_root']).resolve(strict=True)
    validate_identity(process_identity(config['source_identity']['pid']), config['source_identity'])
    if verify_hashes and registration_hashes(source) != config['source_protected_hashes']:
        raise ValueError('Frozen source or input identity changed since handoff registration')
    validate_roots(source, config['important_root'], config.get('guardian_root'))
    if (source / 'important/SUMMARY.json').exists():
        raise ValueError('Old important track already finished; duplicate continuation refused')
    summary = boundary_summary(source)
    return {'state': 'dictionary_boundary_ready' if summary else 'waiting_dictionary_whole_track',
            'at': now(), 'dictionary': summary, 'calibration_requests': 0,
            'source_controller_identity_verified': True}


def important_command(root, report, *, gpu_cards, target_per_track, seed):
    root = Path(root)
    return [sys.executable, '-B', '-u', str(root / 'code/staged_benchmark.py'), 'run',
        '--root', str(root), '--tracks', 'important',
        '--important', str(root / 'inputs/important_candidates_verified.json'),
        '--gpu-cards', str(gpu_cards), '--target-per-track', str(target_per_track),
        '--seed', str(seed), '--front-limit', str(report['front']['selected_limit']),
        '--extraction-limit', str(report['extraction']['selected_limit']),
        '--model-limit', str(report['model_limit']),
        '--calibration-report', str(root / 'CALIBRATION_REPORT.json')]


def service_preflight(gateway, previous, out):
    with build_opener(ProxyHandler({})).open(gateway.api_url + '/v1/models', timeout=30) as response:
        body = response.read(1048577)
    if len(body) > 1048576:
        raise ValueError('Models response exceeds preflight bound')
    models = json.loads(body)
    model = next((row for row in models.get('data', []) if row.get('id') == previous['model']), None)
    if model is None:
        raise ValueError('Original API no longer advertises the expected model')
    context = model.get('max_model_len', model.get('context_length'))
    if type(context) is not int or context < previous['configured_context']:
        raise ValueError('Current advertised model context is absent or smaller than configured')
    service = dict(previous, advertised_context=context, observed_at=now(),
                   gpu_cards_source='user_declared_not_fleet_verified', observed_gpu_cards=None)
    save(out / 'MODELS.json', models)
    save(out / 'SERVICE_PREFLIGHT.json', service)
    return service


def status(home, state, **extra):
    value = {'state': state, 'at': now(), 'semantic_quality_approved': False, **extra}
    save(Path(home) / 'STATUS.json', value)
    print(json.dumps(value, ensure_ascii=False), flush=True)
    return value


def prepare_important_root(config, home, service, report):
    source, destination = Path(config['source_root']), Path(config['important_root'])
    validate_roots(source, destination, home)
    destination.mkdir(parents=True)
    shutil.copytree(ROOT, destination / 'code', ignore=shutil.ignore_patterns(
        '__pycache__', '.git', '.pytest_cache', 'validation_logs', '.superpowers'))
    (destination / 'inputs').mkdir()
    for path in (source / 'inputs').glob('important*'):
        if path.is_file():
            shutil.copy2(path, destination / 'inputs' / path.name)
    if not (destination / 'inputs/important_candidates_verified.json').is_file():
        raise ValueError('Missing verified important input')
    save(destination / 'SERVICE_PREFLIGHT.json', service)
    save(destination / 'CALIBRATION_REPORT.json', report)
    save(destination / 'CONTINUATION_SOURCE.json', {'source_root': str(source),
        'guardian_root': str(home), 'source_identity': config['source_identity'],
        'source_protected_hashes': config['source_protected_hashes'],
        'source_outputs_reused': False, 'semantic_quality_approved': False})
    subprocess.run([sys.executable, '-B', str(destination / 'code/staged_benchmark.py'),
                    'snapshot-code', '--root', str(destination)], check=True)
    return destination


def calibrate_and_continue(config, home, receipt):
    source, home = Path(config['source_root']), Path(home)
    calibration = home / 'calibration'
    calibration.mkdir()
    status(home, 'calibration_preparing', dictionary=receipt['dictionary'],
           source_retired=True, important_root=config['important_root'])
    previous = json.loads((source / 'SERVICE_PREFLIGHT.json').read_text())
    # CPU-only native packet capture does not modify source preparation or outputs.
    important = important_front_packets(source, calibration, previous)
    front = dictionary_front_packets(source, previous['model']) + important
    extraction = dictionary_extraction_packets(source, previous['model'])
    save(calibration / 'PACKETS_NATIVE.json', {'front': front, 'extraction': extraction})
    with Gateway(calibration / 'gateway', previous['api_url'], limit=128) as gateway:
        service = service_preflight(gateway, previous, calibration)
        front = context_checked_packets(gateway.api_url, front, service,
                                         calibration / 'FRONT_CONTEXT.json')
        extraction = context_checked_packets(gateway.api_url, extraction, service,
                                              calibration / 'EXTRACTION_CONTEXT.json')
        if (any(sum(packet.get('native_front_track') == track for packet in front) < 4
                for track in ('dictionary', 'important')) or len(extraction) < 4):
            raise ValueError('Insufficient actual native packets within model context')
        save(calibration / 'PACKETS.json', {'front': front, 'extraction': extraction})
        status(home, 'calibrating_front', test_levels=[32, 64], fallback_levels=[16, 8],
               fixed_population=64, source_retired=True)
        front_result = calibrate_stage(gateway, 'front', front, [32, 64], calibration,
                                       600, 64, fallback_levels=[16, 8])
        status(home, 'calibrating_dictionary_extraction', test_levels=[64, 96, 128],
               fallback_levels=[32, 16], fixed_population=128, source_retired=True)
        extraction_result = calibrate_stage(gateway, 'extraction', extraction, [64, 96, 128],
                                            calibration, 900, 128, fallback_levels=[32, 16])
        observed = gateway.stats()
        if observed['total']['active'] or observed['total']['queued']:
            raise RuntimeError('Calibration requests did not finish before important continuation')
        save(calibration / 'FINAL_OBSERVED_STATS.json', observed)
    report = {'schema': 1, 'status': 'completed', 'at': now(), 'service': service,
              'front': front_result, 'extraction': extraction_result,
              'model_limit': extraction_result['selected_limit'],
              'downstream_budget_scope': 'conservative extraction-derived cap; cleaning/mounting not calibrated',
              'dictionary_summary': receipt['dictionary'], 'calibration_root': str(calibration),
              'test_scope': 'fixed native request replays, transport and native JSON/evidence guards',
              'actual_gpu_cards_verified': False, 'semantic_quality_approved': False}
    save(home / 'CALIBRATION_REPORT.json', report)
    important_root = prepare_important_root(config, home, service, report)
    command = important_command(important_root, report, gpu_cards=config['gpu_cards'],
                                target_per_track=config['target_per_track'], seed=config['seed'])
    status(home, 'important_running', important_root=str(important_root),
           front_limit=report['front']['selected_limit'], model_limit=report['model_limit'])
    with (important_root / 'controller.log').open('w') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=important_root / 'code')
        save(important_root / 'LAUNCH.json', {'pid': child.pid, 'command': command,
            'guardian_root': str(home), 'at': now(), 'track_order': ['important'],
            'target_per_track': config['target_per_track'], 'background_mode': 'one_shot_successor_session',
            'calibration_report_sha256': hashlib.sha256((important_root / 'CALIBRATION_REPORT.json').read_bytes()).hexdigest()})
        code = child.wait()
    final = important_root / 'important/SUMMARY.json'
    if code or not final.is_file():
        raise RuntimeError(f'Important controller failed: exit={code}; summary_exists={final.is_file()}')
    summary = json.loads(final.read_text())
    status(home, 'finished_with_unresolved' if summary['status'] != 'completed_native_stages' else 'finished',
           dictionary=receipt['dictionary']['summary'], important=summary,
           important_root=str(important_root), calibration_report=str(home / 'CALIBRATION_REPORT.json'))


def register(source, home, important, pid, *, gpu_cards=16, target_per_track=30, seed=20261012):
    source, home = Path(source).resolve(strict=True), Path(home).resolve(strict=True)
    if ROOT.resolve() != home / 'code':
        raise ValueError('Guardian must execute from its new frozen HOME/code')
    validate_roots(source, important, home)
    from staged_benchmark import verify_code_snapshot
    verify_code_snapshot(home / 'CODE_SNAPSHOT.json')
    identity = process_identity(pid)
    validate_identity(identity, identity)
    launch = json.loads((source / 'LAUNCH.json').read_text())
    if launch['pid'] != pid or launch['command'] != identity['argv']:
        raise ValueError('Process does not match original launch receipt')
    config = {'schema': 1, 'source_root': str(source), 'guardian_root': str(home), 'source_identity': identity,
        'source_protected_hashes': registration_hashes(source),
        'important_root': str(Path(important).resolve()), 'gpu_cards': gpu_cards,
        'target_per_track': target_per_track, 'seed': seed,
        'order': ['dictionary_whole_track', 'calibration', 'important_whole_track'],
        'registered_at': now(), 'calibration_requests_at_registration': 0}
    inspect_boundary(config)
    if (home / 'REGISTRATION.json').exists():
        raise FileExistsError('Guardian is already registered')
    save(home / 'REGISTRATION.json', config)
    status(home, 'registered_waiting_dictionary', calibration_requests=0)
    return config


def run(home, poll_seconds=1):
    home = Path(home).resolve(strict=True)
    if ROOT.resolve() != home / 'code':
        raise ValueError('Guardian must execute from its frozen HOME/code')
    from staged_benchmark import verify_code_snapshot
    verify_code_snapshot(home / 'CODE_SNAPSHOT.json')
    # flock is released by the kernel after exit; concurrent guardians cannot act.
    import fcntl
    with (home / 'GUARDIAN.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((home / 'REGISTRATION.json').read_text())
        if (home / 'HANDOFF_RECEIPT.json').exists() or (home / 'calibration').exists():
            raise FileExistsError('This continuation already crossed its boundary')
        try:
            last_verify = 0
            while True:
                verify = time.monotonic() - last_verify >= 60
                value = inspect_boundary(config, verify_hashes=verify)
                if verify:
                    last_verify = time.monotonic()
                if value['state'] == 'dictionary_boundary_ready':
                    inspect_boundary(config)
                    break
                if not (home / 'STATUS.json').is_file() or verify:
                    save(home / 'STATUS.json', value)
                time.sleep(poll_seconds)
            status(home, 'dictionary_handoff', dictionary=value['dictionary'], calibration_requests=0)
            receipt = retire_launch(config['source_root'], config['source_identity'])
            save(home / 'HANDOFF_RECEIPT.json', receipt)
            # Allows timed-out upstream tails to settle; no claim of fleet-wide idle.
            time.sleep(30)
            calibrate_and_continue(config, home, receipt)
        except BaseException as exc:
            status(home, 'recovery_failed' if isinstance(exc, HandoffRecoveryError) else 'failed',
                   error=f'{type(exc).__name__}: {exc}',
                   handoff_recovery=getattr(exc, 'receipt', None),
                   important_root=config['important_root'])
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('register', 'run', 'inspect'))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--important-root', type=Path)
    parser.add_argument('--pid', type=int)
    args = parser.parse_args(argv)
    if args.action == 'register':
        if not args.source or not args.important_root or not args.pid:
            parser.error('register requires --source, --important-root and --pid')
        register(args.source, args.home, args.important_root, args.pid)
    elif args.action == 'inspect':
        print(json.dumps(inspect_boundary(json.loads((args.home / 'REGISTRATION.json').read_text()))))
    else:
        run(args.home)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
