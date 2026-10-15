"""Run v5 in three serial, metrics-gated transport rounds (default 1024/256/64).

Exit codes: 0 completed; 2 normal partial; 3 blocked metrics/lock; 1 aborted.
The runner owns all cache selection: this driver never deletes or edits caches.
SUMMARY.json must be freshly written after every subprocess, contain complete
expected_books/finished_books/books, and provide both per-book and aggregate
http504_pending_chunks/http504_unresolved_chunks. New runners also provide
transport_pending_chunks/transport_unresolved_chunks: the legacy pending field
aliases all transport failures, while the legacy unresolved field counts 504 only.
Round 3 has no pending retries.
INPUT identity must exclude transport-round and worker counts. Run this script
under nohup (or another supervisor) for unattended, independent continuation.
"""
import argparse
import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from round_integrity import digest_file


WORKERS = (1024, 256, 64)
COUNTERS = ('http504_pending_chunks', 'http504_unresolved_chunks')
TRANSPORT_COUNTERS = ('transport_pending_chunks', 'transport_unresolved_chunks')


def unresolved_transport(snapshot):
    return snapshot.get(TRANSPORT_COUNTERS[1], snapshot.get(COUNTERS[1], 0))


def transport_label(snapshot):
    return 'transport' if TRANSPORT_COUNTERS[1] in snapshot else 'http504'


class Blocked(RuntimeError):
    pass


class RunnerFailure(RuntimeError):
    pass


