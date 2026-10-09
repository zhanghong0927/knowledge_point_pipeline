"""Run v5 in three serial, metrics-gated transport rounds (1024/256/64).

Exit codes: 0 completed; 2 normal partial; 3 blocked metrics/lock; 1 aborted.
The runner owns all cache selection: this driver never deletes or edits caches.
SUMMARY.json must be freshly written after every subprocess, contain complete
expected_books/finished_books/books, and provide both per-book and aggregate
http504_pending_chunks/http504_unresolved_chunks. Round 3 has no pending retries.
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


WORKERS = (1024, 256, 64)
COUNTERS = ('http504_pending_chunks', 'http504_unresolved_chunks')


class Blocked(RuntimeError):
    pass


class RunnerFailure(RuntimeError):
    pass


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
    totals = {key: 0 for key in COUNTERS}
    for book in books:
        status = book.get('status')
        if status not in ('completed', 'partial'):
            raise RunnerFailure(f'Book {book["identifier"]} failed: {status}')
        pending, unresolved = [count(book, key) for key in COUNTERS]
        if pending > unresolved or (status == 'completed' and unresolved):
            raise RunnerFailure('Inconsistent per-book HTTP504 counters/status')
        for key in COUNTERS:
            totals[key] += book[key]
    for key in COUNTERS:
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
                       'round_workers': list(WORKERS), 'book_workers': args.book_workers,
                       'metrics_urls': args.metrics_url}
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
        record, start = self.stage_start('gates', phase, round_number, samples=[])
        deadline = start + self.args.metrics_max_wait
        consecutive = 0
        try:
            while True:
                readings = []
                for url in self.args.metrics_url:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Blocked('Metrics drain timed out before two consecutive idle samples')
                    request = urllib.request.Request(url, headers={'Accept': 'text/plain'})
                    try:
                        with urllib.request.urlopen(request, timeout=min(
                                self.args.metrics_timeout, remaining)) as response:
                            gauges = parse_metrics(response.read().decode('utf-8'))
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
            for round_number, workers in enumerate(WORKERS, 1):
                if self.args.manifest.read_bytes() != manifest_bytes:
                    raise RunnerFailure('The manifest changed between transport rounds')
                snapshot, outcome = self.run_round(round_number, workers, identifiers)
                self.wait_idle(f'after_round_{round_number}', round_number)
                if snapshot[COUNTERS[0]] == 0:
                    reason = ('http504_exhausted' if round_number == 3 and snapshot[COUNTERS[1]]
                              else 'no_http504_pending')
                    return self.finish(outcome, 2 if outcome == 'partial' else 0, stop_reason=reason)
            raise RunnerFailure('Final round unexpectedly left pending retries')
        except Blocked as exc:
            return self.finish('blocked', 3, error=str(exc))
        except (OSError, ValueError, RunnerFailure) as exc:
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
    parser.add_argument('--server-context', type=int)
    parser.add_argument('--tokenizer')
    parser.add_argument('--overlap', type=int, default=4)
    parser.add_argument('--runner', type=Path, default=Path(__file__).with_name('fullbook_llm_v5.py'))
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--metrics-url', action='append', help='Repeat for all visible vLLM endpoints')
    parser.add_argument('--metrics-max-wait', type=float, default=1800.)
    parser.add_argument('--metrics-poll-interval', type=float, default=15.)
    parser.add_argument('--metrics-timeout', type=float, default=10.)
    args = parser.parse_args(argv)
    if min(args.book_workers, args.output_tokens, args.timeout) < 1 or args.overlap < 0 \
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
