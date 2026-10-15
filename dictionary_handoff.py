"""Retire only a verified launch after its complete dictionary-track summary.

The frozen source and dictionary artifacts are never changed. This module is
run from the independently tested successor checkout, not the running RUN/code.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import ipaddress
from urllib.parse import urlsplit

from batch_pipeline import now


def boundary_summary(root):
    path = Path(root) / 'dictionary/SUMMARY.json'
    if not path.exists():
        return None
    raw = path.read_bytes()
    value = json.loads(raw)
    required = ('selected_books', 'completed_cleaning_books', 'dedup_completed')
    if (not isinstance(value, dict) or value.get('track') != 'dictionary' or
            value.get('status') not in ('completed_native_stages', 'finished_with_unresolved') or
            any(type(value.get(key)) is not int or value[key] < 0 for key in required) or
            not isinstance(value.get('unresolved_stages'), list) or
            value.get('semantic_quality_approved') is not False):
        raise ValueError('Not a complete native dictionary-track summary')
    if value['selected_books'] > 30:
        raise ValueError('Invalid dictionary cohort size')
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'summary': value}


def process_identity(pid):
    directory = Path('/proc') / str(pid)
    try:
        stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'pgrp': int(stat[2]), 'session': int(stat[3]),
                'start_ticks': int(stat[19]), 'uid': directory.stat().st_uid,
                'state': stat[0], 'argv': [part.decode() for part in
                    (directory / 'cmdline').read_bytes().split(b'\0') if part]}
    except FileNotFoundError:
        return None


def validate_identity(observed, expected):
    keys = ('pid', 'pgrp', 'session', 'start_ticks', 'uid', 'argv')
    if (not isinstance(observed, dict) or not isinstance(expected, dict) or
            any(observed.get(key) != expected.get(key) for key in keys) or
            observed.get('state') == 'Z' or observed.get('pgrp') != observed.get('pid') or
            observed.get('session') != observed.get('pid') or
            observed.get('pgrp') == getattr(os, 'getpgrp', lambda: -1)()):
        raise ValueError('Launch identity changed or is not an isolated process session')


def group_members(pgrp):
    result = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            value = process_identity(int(directory.name))
            if value and value['pgrp'] == pgrp and value['state'] != 'Z':
                result.append(value)
        except (OSError, ValueError, IndexError):
            pass
    return result


def protected_hashes(root):
    root = Path(root)
    paths = list((root / 'code').rglob('*')) + list((root / 'inputs').glob('*'))
    paths += [root / 'LAUNCH.json', root / 'CODE_SNAPSHOT.json', root / 'SERVICE_PREFLIGHT.json',
              root / 'dictionary/SUMMARY.json']
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file() and '__pycache__' not in path.parts}


class HandoffRecoveryError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


def validate_members(members, expected):
    if any(value['session'] != expected['session'] or value['uid'] != expected['uid']
           or value['pgrp'] != expected['pgrp'] for value in members):
        raise ValueError('Unexpected process-group member')


def wait_stopped_group(expected, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        members = group_members(expected['pgrp'])
        validate_members(members, expected)
        if members and all(value['state'] in ('T', 't') for value in members):
            return members
        if time.monotonic() >= deadline:
            raise TimeoutError('Not every launch process entered stopped state')
        time.sleep(.01)


def loopback_origin(api_url):
    parts = urlsplit(api_url)
    host = ipaddress.ip_address(parts.hostname or '')
    if (parts.scheme != 'http' or not host.is_loopback or parts.username or parts.password
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError('Old gateway must be a literal loopback HTTP origin')
    return api_url.rstrip('/')


def drain_gateway(api_url, timeout=1800):
    from adapters.gateway_control import _request
    deadline = time.monotonic() + timeout
    while True:
        state = _request(loopback_origin(api_url), 'GET', '/_scheduler/stats',
                         timeout=max(.001, min(10, deadline - time.monotonic())))
        if state.get('paused') is not True:
            raise ValueError('Gateway admission resumed during handoff')
        if state['total']['active'] == 0:
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError('Old active requests did not drain; continuation is held')
        time.sleep(.02)


def wait_empty_group(expected, terminate_timeout=15, kill_timeout=10):
    deadline = time.monotonic() + terminate_timeout
    escalated = False
    while True:
        remaining = group_members(expected['pgrp'])
        validate_members(remaining, expected)
        if not remaining:
            return {'remaining_live_processes': 0, 'sigkill_required': escalated}
        if time.monotonic() >= deadline:
            if escalated:
                raise TimeoutError('Launch process group still exists after SIGKILL')
            os.killpg(expected['pgrp'], signal.SIGKILL)
            escalated = True
            deadline = time.monotonic() + kill_timeout
        time.sleep(.02)


def restore_gateway(api_url, prior_paused):
    from adapters.gateway_control import _request
    errors = []
    for _ in range(3):
        try:
            state = _request(api_url, 'POST', '/_scheduler/control', {'paused': prior_paused})
            if state.get('paused') is not prior_paused:
                raise RuntimeError('Gateway did not confirm restored admission state')
            return state
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    raise HandoffRecoveryError('Gateway recovery failed; controller remains stopped',
                               {'state': 'recovery_failed', 'api_url': api_url,
                                'required_paused_state': prior_paused, 'errors': errors})


def retire_launch(root, expected, require_gateway=True):
    root = Path(root).resolve(strict=True)
    boundary = boundary_summary(root)
    if boundary is None:
        raise ValueError('Dictionary track has not finished')
    if (root / 'important/SUMMARY.json').exists():
        raise ValueError('Important track already finished; refusing a duplicate successor')
    observed = process_identity(expected['pid'])
    validate_identity(observed, expected)
    if str(root / 'code/staged_benchmark.py') not in observed['argv'] or '--root' not in observed['argv']:
        raise ValueError('Launch does not execute the expected frozen controller')
    root_index = observed['argv'].index('--root') + 1
    if root_index >= len(observed['argv']) or Path(observed['argv'][root_index]).resolve() != root:
        raise ValueError('Launch root mismatch')
    before = protected_hashes(root)
    pgrp = expected['pgrp']
    members = group_members(pgrp)
    validate_members(members, expected)
    stopped = retired = False
    gateway = None
    prior_paused = None
    ready = None
    drained = {'total': {'active': 0, 'queued': 0}, 'scope': 'no_gateway_test_only'}
    pidfd = None
    try:
        if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
            pidfd = os.pidfd_open(expected['pid'])
            validate_identity(process_identity(expected['pid']), expected)
            signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        else:
            os.kill(expected['pid'], signal.SIGSTOP)
        stopped = True
        validate_identity(process_identity(expected['pid']), expected)
        os.killpg(pgrp, signal.SIGSTOP)
        wait_stopped_group(expected)
        if boundary_summary(root)['sha256'] != boundary['sha256']:
            raise ValueError('Dictionary summary changed at the boundary')
        if require_gateway:
            from adapters.gateway_control import _request
            ready = json.loads((root / 'gateway/READY.json').read_text())
            ready['api_url'] = loopback_origin(ready['api_url'])
            script = str(root / 'code/adapters/inference_gateway.py')
            relays = [value for value in group_members(pgrp) if script in value['argv']]
            if len(relays) != 1:
                raise ValueError('Could not identify the old launch gateway')
            gateway = relays[0]
            os.kill(gateway['pid'], signal.SIGCONT)
            prior_state = _request(ready['api_url'], 'GET', '/_scheduler/stats')
            prior_paused = prior_state['paused']
            if type(prior_paused) is not bool:
                raise ValueError('Invalid old gateway admission state')
            paused = _request(ready['api_url'], 'POST', '/_scheduler/control', {'paused': True})
            if paused.get('paused') is not True:
                raise ValueError('Old gateway did not confirm paused admission')
            if any(value['total']['active'] or value['total']['queued']
                   for key, value in paused['jobs'].items() if not key.startswith('important_')):
                raise ValueError('Dictionary or shared boundary requests are still active')
            # Resume request clients so their TCP response bodies can drain.
            # The controller stays stopped; paused admission holds all new calls.
            for member in group_members(pgrp):
                validate_members([member], expected)
                if member['pid'] != expected['pid']:
                    current = process_identity(member['pid'])
                    if current and current['start_ticks'] == member['start_ticks']:
                        os.kill(member['pid'], signal.SIGCONT)
            drained = drain_gateway(ready['api_url'])
        # Any old important preparation belongs to this retired attempt. Its
        # outputs stay in place, and the successor starts in a fresh directory.
        validate_identity(process_identity(expected['pid']), expected)
        os.killpg(pgrp, signal.SIGTERM)
        os.killpg(pgrp, signal.SIGCONT)
        retired = True
        termination = wait_empty_group(expected)
        after = protected_hashes(root)
        if before != after:
            raise ValueError('Protected frozen source or dictionary artifacts changed')
        return {'state': 'retired_after_dictionary', 'at': now(), 'source_root': str(root),
                'source_identity': expected, 'dictionary': boundary,
                'protected_hashes': after, 'protected_files_unchanged': True,
                'old_gateway_drain': drained, 'termination': termination,
                'drain_scope': 'this launch gateway clients; backend fleet idle is not asserted',
                'old_important_outputs_reused': False,
                'old_important_preparation_exists': (root / 'important').exists()}
    except BaseException as exc:
        if stopped and not retired:
            current = process_identity(expected['pid'])
            if current and current.get('start_ticks') == expected['start_ticks']:
                if gateway and prior_paused is not None:
                    try:
                        restore_gateway(ready['api_url'], prior_paused)
                    except HandoffRecoveryError as recovery:
                        recovery.receipt.update(source_identity=expected, original_error=str(exc))
                        raise recovery from exc
                os.killpg(pgrp, signal.SIGCONT)
        raise
    finally:
        if pidfd is not None:
            os.close(pidfd)