class Refused(RunnerFailure):
    """Reject an unsafe staged invocation without changing a valid prior state."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, value):
    path = Path(path)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def fingerprint(path):
    try:
        data = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, len(data), hashlib.sha256(data).hexdigest())


def file_sha256(path, *, force_full=False):
    return digest_file(path, force_full=force_full)


def input_identity(args, manifest_bytes, specs, *, force_full=False):
    """Freeze the native payload and source inputs across separate processes."""
    sources = {}
    for spec in specs:
        for key in ('md_path', 'pdf_evidence_path'):
            if spec.get(key):
                path = Path(spec[key]).resolve()
                sources[f'{spec["identifier"]}:{key}'] = [str(path), file_sha256(path,force_full=force_full)]
    code_dir = Path(__file__).resolve().parent
    code = {path.name: file_sha256(path,force_full=force_full) for path in sorted(code_dir.glob('*.py'))}
    code['round_integrity.py'] = file_sha256(code_dir.parents[2]/'round_integrity.py',force_full=force_full)
    return {'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'manifest': str(args.manifest), 'out': str(args.out),
            'runner': str(args.runner), 'runner_sha256': file_sha256(args.runner,force_full=force_full),
            'api_url': args.api_url, 'model': args.model,
            'context': args.context, 'output_tokens': args.output_tokens,
            'timeout': args.timeout, 'overlap': args.overlap,
            'server_context': args.server_context, 'tokenizer': args.tokenizer,
            'round_workers': list(args.round_workers), 'book_workers': args.book_workers,
            'repair_policy':{name:getattr(args,name,default) for name,default in (
                ('repair_batch_size',8),('repair_attempts',2),('repair_max_requests',128),('tokenize_attempts',3))},
            'sources': sources, 'code_sha256': code}


def checkpoint_hashes(out, identifiers, *, force_full=False):
    """Capture only native checkpoint/output files, including cached requests."""
    files = [out / 'SUMMARY.json']
    for identifier in identifiers:
        folder = out / hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:16]
        files.extend(folder / name for name in (
            'INPUT.json', 'SUMMARY.json', 'units.json', 'entries.json',
            'accepted_entries.json', 'quarantined_entries.json'))
        for directory in ('chunks', 'responses', 'errors'):
            files.extend(sorted((folder / directory).glob('*.json')))
    if any(not path.is_file() for path in files):
        raise RunnerFailure('Native checkpoint/output artifact is missing')
    return {str(path.relative_to(out)): file_sha256(path,force_full=force_full) for path in files}


def verify_checkpoint(out, recorded, identifiers, *, force_full=False):
    if not isinstance(recorded, dict) or not recorded:
        raise RunnerFailure('No verified native checkpoint for completed round')
    current = checkpoint_hashes(out, identifiers,force_full=force_full)
    if current != recorded:
        raise RunnerFailure('Native checkpoint/output artifact changed since completed round')


@contextlib.contextmanager
def output_lock(out):
    # Keep the lock inode: deleting it can let concurrent drivers lock two files.
    with (out / '.ORCHESTRATION.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Blocked('Another driver owns this output directory') from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Blocked('Another driver owns this output directory') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def parse_metrics(text):
    names = {'vllm:num_requests_running': 'running',
             'vllm:num_requests_waiting': 'waiting'}
    totals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name = re.split(r'[\s{]', line, maxsplit=1)[0]
        if name not in names:
            continue
        match = re.fullmatch(r'[^\s{]+(?:\{.*\})?\s+(\S+)(?:\s+\d+)?', line)
        try:
            value = float(match.group(1)) if match else float('nan')
        except ValueError:
            value = float('nan')
        if not math.isfinite(value) or value < 0:
            raise Blocked(f'Invalid metrics sample: {line}')
        key = names[name]
        totals[key] = totals.get(key, 0.) + value
        if not math.isfinite(totals[key]):
            raise Blocked(f'Non-finite metrics total: {key}')
    if set(totals) != {'running', 'waiting'}:
        raise Blocked('Metrics unavailable: both vllm running/waiting gauges are required')
    return totals


def parse_scheduler_job(text, job_id):
    try:
        snapshot = json.loads(text)
        if type(snapshot['limit']) is not int or snapshot['limit'] <= 0 or not isinstance(snapshot['jobs'], dict):
            raise ValueError('Invalid scheduler identity')
        entry = snapshot['jobs'].get(job_id)
        counts = entry['total'] if entry is not None else {'active': 0, 'queued': 0}
        if any(type(counts[key]) is not int or counts[key] < 0 for key in ('active', 'queued')):
            raise ValueError('Invalid job request counters')
        return {'running': counts['active'], 'waiting': counts['queued']}
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked('Shared scheduler job counters unavailable') from exc


def validate_summary(snapshot, identifiers, exit_code, round_number):
    if not isinstance(snapshot, dict):
        raise RunnerFailure('Global SUMMARY must be a JSON object')

    def count(obj, key):
        value = obj.get(key)
        if type(value) is not int or value < 0:
            raise RunnerFailure(f'SUMMARY requires nonnegative integer {key}')
        return value

    expected = count(snapshot, 'expected_books')
    finished = count(snapshot, 'finished_books')
    if expected != len(identifiers) or finished != expected:
        raise RunnerFailure(f'Incomplete global SUMMARY: expected={expected}, '
                            f'finished={finished}, manifest={len(identifiers)}')
    books = snapshot.get('books')
    if not isinstance(books, list) or len(books) != expected:
        raise RunnerFailure('Global SUMMARY books coverage is incomplete')
    if any(not isinstance(book, dict) or not isinstance(book.get('identifier'), str)
           for book in books):
        raise RunnerFailure('Global SUMMARY has invalid book records')
    if {book['identifier'] for book in books} != set(identifiers):
        raise RunnerFailure('SUMMARY book identifier set differs from manifest')
    transport_format = any(key in snapshot for key in TRANSPORT_COUNTERS)
    keys = COUNTERS + TRANSPORT_COUNTERS if transport_format else COUNTERS
    totals = {key: 0 for key in keys}
    for book in books:
        status = book.get('status')
        if status not in ('completed', 'partial'):
            raise RunnerFailure(f'Book {book["identifier"]} failed: {status}')
        pending, unresolved = [count(book, key) for key in COUNTERS]
        if transport_format:
            transport_pending, transport_unresolved = [count(book, key) for key in TRANSPORT_COUNTERS]
            if transport_pending != pending or unresolved > transport_unresolved:
                raise RunnerFailure('Inconsistent per-book transport compatibility counters')
            unresolved = transport_unresolved
        elif any(key in book for key in TRANSPORT_COUNTERS):
            raise RunnerFailure('Global SUMMARY omits per-book transport counters')
        if pending > unresolved or (status == 'completed' and unresolved):
            raise RunnerFailure('Inconsistent per-book transport counters/status')
        for key in keys:
            totals[key] += book[key]
    for key in keys:
        if count(snapshot, key) != totals[key]:
            raise RunnerFailure(f'Global SUMMARY {key} differs from per-book total')
    if round_number == 3 and totals[COUNTERS[0]]:
        raise RunnerFailure('SUMMARY claims retries after final transport round 3')
    partial = any(book['status'] == 'partial' for book in books)
    if exit_code not in (0, 2):
        raise RunnerFailure(f'Runner failed with exit code {exit_code}')
    if (exit_code == 2) != partial:
        raise RunnerFailure('Runner exit code and SUMMARY completion status disagree')
    return 'partial' if partial else 'completed'


class Orchestrator:
    def __init__(self, args):
        self.args = args
        self.history = {'run_id': uuid.uuid4().hex, 'rounds': [], 'gates': []}
        self.status = {'run_id': self.history['run_id'], 'pid': os.getpid(),
                       'started_at': utc_now(), 'ended_at': None,
                       'manifest': str(args.manifest), 'out': str(args.out),
                       'round_workers': list(args.round_workers), 'book_workers': args.book_workers,
                       'metrics_urls': args.metrics_url}
        self.status['idle_scope'] = getattr(args, 'idle_scope', 'service')
        if self.status['idle_scope'] == 'scheduler-job':
            self.status.update(scheduler_job_id=args.scheduler_job_id,
                               scheduler_stats_url=args.scheduler_stats_url,
                               idle_evidence='forwarded client requests; not whole-service GPU idle')
        self.started = time.monotonic()

    def persist(self, state=None, **fields):
        if state:
            self.status['state'] = state
        self.status.update(fields)
        self.status['updated_at'] = utc_now()
        atomic_write(self.args.out / 'ROUNDS.json', self.history)
        atomic_write(self.args.out / 'ORCHESTRATION_STATUS.json', self.status)

    def finish(self, state, exit_code, **fields):
        self.persist(state, exit_code=exit_code, ended_at=utc_now(),
                     wall_seconds=round(time.monotonic() - self.started, 6), **fields)
        return exit_code

    def stage_start(self, kind, phase, round_number, **fields):
        record = {'phase': phase, 'round': round_number, 'started_at': utc_now(),
                  'ended_at': None, 'wall_seconds': None, 'outcome': 'running',
                  'exit_code': None, **fields}
        self.history[kind].append(record)
        self.persist('running', phase=phase, round=round_number)
        return record, time.monotonic()

    def stage_end(self, record, start, outcome, **fields):
        record.update(ended_at=utc_now(), wall_seconds=round(time.monotonic() - start, 6),
                      outcome=outcome, **fields)
        self.persist()

    def wait_idle(self, phase, round_number):
        shared = getattr(self.args, 'idle_scope', 'service') == 'scheduler-job'
        record, start = self.stage_start('gates', phase, round_number, samples=[],
                                        idle_scope='scheduler-job' if shared else 'service')
        deadline = start + self.args.metrics_max_wait
        consecutive = 0
        try:
            while True:
                readings = []
                for url in ([self.args.scheduler_stats_url] if shared else self.args.metrics_url):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Blocked('Metrics drain timed out before two consecutive idle samples')
                    request = urllib.request.Request(url, headers={'Accept': 'text/plain'})
                    try:
                        with urllib.request.urlopen(request, timeout=min(
                                self.args.metrics_timeout, remaining)) as response:
                            text = response.read().decode('utf-8')
                            gauges = (parse_scheduler_job(text, self.args.scheduler_job_id)
                                      if shared else parse_metrics(text))
                    except Blocked:
                        raise
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        raise Blocked(f'Metrics unavailable at {url}: {exc}') from exc
                    readings.append({'url': url, **gauges})
                if time.monotonic() >= deadline:
                    raise Blocked('Metrics drain timed out before two consecutive idle samples')
                idle = all(item['running'] == 0 and item['waiting'] == 0 for item in readings)
                consecutive = consecutive + 1 if idle else 0
                record['samples'].append({'utc': utc_now(), 'readings': readings,
                                          'consecutive_idle': consecutive})
                self.persist()
                if consecutive == 2:
                    self.stage_end(record, start, 'idle', exit_code=0)
                    return
                time.sleep(min(self.args.metrics_poll_interval, deadline - time.monotonic()))
        except Blocked as exc:
            self.stage_end(record, start, 'blocked', exit_code=3, error=str(exc))
            raise
        except KeyboardInterrupt:
            self.stage_end(record, start, 'aborted', exit_code=1, error='Metrics gate interrupted')
            raise

    def command(self, round_number, workers):
        args = self.args
        command = [args.python, '-u', str(args.runner), '--manifest', str(args.manifest),
                   '--out', str(args.out), '--api-url', args.api_url, '--model', args.model,
                   '--workers', str(workers), '--book-workers', str(args.book_workers),
                   '--transport-round', str(round_number), '--context', str(args.context),
                   '--output-tokens', str(args.output_tokens), '--timeout', str(args.timeout),
                   '--overlap', str(args.overlap)]
        for name,default in (('repair_batch_size',8),('repair_attempts',2),
                             ('repair_max_requests',128),('tokenize_attempts',3)):
            command.extend(['--'+name.replace('_','-'),str(getattr(args,name,default))])
        for flag, value in [('--server-context', args.server_context), ('--tokenizer', args.tokenizer)]:
            if value is not None:
                command.extend([flag, str(value)])
        return command

    def run_round(self, round_number, workers, identifiers):
        path = self.args.out / 'SUMMARY.json'
        previous = fingerprint(path)
        command = self.command(round_number, workers)
        log = self.args.out / f'ROUND_{round_number}_{self.history["run_id"]}.log'
        record, start = self.stage_start('rounds', 'runner', round_number,
                                         workers=workers, command=command, log=str(log), summary=None)
        try:
            with log.open('w', encoding='utf-8') as stream:
                process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
            record['exit_code'] = process.returncode
            current = fingerprint(path)
            if current is not None:
                raw = path.read_text(encoding='utf-8-sig')
                try:
                    snapshot = json.loads(raw)
                    json.dumps(snapshot, allow_nan=False)
                except ValueError:
                    record['summary_raw'] = raw
                    raise
                record['summary'] = snapshot
            if current is None or current == previous:
                raise RunnerFailure('Runner did not write a fresh global SUMMARY')
            outcome = validate_summary(record['summary'], identifiers, process.returncode, round_number)
            self.stage_end(record, start, outcome)
            self.persist(summary=record['summary'], runner_exit_code=process.returncode)
            return record['summary'], outcome
        except (OSError, ValueError, RunnerFailure) as exc:
            self.stage_end(record, start, 'aborted', error=str(exc))
            raise RunnerFailure(str(exc)) from exc
        except KeyboardInterrupt:
            self.stage_end(record, start, 'aborted', error='Runner phase interrupted')
            raise

    def run_locked(self):
        if self.args.only_round is not None or self.args.finalize_only:
            return self.run_staged_locked()
        try:
            manifest_bytes = self.args.manifest.read_bytes()
            specs = json.loads(manifest_bytes.decode('utf-8-sig'))
            if not isinstance(specs, list) or not specs or any(
                    not isinstance(spec, dict) or not isinstance(spec.get('identifier'), str)
                    or not spec['identifier'] for spec in specs):
                raise RunnerFailure('Manifest must contain a nonempty list of book identifiers')
            identifiers = [spec['identifier'] for spec in specs]
            if len(set(identifiers)) != len(identifiers):
                raise RunnerFailure('Manifest contains duplicate book identifiers')
            self.persist('running', manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())
            self.wait_idle('before_round_1', 1)
            for round_number, workers in enumerate(self.args.round_workers, 1):
                if self.args.manifest.read_bytes() != manifest_bytes:
                    raise RunnerFailure('The manifest changed between transport rounds')
                snapshot, outcome = self.run_round(round_number, workers, identifiers)
                self.wait_idle(f'after_round_{round_number}', round_number)
                if snapshot[COUNTERS[0]] == 0:
                    label = transport_label(snapshot)
                    reason = (f'{label}_exhausted' if round_number == 3 and unresolved_transport(snapshot)
                              else f'no_{label}_pending')
                    return self.finish(outcome, 2 if outcome == 'partial' else 0, stop_reason=reason)
            raise RunnerFailure('Final round unexpectedly left pending retries')
        except Blocked as exc:
            return self.finish('blocked', 3, error=str(exc))
        except (OSError, ValueError, RunnerFailure) as exc:
            return self.finish('aborted', 1, error=str(exc))
        except KeyboardInterrupt:
            return self.finish('aborted', 1, error='Driver interrupted; no further rounds launched')

    def run_staged_locked(self):
        try:
            try:
                manifest_bytes = self.args.manifest.read_bytes()
                specs = json.loads(manifest_bytes.decode('utf-8-sig'))
            except (OSError, UnicodeError, ValueError) as exc:
                raise Refused(f'Cannot verify staged manifest: {exc}') from exc
            if not isinstance(specs, list) or not specs or any(
                    not isinstance(spec, dict) or not isinstance(spec.get('identifier'), str)
                    or not spec['identifier'] for spec in specs):
                raise Refused('Manifest must contain a nonempty list of book identifiers')
            identifiers = [spec['identifier'] for spec in specs]
            if len(set(identifiers)) != len(identifiers):
                raise Refused('Manifest contains duplicate book identifiers')
            try:
                identity = input_identity(self.args, manifest_bytes, specs,force_full=self.args.finalize_only)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise Refused(f'Cannot verify staged input identity: {exc}') from exc
            status_path = self.args.out / 'ORCHESTRATION_STATUS.json'
            rounds_path = self.args.out / 'ROUNDS.json'
            if status_path.exists() != rounds_path.exists():
                raise Refused('Incomplete persisted orchestration state')
            if status_path.exists():
                previous_status = json.loads(status_path.read_text(encoding='utf-8-sig'))
                previous_history = json.loads(rounds_path.read_text(encoding='utf-8-sig'))
                if previous_status.get('mode') != 'staged' or previous_status.get('input_identity') != identity:
                    raise Refused('Staged input identity changed or output belongs to another run')
                if previous_status.get('run_id') != previous_history.get('run_id'):
                    raise Refused('Persisted run identity is inconsistent')
                if not isinstance(previous_history.get('rounds'), list) or not isinstance(previous_history.get('gates'), list):
                    raise Refused('Persisted round history is invalid')
                self.status = previous_status
                self.history = previous_history
                self.status.update(pid=os.getpid(), updated_at=utc_now())
            else:
                if self.args.finalize_only:
                    raise Refused('No completed staged round to finalize')
                if any((self.args.out / name).exists() for name in
                       ('SUMMARY.json', 'HANDOFF.json', 'UNRESOLVED.jsonl')) or any(
                        path.is_dir() for path in self.args.out.iterdir()) or any(
                        (self.args.out / hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:16]).exists()
                        for identifier in identifiers):
                    raise Refused('Output already contains unrelated extraction results')
                if self.args.only_round != 1:
                    raise Refused('Transport round 1 must run first')
                self.status.update(mode='staged', input_identity=identity,
                                   manifest_sha256=identity['manifest_sha256'])
                self.persist('running', phase='before_round_1', round=1)

            completed = [record for record in self.history['rounds']
                         if record.get('outcome') in ('completed', 'partial')]
            if len(completed) != len(self.history['rounds']):
                raise Refused('An earlier transport round is unfinished; refusing uncertain replay')
            last = completed[-1] if completed else None
            if last:
                if [record.get('round') for record in completed] != list(range(1, len(completed) + 1)):
                    raise Refused('Persisted round sequence is invalid')
                try:
                    verify_checkpoint(self.args.out, last.get('checkpoint_sha256'), identifiers,force_full=self.args.finalize_only)
                except RunnerFailure as exc:
                    raise Refused(str(exc)) from exc
                validate_summary(last.get('summary'), identifiers, last.get('exit_code'), last['round'])
                if json.loads((self.args.out / 'SUMMARY.json').read_text(encoding='utf-8-sig')) != last['summary']:
                    raise Refused('Current native SUMMARY differs from completed round')

            if self.args.finalize_only:
                if not last or self.status.get('state') not in ('ready_to_finalize', 'completed', 'partial'):
                    raise Refused('Staged extraction is not ready for finalization')
                # This native validator also checks every per-book output and technical checkpoint.
                from importlib.util import module_from_spec, spec_from_file_location
                adapter_path = Path(__file__).resolve().parents[3] / 'adapters/dictionary_extraction.py'
                adapter_spec = spec_from_file_location('_dictionary_finalizer', adapter_path)
                adapter = module_from_spec(adapter_spec)
                adapter_spec.loader.exec_module(adapter)
                outcome = validate_summary(last['summary'], identifiers, last['exit_code'], last['round'])
                final_code = 2 if outcome == 'partial' else 0
                try:
                    adapter.finalized_report(self.args.manifest, self.args.out, final_code,
                                             allow_partial=True, require_driver_state=False)
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    return self.finish('technical_failure', 1, phase='finalization',
                                       error=f'Native finalization rejected extraction: {exc}')
                return self.finish(outcome, final_code, phase='finalized',
                                   stop_reason=f'no_{transport_label(last["summary"])}_pending')

            number = self.args.only_round
            if last and number == last['round']:
                if self.status.get('state') in ('running', 'blocked'):
                    gates = [gate for gate in self.history['gates']
                             if gate.get('phase') == f'after_round_{number}']
                    if not gates or gates[-1].get('outcome') != 'idle':
                        self.wait_idle(f'after_round_{number}', number)
                    snapshot = last['summary']
                    if snapshot[COUNTERS[0]]:
                        if number == 3:
                            return self.finish('technical_failure', 1, phase='round_complete',
                                               error='Final round unexpectedly left pending retries')
                        return self.finish('awaiting_retry', 0, phase='round_complete',
                                           next_round=number + 1, stop_reason=f'{transport_label(snapshot)}_pending')
                    if unresolved_transport(snapshot):
                        return self.finish('technical_failure', 1, phase='round_complete',
                                           stop_reason=f'{transport_label(snapshot)}_exhausted',
                                           error='Unresolved transport failure after final transport attempt')
                    return self.finish('ready_to_finalize', 2 if last['outcome'] == 'partial' else 0,
                                       phase='round_complete', stop_reason=f'no_{transport_label(snapshot)}_pending')
                if self.status.get('state') not in ('awaiting_retry', 'ready_to_finalize'):
                    raise Refused('Completed round is not in a resumable state')
                return 2 if self.status['state'] == 'ready_to_finalize' and last['outcome'] == 'partial' else 0
            if number != len(completed) + 1 or number > 3:
                raise Refused('Transport rounds must run in order without skipping or replay')
            if last and (self.status.get('state') != 'awaiting_retry'
                         or last['summary'][COUNTERS[0]] <= 0):
                raise Refused('No pending transport chunks for another transport round')
            self.wait_idle(f'before_round_{number}', number)
            snapshot, outcome = self.run_round(number, self.args.round_workers[number - 1], identifiers)
            self.history['rounds'][-1]['checkpoint_sha256'] = checkpoint_hashes(self.args.out, identifiers)
            self.persist()
            self.wait_idle(f'after_round_{number}', number)
            if snapshot[COUNTERS[0]]:
                if number == 3:
                    raise RunnerFailure('Final round unexpectedly left pending retries')
                return self.finish('awaiting_retry', 0, phase='round_complete',
                                   next_round=number + 1, stop_reason=f'{transport_label(snapshot)}_pending')
            if unresolved_transport(snapshot):
                return self.finish('technical_failure', 1, phase='round_complete',
                                   stop_reason=f'{transport_label(snapshot)}_exhausted',
                                   error='Unresolved transport failure after final transport attempt')
            return self.finish('ready_to_finalize', 2 if outcome == 'partial' else 0,
                               phase='round_complete', stop_reason=f'no_{transport_label(snapshot)}_pending')
        except Blocked as exc:
            return self.finish('blocked', 3, error=str(exc))
        except Refused as exc:
            print(f'refused: {exc}', file=sys.stderr)
            self.status.update(state='refused', error=str(exc))
            return 1
        except (OSError, KeyError, TypeError, ValueError, RunnerFailure) as exc:
            return self.finish('aborted', 1, error=str(exc))
        except KeyboardInterrupt:
            return self.finish('aborted', 1, error='Driver interrupted; no further rounds launched')

    def run(self):
        self.args.out.mkdir(parents=True, exist_ok=True)
        try:
            with output_lock(self.args.out):
                return self.run_locked()
        except Blocked as exc:
            # Do not overwrite the active owner's status/history.
            print(f'blocked: {exc}', file=sys.stderr)
            return 3


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--context', type=int, default=100000)
    parser.add_argument('--output-tokens', type=int, default=16000)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--book-workers', type=int, default=32)
    parser.add_argument('--round-workers', type=int, nargs=3, default=WORKERS,
                        metavar=('FIRST', 'RETRY_1', 'RETRY_2'),
                        help='Request concurrency for each of the three transport rounds')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--only-round', type=int, choices=(1, 2, 3))
    mode.add_argument('--finalize-only', action='store_true')
    parser.add_argument('--server-context', type=int)
    parser.add_argument('--tokenizer')
    parser.add_argument('--overlap', type=int, default=4)
    parser.add_argument('--repair-batch-size', type=int, default=8)
    parser.add_argument('--repair-attempts', type=int, default=2)
    parser.add_argument('--repair-max-requests', type=int, default=128)
    parser.add_argument('--tokenize-attempts', type=int, default=3)
    parser.add_argument('--runner', type=Path, default=Path(__file__).with_name('fullbook_llm_v5.py'))
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--metrics-url', action='append', help='Repeat for all visible vLLM endpoints')
    parser.add_argument('--metrics-max-wait', type=float, default=1800.)
    parser.add_argument('--metrics-poll-interval', type=float, default=15.)
    parser.add_argument('--metrics-timeout', type=float, default=10.)
    parser.add_argument('--idle-scope', choices=('service', 'scheduler-job'), default='service',
                        help='Shared gateway waits only for this job; does not assert backend GPU idle')
    args = parser.parse_args(argv)
    if min(args.book_workers, args.output_tokens, args.timeout,args.repair_batch_size,
           args.repair_attempts,args.repair_max_requests,args.tokenize_attempts, *args.round_workers) < 1 or args.overlap < 0 \
            or args.context <= args.output_tokens + 4096:
        parser.error('Invalid concurrency, timeout or token budget')
    if args.server_context is not None and args.server_context < args.context:
        parser.error('server-context must be at least context')
    for name in ('metrics_max_wait', 'metrics_poll_interval', 'metrics_timeout'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f'{name.replace("_", "-")} must be finite and positive')
    api = urlsplit(args.api_url)
    if api.scheme not in ('http', 'https') or not api.netloc:
        parser.error('api-url must be an HTTP(S) URL')
    if args.idle_scope == 'scheduler-job':
        job = re.fullmatch(r'/_jobs/([A-Za-z0-9_-]{1,128})/?', api.path)
        if api.hostname not in ('127.0.0.1', '::1') or not job or api.query or api.fragment or api.username or api.password:
            parser.error('scheduler-job scope requires a loopback shared gateway job URL')
        args.scheduler_job_id = job.group(1)
        args.scheduler_stats_url = urlunsplit((api.scheme, api.netloc, '/_scheduler/stats', '', ''))
    if not args.metrics_url:
        args.metrics_url = [urlunsplit((api.scheme, api.netloc, '/metrics', '', ''))]
    for url in args.metrics_url:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            parser.error('metrics-url must be an HTTP(S) URL')
    args.manifest = args.manifest.resolve()
    args.out = args.out.resolve()
    args.runner = args.runner.resolve()
    return args


def main(argv=None):
    orchestration = Orchestrator(parse_args(argv))
    code = orchestration.run()
    print(json.dumps({'state': orchestration.status.get('state', 'blocked'), 'exit_code': code,
                      'out': str(orchestration.args.out), 'error': orchestration.status.get('error')},
                     ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
