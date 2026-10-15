"""Offline checkpoint migration and narrowly evidenced recovery permissions.

A running round is an incomplete checkpoint. A full current-file snapshot and
the unchanged completed-round prefix are both required before its continuation.
"""
import ast
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import time
import threading
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def writer_host():
    return os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME')


def process_start_ticks(pid):
    """Linux process identity; unavailable hosts never auto-release a dead lock."""
    try:
        value = Path('/proc') / str(pid) / 'stat'
        return int(value.read_text().rsplit(')', 1)[1].split()[19])
    except (FileNotFoundError, ValueError, IndexError, OSError):
        return None


def _authorized_writer_receipt(out, receipt_path):
    out = Path(out).resolve(strict=True)
    receipt_path = Path(receipt_path).resolve(strict=True)
    expected = out.parent / (out.name + '_RESUME_RECEIPT.json')
    value = read(receipt_path)
    source = Path(value.get('source', '')).resolve(strict=True)
    if (receipt_path != expected or value.get('schema') != 1 or
            Path(value.get('target', '')).resolve() != out or source == out or
            out.is_relative_to(source) or source.is_relative_to(out) or
            value.get('new_manifest') != read(out / 'MANIFEST.json')):
        raise ValueError('Writer lock is not an authorized independent resume target')
    return receipt_path


_writer_guard_mutex = threading.RLock()
_writer_guards = {}
_writer_guard_local = threading.local()


@contextmanager
def writer_guard(out):
    """One external OS lock for owner check, dead cleanup, creation and release."""
    out = Path(out).resolve(strict=True)
    path = out.parent / (out.name + '_WRITER_GUARD.lock')
    key = str(path)
    with _writer_guard_mutex:
        mutex = _writer_guards.setdefault(key, threading.RLock())
    with mutex:
        owned = getattr(_writer_guard_local, 'owned', set())
        if key in owned:
            yield
            return
        with path.open('a+b') as stream:
            try:
                import fcntl
            except ImportError:
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b'0');stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                release = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            _writer_guard_local.owned = owned | {key}
            try:
                yield
            finally:
                _writer_guard_local.owned = owned
                release()


def verify_writer_idle(out, receipt_path=None, *, clear_stale=False, allow_current=False):
    out = Path(out).resolve(strict=True)
    with writer_guard(out):
        return _verify_writer_idle_locked(out, receipt_path, clear_stale=clear_stale, allow_current=allow_current)


def _verify_writer_idle_locked(out, receipt_path=None, *, clear_stale=False, allow_current=False):
    """Never write over a live/unknown owner; release proven dead NEW locks only."""
    out = Path(out).resolve(strict=True)
    lock = out / '.running'
    if lock.is_symlink():
        raise ValueError('Unknown writer lock symlink')
    if not lock.exists():
        return None
    receipt_path = _authorized_writer_receipt(out, receipt_path or
        out.parent / (out.name + '_RESUME_RECEIPT.json'))
    raw = lock.read_bytes()
    try:
        owner = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('Unknown writer lock metadata') from exc
    if (owner.get('schema') != 1 or owner.get('kind') != 'dictionary_resumed_writer' or
            owner.get('host') != writer_host() or not writer_host() or
            Path(owner.get('target', '')).resolve() != out or type(owner.get('pid')) is not int or
            owner['pid'] <= 0 or owner.get('resume_receipt_sha256') != digest(receipt_path)):
        raise ValueError('Unknown writer owner/host/receipt identity; offline inspection is required')
    if (allow_current and owner['pid'] == os.getpid() and
            owner.get('process_start_ticks') == process_start_ticks(os.getpid())):
        return owner
    try:
        os.kill(owner['pid'], 0)
    except ProcessLookupError:
        dead = True
    except (PermissionError, OSError) as exc:
        raise ValueError('Cannot prove the writer process is dead') from exc
    else:
        raise ValueError('A live native writer still owns this independent cache')
    if (not clear_stale or not dead or type(owner.get('process_start_ticks')) is not int or
            owner['process_start_ticks'] <= 0):
        raise ValueError('Dead lock lacks explicit resumable owner/start identity; offline inspection is required')
    audit = out.parent / (out.name + '_LOCK_RECOVERY.jsonl')
    event = {'schema': 1, 'target': str(out), 'old_lock_sha256': hashlib.sha256(raw).hexdigest(),
             'owner': owner, 'resume_receipt_sha256': digest(receipt_path),
             'evidence': 'same_host_process_lookup_error', 'processes_signalled': False}
    with audit.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({**event, 'state': 'release_authorized'}, ensure_ascii=False) + '\n')
    if lock.read_bytes() != raw:
        raise ValueError('Writer lock changed while checking its dead owner')
    lock.unlink()
    with audit.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({**event, 'state': 'released'}, ensure_ascii=False) + '\n')
    return owner


@contextmanager
def resumed_book_lock(out, receipt_path):
    out = Path(out).resolve(strict=True)
    receipt_path = _authorized_writer_receipt(out, receipt_path)
    lock = out / '.running'
    with writer_guard(out):
        _verify_writer_idle_locked(out, receipt_path, clear_stale=True)
        owner = {'schema': 1, 'kind': 'dictionary_resumed_writer', 'host': writer_host(),
            'pid': os.getpid(), 'process_start_ticks': process_start_ticks(os.getpid()),
            'target': str(out), 'resume_receipt_sha256': digest(receipt_path), 'started': time.time()}
        with lock.open('x', encoding='utf-8') as stream:
            json.dump(owner, stream, ensure_ascii=False)
    try:
        yield
    finally:
        with writer_guard(out):
            if lock.exists():
                if read(lock) != owner:
                    raise ValueError('Writer lock owner changed; refusing to remove another lock')
                lock.unlink()


def _bounded_io(items, function):
    """At most 32 filesystem readers and 128 submitted operations."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=32) as pool:
        pending = set()
        exhausted = False
        while pending or not exhausted:
            while not exhausted and len(pending) < 128:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(pool.submit(function, item))
            if pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for job in completed:
                    yield job.result()


def snapshot_files(root):
    """Hash every file, including state, logs and journals; exclude only the lock."""
    root = Path(root).resolve(strict=True)
    result = {}
    def inspect(path):
        if path.is_symlink():
            raise ValueError('Checkpoint symlinks are not independent cache files')
        if path.is_file() and path.name != '.running':
            return path.relative_to(root).as_posix(), {'sha256': digest(path), 'size': path.stat().st_size}
    for value in _bounded_io(root.rglob('*'), inspect):
        if value is not None:
            result[value[0]] = value[1]
    return dict(sorted(result.items()))


_PASSED_FIELDS = ('id', 'knowledge_point', 'name', 'definition', 'en_definition',
                  'description', 'en_description', 'source')


def _passed_content_hash(row):
    return hashlib.sha256(json.dumps({key: row.get(key) for key in _PASSED_FIELDS},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def retained_passed_baseline(out):
    path = Path(out) / 'FINAL_RECORDS.jsonl'
    records = {}
    if path.exists():
        for row in _journal_rows(path):
            sid = row.get('id')
            if not sid or sid in records:
                raise ValueError('Invalid or duplicate current passed record ID')
            records[sid] = _passed_content_hash(row)
    return {'source': str(path), 'sha256': digest(path) if path.exists() else None,
            'records': records, 'fields': list(_PASSED_FIELDS), 'count': len(records)}


def verify_retained_passes(out, receipt_path):
    receipt = read(receipt_path)
    expected = receipt.get('retained_passed')
    if not isinstance(expected, dict):
        raise ValueError('Missing immediately frozen retained passed baseline')
    current = retained_passed_baseline(out)
    if any(current['records'].get(sid) != checksum for sid, checksum in expected['records'].items()):
        raise ValueError('An immediately frozen passed record disappeared or its retained content changed')
    return {'retained': expected['count'], 'current_passed': current['count']}


def _safe_relative(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative:
        raise ValueError('Checkpoint path escapes its root')
    return Path(root) / path


def _journal_rows(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.endswith('\n'):
                raise ValueError('Torn request journal; preserve and inspect its partial tail')
            if line.strip():
                try:
                    yield json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise ValueError('Invalid request journal: ' + str(path)) from exc


def _array_rows(path):
    """Read a potentially large frozen JSON array without retaining raw bodies."""
    decoder = json.JSONDecoder()
    with Path(path).open(encoding='utf-8-sig') as stream:
        buffer, position, eof = '', 0, False
        started, expect_value = False, True
        while True:
            if position >= len(buffer) and not eof:
                buffer, position = stream.read(1024 * 1024), 0
                eof = not buffer
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position >= len(buffer):
                if eof:
                    raise ValueError('Incomplete cleaning input array')
                continue
            if not started:
                if buffer[position] != '[':
                    raise ValueError('Cleaning input must be a JSON array')
                started, position = True, position + 1
                continue
            if buffer[position] == ']':
                if buffer[position + 1:].strip() or stream.read().strip():
                    raise ValueError('Trailing data after cleaning input array')
                return
            if not expect_value:
                if buffer[position] != ',':
                    raise ValueError('Invalid cleaning input array delimiter')
                expect_value, position = True, position + 1
                continue
            try:
                row, end = decoder.raw_decode(buffer, position)
            except ValueError:
                more = stream.read(1024 * 1024)
                if not more:
                    raise ValueError('Invalid or truncated cleaning input array')
                buffer, position = buffer[position:] + more, 0
                continue
            yield row
            position, expect_value = end, False


def _record_identity(row):
    return (hashlib.sha256(json.dumps(row.get('source'), sort_keys=True).encode()).hexdigest(),
            hashlib.sha256(str(row.get('raw_content')).encode()).hexdigest())


def _filter_module(script):
    spec = importlib.util.spec_from_file_location('_resume_filter_' + Path(script).stem, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project_filter(module, source_row, vote, line_number):
    class Buffer(io.StringIO):
        def close(self):
            pass

        def open(self, *_args, **_kwargs):
            return self
    outputs = {key: Buffer() for key in ('all_full', 'keep_full', 'drop_full', 'review_full')}
    source_row = module.preclean_title_fields(source_row) if hasattr(module, 'preclean_title_fields') else source_row
    record = {'source_row': source_row, 'id': source_row['id'],
              'line_no': source_row.get('line_no', line_number),
              'name': source_row.get('name', ''), 'knowledge_point': source_row.get('knowledge_point', '')}
    module.write_full_record_outputs([record], {module.record_key(record): vote}, outputs)
    return json.loads(outputs['all_full'].getvalue())


def _native_error_receipt(vote):
    # Existing native error constructors omit policy/config metadata. Their
    # request input and frozen source/config still bind the attempted operation.
    return (vote.get('api_status') not in (None, 'ok', 'local') and
            vote.get('decision') == 'review' and (bool(vote.get('api_error')) or
                vote.get('api_status') == vote.get('reason') == 'missing_result') and
            vote.get('failure_kind') in ('transport', 'content_or_format', 'format'))


def _policy_matches(vote, formatter):
    return (vote.get('policy_version') == formatter.POLICY_VERSION or
            vote.get('policy_version') is None and _native_error_receipt(vote))


def filter_journal_records(target, script, values, stem, scope_path=None):
    """Treat both success and a recorded failure as attempted in the same round."""
    target = Path(target)
    journal = target / (stem + '_judgments.jsonl')
    if not journal.exists():
        return {}, values
    module = _filter_module(script)
    inputs = _journal_rows(target / 'input.jsonl')
    original = {row['id']: (number, row) for number, row in enumerate(inputs, 1)}
    votes = {}
    for row in _journal_rows(journal):
        if row['id'] in votes or row['id'] not in original:
            raise ValueError('Duplicate or foreign filter journal IDs')
        votes[row['id']] = row
    scope_digest = module.load_scope_config(Path(scope_path))[1] if scope_path else None
    cached, pending = {}, []
    for row in values:
        sid = row['id']
        if sid not in votes:
            pending.append(row)
            continue
        number, source_row = original[sid]
        vote = votes[sid]
        if (not _policy_matches(vote, module) or
                vote.get('decision') not in ('keep', 'drop', 'review') or
                (scope_digest is not None and vote.get('config_sha256') != scope_digest
                 and not (vote.get('config_sha256') is None and _native_error_receipt(vote))) or
                any(source_row.get(key) != row.get(key) for key in ('source', 'raw_content'))):
            raise ValueError('Filter journal policy/input/source identity changed')
        cached[sid] = _project_filter(module, source_row, vote, number)
    return cached, pending


_ID = re.compile(rb'(?<!\\)"id"\s*:\s*"([^"\\]+)"')
_HTTP404 = re.compile(rb'(?:HTTPError\s+404\b|HTTPError\(code=404\b)')


def _line_id(line):
    match = _ID.search(line)
    if match is None:
        raise ValueError('Missing unambiguous journal record ID')
    return match[1].decode('utf-8')


def _line_index(path):
    result = {}
    with Path(path).open('rb') as stream:
        number = 0
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            number += 1
            if not line.strip():
                continue
            if not line.endswith(b'\n'):
                raise ValueError('Torn journal or historical aggregate')
            sid = _line_id(line)
            if sid in result:
                raise ValueError('Duplicate historical or journal record ID')
            result[sid] = (offset, number)
    return result


def _indexed_line(stream, index, sid):
    stream.seek(index[sid][0])
    return stream.readline()


def _seed_history(source, relative, expected, current_round, native):
    """Reconstruct the frozen seed_filter bytes, then verify the current merge.

    The fast path scans line IDs and hashes bytes without loading giant bodies.
    Its candidate is accepted only when its full hash equals the old receipt.
    """
    from contextlib import ExitStack

    parts = Path(relative).parts
    if len(parts) < 2 or parts[0] not in ('name', 'scope') or parts[-1] not in ('FIRST.jsonl', 'FINAL.jsonl'):
        raise ValueError('Completed checkpoint prefix changed; historical baseline required: ' + relative)
    provenance_path = Path(source) / 'SOURCE_CACHE.json'
    if not provenance_path.is_file():
        raise ValueError('Changed completed prefix has no historical source provenance: ' + relative)
    proof = read(provenance_path)
    original = Path(proof['source']).resolve(strict=True)
    raw_file = _safe_relative(original, relative)
    if digest(raw_file) != proof['files'].get(relative):
        raise ValueError('Historical aggregate differs from SOURCE_CACHE proof')
    stage = 'name' if parts[0] == 'name' else 'scope'
    annotation = native.NAME if stage == 'name' else native.SCOPE
    stem = 'llm_name_title_format' if stage == 'name' else 'subject_scope'
    ids = set()
    for path in sorted(raw_file.parent.glob('*/' + stem + '_judgments.jsonl')):
        key = path.relative_to(original).as_posix()
        if digest(path) != proof['files'].get(key):
            raise ValueError('Historical 404 journal differs from SOURCE_CACHE proof')
        with path.open('rb') as stream:
            for line in stream:
                if _HTTP404.search(line):
                    value = json.loads(line)
                    if value.get('api_status') == 'api_error_after_retries' and _HTTP404.search(
                            str(value.get('api_error', '')).encode()):
                        ids.add(value['id'])

    def seed_line(line):
        if _line_id(line) not in ids:
            return line
        old = b'"failure_kind": "content_or_format"'
        if old in line and line.count(b'"failure_kind"') == 1:
            return line.replace(old, b'"failure_kind": "transport"', 1)
        value = json.loads(line)
        vote = value[annotation]
        if vote.get('api_status') == 'api_error_after_retries' and vote.get('failure_kind') == 'content_or_format':
            vote['failure_kind'] = 'transport'
        return (json.dumps(value, ensure_ascii=False) + '\n').encode()

    rebuilt = hashlib.sha256()
    with raw_file.open('rb') as stream:
        for line in stream:
            rebuilt.update(seed_line(line))
    if rebuilt.hexdigest() != expected:
        raise ValueError('Historical seed reconstruction does not match completed prefix SHA256: ' + relative)
    folder = Path(source) / Path(relative).parent
    script = native.ROOT / 'name_filters' / ('01_name_title_quality.py' if stage == 'name'
                                           else '02_subject_scope_filter.py')
    formatter = _filter_module(script)
    journals = {}
    for context in (False, True):
        target = folder / ('round_' + str(current_round) + ('_review_context' if context else ''))
        votes = {}
        if target.exists():
            for journal in sorted(target.rglob(stem + '_judgments.jsonl')):
                index = _line_index(journal.parent / 'input.jsonl')
                for vote in _journal_rows(journal):
                    sid = vote.get('id')
                    if sid not in index or sid in votes or not _policy_matches(vote, formatter):
                        raise ValueError('Current request journal identity/policy differs')
                    votes[sid] = (vote, journal.parent / 'input.jsonl', index[sid])
        journals[context] = votes
    primary = journals[False]
    contexts = journals[True] if parts[-1] == 'FINAL.jsonl' else {}
    baseline_index = _line_index(raw_file)
    wanted = set(baseline_index) | set(primary)
    seen = set()
    with ExitStack() as file_stack, raw_file.open('rb') as baseline, (Path(source) / relative).open('rb') as current:
        request_streams = {}
        for line in current:
            sid = _line_id(line)
            if sid not in wanted or sid in seen:
                raise ValueError('Current aggregate ID coverage differs from deterministic journal merge')
            seen.add(sid)
            entry = contexts.get(sid) or primary.get(sid)
            if entry:
                vote, input_file, (offset, number) = entry
                stream = request_streams.get(input_file)
                if stream is None:
                    stream = file_stack.enter_context(input_file.open('rb'))
                    request_streams[input_file] = stream
                if stream.tell() != offset:
                    stream.seek(offset)
                row = json.loads(stream.readline())
                expected_row = _project_filter(formatter, row, vote, number)
                if json.loads(line) != expected_row:
                    raise ValueError('Current aggregate differs from completed request journal merge: ' + relative)
            elif line != seed_line(_indexed_line(baseline, baseline_index, sid)):
                raise ValueError('Current aggregate changed without a current request journal: ' + relative)
    if seen != wanted:
        raise ValueError('Current aggregate has missing request journal IDs')
    return {'file': relative, 'historical_sha256': expected, 'original_sha256': digest(raw_file),
            'method': 'frozen seed_filter reconstruction; exact historical SHA256; deterministic current journal merge',
            'current_round': current_round, 'revoked_404_ids': len(ids)}


def _migrated_manifest_history(source, relative, expected, native):
    if relative != 'MANIFEST.json':
        return None
    source = Path(source)
    receipt_path = source.parent / (source.name + '_RESUME_RECEIPT.json')
    if not receipt_path.exists():
        return None
    receipt = read(receipt_path)
    original = Path(receipt['source']) / relative
    if (receipt.get('target') != str(source.resolve()) or
            receipt.get('source_files', {}).get(relative, {}).get('sha256') != expected or
            not original.is_file() or digest(original) != expected or
            read(source / relative) != receipt.get('new_manifest') or
            _semantic_identity(read(original)) != _semantic_identity(read(source / relative))):
        raise ValueError('Operational manifest migration proof differs from completed prefix')
    return {'file': relative, 'historical_sha256': expected,
            'method': 'sealed independent-copy operational manifest migration',
            'original': str(original)}


def stable_checkpoint_hashes(files):
    """Apply the native checkpoint filter to an already complete byte snapshot."""
    volatile = {'STATE.json', 'ORCHESTRATION_STATUS.json', 'PROGRESS.json', '.running'}
    return {key: value['sha256'] for key, value in files.items()
            if Path(key).name not in volatile and Path(key).suffix not in ('.log', '.tmp', '.lock')}


def capture_round_baseline(out, round_number, previous, receipt_path):
    from round_resume_baseline import capture
    return capture(out, round_number, previous, receipt_path)


def inspect_checkpoint(source, input_path, native):
    source = Path(source).resolve(strict=True)
    before = snapshot_files(source)
    manifest = read(source / 'MANIFEST.json')
    if manifest.get('input_sha256') != digest(input_path):
        raise ValueError('Cleaning input identity changed')
    by_id = {}
    for row in _array_rows(input_path):
        if not isinstance(row, dict) or not row.get('id') or row['id'] in by_id:
            raise ValueError('Invalid or duplicate cleaning input IDs')
        by_id[row['id']] = _record_identity(row)
    state = read(source / 'STATE.json')
    previous = read(source / 'ORCHESTRATION_STATUS.json')
    completion_pending = (state.get('status') == 'round_running' and
        state.get('round') == previous.get('round') and previous.get('round') in (1, 2, 3) and
        previous.get('state') in ('awaiting_retry', 'ready_to_finalize', 'technical_failure') and
        stable_checkpoint_hashes(before) == previous.get('checkpoint_hashes'))
    interrupted = state.get('status') == 'round_running' and not completion_pending
    completed = previous.get('round')
    historical = []
    if type(completed) is not int or completed not in (1, 2, 3):
        raise ValueError('Missing completed round receipt')
    if interrupted:
        current = state.get('round')
        if (current not in (2, 3) or completed != current - 1 or
                previous.get('state') != 'awaiting_retry' or previous.get('next_round') != current):
            raise ValueError('Interrupted round does not follow its completed prefix')
        prefix = previous.get('checkpoint_hashes')
        if not isinstance(prefix, dict) or not prefix:
            raise ValueError('Missing completed checkpoint prefix')
        def verify_prefix(pair):
            relative, checksum = pair
            if Path(relative).name == '.running':
                return None
            _safe_relative(source, relative)
            if before.get(relative, {}).get('sha256') != checksum:
                return relative, checksum
        changed = [pair for pair in _bounded_io(prefix.items(), verify_prefix) if pair is not None]
        for relative, checksum in changed:
            from round_resume_baseline import history
            round_baseline = history(source, relative, checksum, current, native)
            if round_baseline is not None:
                historical.append(round_baseline)
                continue
            migration = _migrated_manifest_history(source, relative, checksum, native)
            historical.append(migration or _seed_history(source, relative, checksum, current, native))
    else:
        expected = {key: value for key, value in previous.get('checkpoint_hashes', {}).items()
                    if Path(key).name != '.running'}
        if not expected or stable_checkpoint_hashes(before) != expected:
            raise ValueError('Dictionary cleaning checkpoint changed after round')
        current = previous.get('next_round') if previous.get('state') == 'awaiting_retry' else None
        if state.get('round') != completed:
            raise ValueError('Native state differs from completed checkpoint receipt')
    journal_records = 0
    for path in sorted(source.rglob('*judgments.jsonl')):
        input_file = path.parent / 'input.jsonl'
        if not input_file.exists():
            raise ValueError('Request journal has no original request input')
        requested = set()
        for row in _journal_rows(input_file):
            sid = row.get('id')
            if sid not in by_id or sid in requested:
                raise ValueError('Request journal input contains foreign/duplicate IDs')
            if _record_identity(row) != by_id[sid]:
                raise ValueError('Request journal source identity changed')
            requested.add(sid)
        values = _journal_rows(path)
        seen = set()
        for row in values:
            sid = row.get('id')
            if sid not in requested or sid in seen:
                raise ValueError('Foreign or duplicate request journal identity')
            seen.add(sid)
            if row.get('api_status') is None or row.get('decision') not in ('keep', 'drop', 'review'):
                raise ValueError('Request journal is missing a completed per-request verdict')
        journal_records += len(seen)
    for path in sorted((source / 'content/calls').glob('*/*.json')):
        value = read(path)
        if (path.stem not in by_id or not isinstance(value.get('packet'), dict) or
                not isinstance(value.get('prompt'), str) or value.get('status') not in ('ok', 'failed')):
            raise ValueError('Content request journal has invalid identity or protocol')
        call_round = value.get('round', 1)
        if type(call_round) is not int or not 1 <= call_round <= (current or completed):
            raise ValueError('Content request journal round is inconsistent')
        journal_records += 1
    if snapshot_files(source) != before:
        raise ValueError('Checkpoint changed during offline inspection')
    return {'schema': 1, 'source': str(source), 'files': before, 'old_manifest': manifest,
            'completed_round': completed, 'resume_round': current, 'round_complete': not interrupted,
            'stable_completed_prefix_verified': True, 'journal_records': journal_records,
            'historical_seed_proofs': historical,
            'native_completion_pending': completion_pending,
            'retained_passed': retained_passed_baseline(source),
            'semantic_quality_approved': False}


def _semantic_identity(manifest):
    operational = {'api_url', 'workers', 'staged_round', 'recovery_source', 'recovery_provenance'}
    return {key: manifest[key] for key in ('kind', 'input_sha256', 'sources')} | {
        'config': {key: value for key, value in manifest['config'].items() if key not in operational}}


def prompt_identity(root):
    """Compare prompt/schema expressions independently of operational code edits."""
    values = {}
    for path in sorted(Path(root).rglob('*.py')):
        relative_parts = path.relative_to(root).parts
        if '__pycache__' in path.parts or any(part in ('outputs', 'runs', 'tests', 'docs') for part in relative_parts):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        selected = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and 'prompt' in node.name.lower():
                selected.append(ast.dump(node, include_attributes=False))
            elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and any(word in target.id.upper()
                        for word in ('PROMPT', 'POLICY', 'SCHEMA', 'EXTENSION')) for target in node.targets):
                selected.append(ast.dump(node, include_attributes=False))
        if selected:
            values[path.relative_to(root).as_posix()] = selected
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def native_code_hashes(root):
    root = Path(root).resolve(strict=True)
    return {str(path.relative_to(root)): digest(path) for path in root.rglob('*.py')
            if '__pycache__' not in path.parts and not any(part in ('outputs', 'runs')
            for part in path.relative_to(root).parts)}


def code_migration_proof(old_code, new_code, frozen_source_code):
    roots = [Path(path).resolve(strict=True) for path in (old_code, new_code, frozen_source_code)]
    identities = [prompt_identity(root) for root in roots]
    if len(set(identities)) != 1:
        raise ValueError('Operational code migration changed a prompt/schema/policy identity')
    return {'schema': 1, 'purpose': 'explicit_operational_code_migration', 'prompt_identity': identities[0],
            **{role: {'root': str(root), 'files': native_code_hashes(root)}
               for role, root in zip(('old', 'new', 'frozen_source'), roots)},
            'semantic_quality_approved': False}


def verify_code_migration(token, old_hashes, new_hashes):
    if digest(token['path']) != token['sha256']:
        raise ValueError('Code migration proof hash changed')
    proof = read(token['path'])
    rebuilt = code_migration_proof(proof['old']['root'], proof['new']['root'], proof['frozen_source']['root'])
    if (rebuilt != proof or old_hashes != proof['old']['files'] or new_hashes != proof['new']['files']):
        raise ValueError('Code migration old/new exact source identity differs')
    return proof


def verify_freeze(source, freeze_receipt, book_checkpoints):
    if freeze_receipt is None or book_checkpoints is None:
        raise ValueError('A frozen source requires freeze and book checkpoint evidence')
    proof, books = read(freeze_receipt), read(book_checkpoints)
    source = Path(source).resolve(strict=True)
    frozen_root = Path(proof.get('root', '')).resolve(strict=True)
    if (proof.get('state') != 'frozen' or proof.get('scope') != 'dictionary_only' or
            not proof.get('gateway_drained') or not proof.get('processes') or
            any(row.get('state') != 'T' for row in proof['processes']) or
            not source.is_relative_to(frozen_root)):
        raise ValueError('Frozen source evidence does not prove stopped dictionary processes')
    matches = [row for row in books if Path(row['out']).resolve() == source]
    if len(matches) != 1:
        raise ValueError('Frozen book checkpoint identity differs')
    for name, metadata in matches[0]['files'].items():
        if name == '.running':
            continue
        if digest(_safe_relative(source, name)) != metadata['sha256']:
            raise ValueError('Frozen book checkpoint changed: ' + name)
    return {'freeze_sha256': digest(freeze_receipt), 'books_sha256': digest(book_checkpoints),
            'frozen_root': str(frozen_root), 'freeze_time': proof.get('time')}


def _file_set(root):
    result = set()
    root = Path(root)
    def inspect(path):
        if path.is_symlink():
            raise ValueError('Checkpoint symlinks are not independent cache files')
        if path.is_file() and path.name != '.running':
            return path.relative_to(root).as_posix()
    for relative in _bounded_io(root.rglob('*'), inspect):
        if relative is not None:
            result.add(relative)
    return result


def _verified_copy(original, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    result = hashlib.sha256()
    size = 0
    with original.open('rb') as source, target.open('xb') as output:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
            if output.write(block) != len(block):
                raise OSError('Independent checkpoint copy was incomplete')
            size += len(block)
    if result.hexdigest() != expected['sha256'] or size != expected['size']:
        raise ValueError('Source hash changed while copying independent cache: ' + str(original))
    target.chmod(target.stat().st_mode | stat.S_IWUSR)
    return {'sha256': result.hexdigest(), 'size': size}


def migrate_checkpoint(source, destination, input_path, config, books, native, source_code=None,
                       freeze_receipt=None, book_checkpoints=None,
                       inspection_receipt=None, inspection_sha256=None):
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if destination == source or destination in source.parents or source in destination.parents:
        raise ValueError('Migration requires an independent output directory')
    if destination.exists():
        raise FileExistsError('Migration output already exists; inspect it before reuse')
    freeze = verify_freeze(source, freeze_receipt, book_checkpoints)
    if inspection_receipt is None:
        inspection = inspect_checkpoint(source, input_path, native)
    else:
        if not inspection_sha256 or digest(inspection_receipt) != inspection_sha256:
            raise ValueError('Inspection receipt hash binding differs')
        inspection = read(inspection_receipt)
        if (inspection.get('schema') != 1 or inspection.get('source') != str(source) or
                inspection.get('stable_completed_prefix_verified') is not True or
                inspection.get('semantic_quality_approved') is not False or
                inspection.get('old_manifest') != read(source / 'MANIFEST.json') or
                inspection['old_manifest']['input_sha256'] != digest(input_path) or
                _file_set(source) != set(inspection['files'])):
            raise ValueError('Inspection source identity or complete file set changed')
        if 'retained_passed' not in inspection:
            inspection['retained_passed'] = retained_passed_baseline(source)
        inspection['inspection_reused'] = {'path': str(inspection_receipt), 'sha256': inspection_sha256}
    new_manifest = native.staged_manifest(input_path, config, books)
    old_manifest = inspection['old_manifest']
    if _semantic_identity(new_manifest) != _semantic_identity(old_manifest):
        raise ValueError('Migration semantic identity changed: input/model/context/source/config')
    if new_manifest['code'] != old_manifest['code']:
        if source_code is None:
            raise ValueError('Changed operational code requires the original native code for prompt verification')
        original_code = Path(source_code).resolve(strict=True)
        if any(digest(_safe_relative(original_code, rel)) != checksum
               for rel, checksum in old_manifest['code'].items()):
            raise ValueError('Original code differs from frozen manifest')
        if prompt_identity(original_code) != prompt_identity(native.ROOT):
            raise ValueError('Migration prompt/schema identity changed')
    destination.mkdir(parents=True)
    copied = {}
    def copy_file(relative):
        original, target = _safe_relative(source, relative), _safe_relative(destination, relative)
        return relative, _verified_copy(original, target, inspection['files'][relative])
    copied.update(_bounded_io(inspection['files'], copy_file))
    if _file_set(source) != set(inspection['files']) or _file_set(destination) != set(copied):
        raise ValueError('Checkpoint file set changed while copying independent cache')
    save(destination / 'MANIFEST.json', new_manifest)
    copied['MANIFEST.json'] = {'sha256': digest(destination / 'MANIFEST.json'),
                               'size': (destination / 'MANIFEST.json').stat().st_size}
    if inspection['round_complete']:
        status = read(destination / 'ORCHESTRATION_STATUS.json')
        status['checkpoint_hashes'] = {relative: copied[relative]['sha256']
            for relative in status['checkpoint_hashes'] if Path(relative).name != '.running'}
        save(destination / 'ORCHESTRATION_STATUS.json', status)
        copied['ORCHESTRATION_STATUS.json'] = {'sha256': digest(destination / 'ORCHESTRATION_STATUS.json'),
            'size': (destination / 'ORCHESTRATION_STATUS.json').stat().st_size}
    receipt = {**inspection, 'target': str(destination), 'source_files': inspection['files'],
               'freeze_evidence': freeze,
               'new_manifest': new_manifest, 'files': copied,
               'allowed_changes': ['independent writable cache', 'loopback gateway URL',
                                   'operational code with unchanged prompt/schema expressions'],
               'source_mutated': False}
    receipt_path = destination.parent / (destination.name + '_RESUME_RECEIPT.json')
    save(receipt_path, receipt)
    return receipt_path


def verify_resume(out, receipt_path, input_path, config, books, native, round_number):
    out = Path(out).resolve(strict=True)
    receipt = read(receipt_path)
    if (receipt.get('schema') != 1 or receipt.get('target') != str(out) or
            receipt.get('semantic_quality_approved') is not False or
            receipt.get('stable_completed_prefix_verified') is not True):
        raise ValueError('Invalid offline resume receipt')
    if receipt.get('files') != snapshot_files(out):
        raise ValueError('Migrated checkpoint hash or file set changed since resume inspection')
    if (receipt.get('new_manifest') != native.staged_manifest(input_path, config, books) or
            read(out / 'MANIFEST.json') != receipt['new_manifest']):
        raise ValueError('Resume input/model/context/source/code identity changed')
    if receipt.get('resume_round') != round_number:
        raise ValueError('Resume must continue the recorded unfinished/next round')
    return receipt


def models_preflight(config, pause_path, round_number, open_fn=None, wait_fn=None):
    """A temporary GET /models failure pauses the same round without a retry vote."""
    open_fn, wait_fn = open_fn or urlopen, wait_fn or time.sleep
    attempt = 0
    while True:
        try:
            with open_fn(config['api_url'].rstrip('/') + '/v1/models', timeout=30) as response:
                models = json.load(response)
            if config['model'] not in {value['id'] for value in models['data']}:
                raise ValueError('Requested model identity is not advertised by service')
            save(pause_path, {'state': 'healthy', 'round': round_number,
                              'preflight_attempts': attempt + 1, 'model': config['model']})
            return models
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            delay = min(30, 5 * 2 ** min(attempt, 3))
            attempt += 1
            save(pause_path, {'state': 'paused_service_preflight', 'round': round_number,
                'preflight_attempts': attempt, 'retry_after_seconds': delay,
                'error': repr(exc), 'retry_round_consumed': False, 'model': config['model']})
            wait_fn(delay)


def deployment_revocation_proof(current, evidence_path, capture_path, upstream_url):
    """Bind the diagnosed upstream retirement to the sealed original r1 cache."""
    current = Path(current).resolve(strict=True)
    evidence_path, capture_path = Path(evidence_path).resolve(strict=True), Path(capture_path).resolve(strict=True)
    evidence, capture = read(evidence_path), read(capture_path)
    observed = evidence.get('api_check', {})
    if (observed.get('url', '').rstrip('/') != upstream_url.rstrip('/') or
            observed.get('get_health') != 404 or observed.get('get_v1_models') != 404 or
            observed.get('response_type') != 'nginx HTML 404' or
            'expired and deregistered' not in evidence.get('user_confirmed_cause', '') or
            capture.get('gateway', {}).get('phase') != 'dictionary_cleaning_round_1'):
        raise ValueError('Specific upstream service deployment retirement evidence differs')
    provenance = current / 'SOURCE_CACHE.json'
    sealed = read(provenance)
    original = Path(sealed['source']).resolve(strict=True)
    production = Path(capture['production_root']).resolve(strict=True)
    if not original.is_relative_to(production) or current.is_relative_to(production):
        raise ValueError('Deployment retirement proof names another original source or a writable original')
    failed = set()
    ledger = current / 'DISPOSITIONS.jsonl'
    if ledger.exists():
        for row in _journal_rows(ledger):
            if row.get('content_status') == 'failed' and row.get('content_failure_kind') == 'transport':
                failed.add(row['id'])
    request_files = {}
    for relative, checksum in sealed['files'].items():
        if (relative.startswith(('name/', 'scope/')) and relative.endswith('_judgments.jsonl') or
                relative.startswith('content/calls/') and Path(relative).stem in failed):
            request_files[relative] = checksum
    return {'schema': 1, 'event': 'service_deployment_deregistered', 'source': str(original),
        'upstream_url': upstream_url.rstrip('/'), 'request_files': request_files,
        'evidence': {'path': str(evidence_path), 'sha256': digest(evidence_path)},
        'capture': {'path': str(capture_path), 'sha256': digest(capture_path)},
        'source_cache': {'path': str(provenance), 'sha256': digest(provenance)}}


def _verify_deployment_proof(proof, original):
    for key in ('evidence', 'capture', 'source_cache'):
        if digest(proof[key]['path']) != proof[key]['sha256']:
            raise ValueError('Deployment retirement evidence hash changed')
    current = Path(proof['source_cache']['path']).parent
    rebuilt = deployment_revocation_proof(current, proof['evidence']['path'], proof['capture']['path'],
                                          proof['upstream_url'])
    sealed_files = read(proof['source_cache']['path'])['files']
    if ({key: value for key, value in rebuilt.items() if key != 'request_files'} !=
            {key: value for key, value in proof.items() if key != 'request_files'} or
            any(sealed_files.get(key) != value for key, value in proof['request_files'].items()) or
            Path(proof['source']).resolve() != original):
        raise ValueError('Deployment retirement evidence is not bound to this original cache')


def _retirement_credit_path(current, proof, sid):
    current = Path(current)
    key = _canonical_hash([proof['source'], proof['evidence']['sha256'], sid])
    return current.parent / (current.name + '_COMPENSATION') / 'RETIREMENT_CREDITS' / (key + '.json')


def _deployment_compensation_plan(original, current, proof):
    """One record-level cohort credit follows its earliest remaining failure."""
    ledger = {row['id']: row for row in _journal_rows(current / 'DISPOSITIONS.jsonl') if
        row['name'].get('failure_kind') == 'transport' or
        (row.get('scope') or {}).get('failure_kind') == 'transport' or
        row.get('content_failure_kind') == 'transport'}
    sealed = read(proof['source_cache']['path'])
    old_manifest = read(original / 'MANIFEST.json')
    if (sealed['files'].get('MANIFEST.json') != digest(original / 'MANIFEST.json') or
            _semantic_identity(old_manifest) != _semantic_identity(read(current / 'MANIFEST.json'))):
        raise ValueError('Retirement compensation input/model/context/source identity changed')
    origins = {}
    for relative, checksum in proof['request_files'].items():
        path = _safe_relative(original, relative)
        if digest(path) != checksum:
            raise ValueError('Revocation request evidence differs from original cache')
        stage = relative.split('/')[0]
        if relative.endswith('_judgments.jsonl'):
            # Native error fields are small; do not decode megabytes of unrelated raw_content.
            with path.open('rb') as stream:
                for line in stream:
                    if not _HTTP404.search(line):
                        continue
                    sid = _line_id(line)
                    if sid not in ledger:
                        continue
                    status = re.search(rb'(?<!\\)"api_status"\s*:\s*"api_error_after_retries"', line)
                    error = re.search(rb'(?<!\\)"api_error"\s*:\s*("(?:\\.|[^"\\])*")', line)
                    if not status or not error or not _HTTP404.search(str(json.loads(error[1])).encode()):
                        continue
                    origins.setdefault(sid, []).append({'stage': stage, 'request_file': relative,
                        'sha256': checksum, 'phase': 'context' if 'context' in path.parent.name else 'initial'})
        elif relative.startswith('content/calls/') and path.stem in ledger:
            value = read(path)
            if (value.get('status') == 'failed' and value.get('round', 1) == 1 and
                    any(_HTTP404.search(str(row.get('error', '')).encode())
                        for row in value.get('attempts', []) if isinstance(row, dict))):
                origins.setdefault(path.stem, []).append({'stage': 'content', 'request_file': relative,
                    'sha256': checksum, 'substage': path.parent.name})
    indices, hashes, history_cache = {}, {}, {}
    def artifact(path, sid):
        path = Path(path)
        if path not in indices:
            indices[path] = _line_index(path)
        if sid not in indices[path]:
            return None
        with path.open('rb') as stream:
            return json.loads(_indexed_line(stream, indices[path], sid))
    def checksum(path):
        if path not in hashes:
            hashes[path] = digest(path)
        return hashes[path]
    def history(folder, stem):
        if folder not in history_cache:
            values = {}
            for path in folder.glob('round_*/**/' + stem + '_judgments.jsonl'):
                component = path.relative_to(folder).parts[0]
                number = int(re.match(r'round_(\d+)', component)[1])
                with path.open('rb') as stream:
                    for line in stream:
                        sid = _line_id(line)
                        if sid in origins:
                            value = json.loads(line)
                            values.setdefault(sid, []).append({'round': number,
                                'phase': 'context' if 'review_context' in component else 'initial',
                                'api_status': value.get('api_status'), 'failure_kind': value.get('failure_kind')})
            history_cache[folder] = values
        return history_cache[folder]
    def transport(vote):
        return vote.get('api_status') not in ('ok', 'local') and vote.get('failure_kind') == 'transport'
    eligible = []
    for sid, causes in sorted(origins.items()):
        if _retirement_credit_path(current, proof, sid).exists():
            continue
        disposition = ledger[sid]
        target = None
        if transport(disposition['name']):
            target = 'name'
        elif disposition['name'].get('decision') != 'keep' or disposition['name'].get('api_status') not in ('ok', 'local'):
            continue
        elif transport(disposition.get('scope') or {}):
            target = 'scope'
        elif (disposition.get('scope') or {}).get('decision') != 'keep' or (
                disposition.get('scope') or {}).get('api_status') not in ('ok', 'local'):
            continue
        elif disposition.get('content_status') == 'failed' and disposition.get('content_failure_kind') == 'transport':
            target = 'content'
        if target is None:
            continue
        order = {'name': 0, 'scope': 1, 'content': 2}
        causes = [cause for cause in causes if order[cause['stage']] <= order[target]]
        if not causes:
            continue
        cause = sorted(causes, key=lambda row: (order[row['stage']], row['request_file']))[0]
        common = {'id': sid, 'pipeline_stage': target, 'origin_request_file': cause['request_file'],
            'origin_stage': cause['stage'], 'original_sha256': cause['sha256'],
            'opportunities': 1, 'missing_opportunity': 1, 'maximum_effective_opportunities': 2,
            'execute_after_regular_rounds': True, 'credit_scope': 'one_record_one_retired_cohort'}
        if target in ('name', 'scope'):
            annotation = 'llm_name_title_format_quality' if target == 'name' else 'subject_scope_quality'
            stem = 'llm_name_title_format' if target == 'name' else 'subject_scope'
            folders = [current / 'name'] if target == 'name' else sorted((current / 'scope').glob('group_*'))
            folders = [folder for folder in folders if (folder / 'FINAL.jsonl').is_file() and
                       artifact(folder / 'FINAL.jsonl', sid) is not None]
            if len(folders) != 1:
                raise ValueError('Current failed filter has ambiguous scope/ID identity')
            folder = folders[0]
            first, final = artifact(folder / 'FIRST.jsonl', sid), artifact(folder / 'FINAL.jsonl', sid)
            if first is None or not transport(final[annotation]):
                continue
            vote = first[annotation]
            phase = 'context' if vote.get('decision') == 'review' and vote.get('api_status') in ('ok', 'local') else 'initial'
            chosen = final if phase == 'context' else first
            if not transport(chosen[annotation]):
                continue
            actual = [row for row in history(folder, stem).get(sid, []) if row['phase'] == phase]
            rounds = {row['round'] for row in actual if row['api_status'] not in ('ok', 'local')}
            if not rounds or min(rounds) <= 1 or max(rounds) > 3 or len(rounds) > 2:
                continue
            path = folder / ('FINAL.jsonl' if phase == 'context' else 'FIRST.jsonl')
            eligible.append({**common, 'stage': target, 'group': folder.relative_to(current).as_posix(),
                'vote_phase': phase, 'request_file': path.relative_to(current).as_posix(),
                'current_artifact': str(path), 'expected_hash': checksum(path),
                'row_sha256': _canonical_hash(chosen), 'history_round': max(rounds), 'actual_history': actual})
        else:
            paths = list((current / 'content/calls').glob('*/' + sid + '.json'))
            order_body = {'explanation': 0, 'definition_zh': 1, 'definition_en': 1, 'alignment': 2}
            paths.sort(key=lambda path: (order_body.get(path.parent.name, -1), path.parent.name))
            failed = [(path, read(path)) for path in paths if read(path).get('status') == 'failed']
            if not failed:
                continue
            path, value = failed[0]
            if (value.get('failure_kind') != 'transport' or value.get('round', 1) not in (2, 3) or
                    path.parent.name not in order_body):
                continue
            old_path = original / path.relative_to(current)
            if old_path.is_file():
                old = read(old_path)
                if old.get('status') == 'failed' and not any(_HTTP404.search(str(row.get('error', '')).encode())
                        for row in old.get('attempts', []) if isinstance(row, dict)):
                    continue  # This subrequest already had its original non-retired opportunity.
            eligible.append({**common, 'stage': path.parent.name,
                'request_file': path.relative_to(current).as_posix(), 'current_sha256': checksum(path),
                'packet': value['packet'], 'prompt': value['prompt'], 'history_round': value['round'],
                'actual_history': [{'round': value['round'], 'status': 'failed', 'failure_kind': 'transport'}],
                'history_is_complete': False})
    return {'schema': 1, 'purpose': 'evidenced_unregistered_404_lost_first_opportunity',
        'original': str(original), 'current': str(current), 'revocation': proof, 'eligible': eligible,
        'round_numbers_rewritten': False, 'inference_executed': False,
        'credit_policy': 'one_record_one_retired_cohort_at_current_earliest_transport_failure',
        'planned_stage_counts': {stage: sum(row['pipeline_stage'] == stage for row in eligible)
                                 for stage in ('name', 'scope', 'content')}, 'semantic_quality_approved': False}


def compensation_plan(original, current, revocation):
    """Plan one lost first-round opportunity only; this does not run inference."""
    original, current = Path(original).resolve(strict=True), Path(current).resolve(strict=True)
    if (revocation.get('schema') != 1 or not isinstance(revocation.get('request_files'), dict) or
            revocation.get('event') not in ('gateway_job_unregistered', 'service_deployment_deregistered')):
        raise ValueError('Specific deployment retirement/revocation evidence is required')
    if revocation['event'] == 'service_deployment_deregistered':
        _verify_deployment_proof(revocation, original)
        return _deployment_compensation_plan(original, current, revocation)
    elif not revocation.get('job_id'):
        raise ValueError('Specific gateway job revocation evidence is required')
    eligible, indices, hashes, histories = [], {}, {}, {}
    def artifact_hash(path):
        if path not in hashes:
            hashes[path] = digest(path)
        return hashes[path]

    def artifact_row(path, sid):
        if not path.exists():
            return None
        if path not in indices:
            indices[path] = _line_index(path)
        if sid not in indices[path]:
            return None
        with path.open('rb') as stream:
            return json.loads(_indexed_line(stream, indices[path], sid))

    def filter_history(folder, stem, wanted):
        cache_key = (folder, frozenset(wanted))
        if cache_key not in histories:
            found = {}
            for journal in folder.glob('round_*/**/' + stem + '_judgments.jsonl'):
                component = journal.relative_to(folder).parts[0]
                number = int(re.match(r'round_(\d+)', component)[1])
                with journal.open('rb') as stream:
                    for line in stream:
                        sid = _line_id(line)
                        if sid not in wanted:
                            continue
                        value = json.loads(line)
                        found.setdefault(value['id'], []).append({
                            'round': number, 'phase': 'context' if 'review_context' in component else 'initial',
                            'api_status': value.get('api_status'), 'failure_kind': value.get('failure_kind')})
            histories[cache_key] = found
        return histories[cache_key]

    for relative, checksum in revocation['request_files'].items():
        origin = _safe_relative(original, relative)
        latest = _safe_relative(current, relative)
        if not origin.is_file() or artifact_hash(origin) != checksum:
            raise ValueError('Revocation request evidence differs from original cache')
        if relative.startswith(('name/', 'scope/')) and relative.endswith('_judgments.jsonl'):
            stage = 'name' if relative.startswith('name/') else 'scope'
            annotation = 'llm_name_title_format_quality' if stage == 'name' else 'subject_scope_quality'
            stem = 'llm_name_title_format' if stage == 'name' else 'subject_scope'
            folder = Path(relative).parent.parent
            first = current / folder / 'FIRST.jsonl'
            terminal = current / folder / 'FINAL.jsonl'
            lost_ids = set()
            with origin.open('rb') as stream:
                for line in stream:
                    if not _HTTP404.search(line):
                        continue
                    lost = json.loads(line)
                    if lost.get('api_status') != 'api_error_after_retries' or not _HTTP404.search(
                            str(lost.get('api_error', '')).encode()):
                        continue
                    lost_ids.add(lost['id'])
            found_history = filter_history(current / folder, stem, lost_ids)
            for sid in sorted(lost_ids):
                value, final = artifact_row(first, sid), artifact_row(terminal, sid)
                if value is None or final is None:
                    continue
                vote, last_vote = value.get(annotation, {}), final.get(annotation, {})
                phase = ('context' if vote.get('decision') == 'review' and
                         vote.get('api_status') in ('ok', 'local') else 'initial')
                full = terminal if phase == 'context' else first
                selected = final if phase == 'context' else value
                history = found_history.get(sid, [])
                attempted = [row for row in history if row['round'] > 1 and row['phase'] == phase
                             and row['failure_kind'] == 'transport' and row['api_status'] not in ('ok', 'local')]
                if (not attempted or selected[annotation].get('failure_kind') != 'transport' or
                        last_vote.get('failure_kind') != 'transport' or last_vote.get('api_status') in ('ok', 'local')):
                    continue
                if any(row.get('group') == folder.as_posix() and row['id'] == sid for row in eligible):
                    continue
                eligible.append({'id': sid, 'stage': stage, 'pipeline_stage': stage, 'vote_phase': phase,
                    'origin_vote_phase': 'context' if 'context' in origin.parent.name else 'initial',
                    'group': folder.as_posix(), 'request_file': relative,
                    'original_sha256': checksum, 'expected_hash': artifact_hash(full),
                    'current_artifact': str(full), 'row_sha256': _canonical_hash(selected),
                    'history_round': max(row['round'] for row in history), 'actual_history': history,
                    'opportunities': 1, 'missing_opportunity': 1, 'execute_after_regular_rounds': True})
            continue
        if not relative.startswith('content/calls/') or not relative.endswith('.json'):
            raise ValueError('Revocation proof must bind a native filter judgment or content request')
        if not latest.is_file():
            continue
        old, new = read(origin), read(latest)
        final = current / 'content/results' / origin.name
        if final.exists() and read(final).get('status') != 'failed':
            continue
        if (old.get('status') != 'failed' or old.get('round', 1) != 1 or
                not any(re.search(r'(?:HTTPError\s+404\b|HTTPError\(code=404\b)', str(row.get('error', '')))
                        for row in old.get('attempts', []) if isinstance(row, dict)) or
                new.get('status') != 'failed' or new.get('failure_kind') != 'transport' or
                new.get('round', 1) <= 1 or new.get('packet') != old.get('packet') or
                new.get('prompt') != old.get('prompt')):
            continue
        eligible.append({'id': origin.stem, 'stage': origin.parent.name, 'request_file': relative,
            'original_sha256': checksum, 'current_sha256': digest(latest),
            'pipeline_stage': 'content', 'history_round': new['round'], 'opportunities': 1,
            'missing_opportunity': 1, 'execute_after_regular_rounds': True,
            'packet': new['packet'], 'prompt': new['prompt']})
    return {'schema': 1, 'purpose': 'evidenced_unregistered_404_lost_first_opportunity',
            'original': str(original), 'current': str(current), 'revocation': revocation,
            'eligible': eligible, 'round_numbers_rewritten': False, 'inference_executed': False,
            'planned_stage_counts': {stage: sum(row['pipeline_stage'] == stage for row in eligible)
                                     for stage in ('name', 'scope', 'content')},
            'semantic_quality_approved': False}


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


_compensation_lock = threading.RLock()
_compensation_plans = {}
_compensation_artifacts = set()


def load_compensation_plan(token):
    path = Path(token.get('path', '')).resolve(strict=True)
    marker = path.stat()
    key = (str(path), token.get('sha256'), marker.st_mtime_ns, marker.st_size)
    with _compensation_lock:
        if key not in _compensation_plans:
            if digest(path) != token.get('sha256'):
                raise ValueError('Compensation plan token hash differs')
            value = read(path)
            if value.get('purpose') != 'evidenced_unregistered_404_lost_first_opportunity':
                raise ValueError('Compensation plan purpose differs')
            _compensation_plans[key] = value
        return _compensation_plans[key]


def _claim_path(token, row, suffix=''):
    plan = load_compensation_plan(token)
    if not suffix and plan['revocation'].get('event') == 'service_deployment_deregistered':
        return _retirement_credit_path(plan['current'], plan['revocation'], row['id'])
    key = _canonical_hash([token['sha256'], row['pipeline_stage'], row.get('group'),
                           row['id'], row['stage'], row.get('vote_phase'), suffix])
    return Path(token['path']).parent / 'COMPENSATION_CLAIMS' / (key + '.json')


def _consume_compensation(token, row, suffix=''):
    claim = _claim_path(token, row, suffix)
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        with claim.open('x', encoding='utf-8') as stream:
            json.dump({'plan_sha256': token['sha256'], 'request_file': row['request_file'],
                'id': row['id'], 'stage': row['stage'], 'vote_phase': row.get('vote_phase'),
                'history_round': row['history_round'], 'opportunities_consumed': 0 if suffix else 1,
                'first_downstream_request': suffix or None, 'claimed_before_request': True}, stream)
    except FileExistsError:
        return False
    return True


def _verify_compensation_artifact(path, checksum):
    path = Path(path)
    marker = path.stat()
    key = (str(path), checksum, marker.st_mtime_ns, marker.st_size)
    with _compensation_lock:
        if key not in _compensation_artifacts:
            if digest(path) != checksum:
                raise ValueError('Compensation eligibility changed since plan')
            _compensation_artifacts.add(key)


def claim_compensation(config, sid, stage, saved):
    """Consume a sealed plan permission once, before a specific failed API call."""
    token = config.get('compensation_token')
    if sid not in config.get('compensation_ids', ()) or not isinstance(token, dict):
        return False
    plan = load_compensation_plan(token)
    matches = [row for row in plan['eligible'] if row['id'] == sid and row['stage'] == stage
               and row['pipeline_stage'] == 'content']
    if not matches:
        return False
    row = matches[0]
    current = _safe_relative(plan['current'], row['request_file'])
    if (saved.get('status') != 'failed' or saved.get('failure_kind') != 'transport' or
            saved.get('packet') != row['packet'] or saved.get('prompt') != row['prompt'] or
            saved.get('round') != row['history_round'] or digest(current) != row['current_sha256']):
        raise ValueError('Compensation eligibility changed since plan')
    return _consume_compensation(token, row)


def compensation_filter_eligible(config, stage, group, sid, phase):
    plan = load_compensation_plan(config['compensation_token'])
    return any(row['pipeline_stage'] == stage and row.get('group') == group and row['id'] == sid
               and row.get('vote_phase') == phase for row in plan['eligible'])


def claim_filter_compensation(config, stage, group, value, phase):
    token = config['compensation_token']
    plan = load_compensation_plan(token)
    matches = [row for row in plan['eligible'] if row['pipeline_stage'] == stage and
               row.get('group') == group and row['id'] == value['id'] and row.get('vote_phase') == phase]
    if not matches:
        return claim_first_downstream(config, value['id'], stage, group + '/' + phase)
    row = matches[0]
    if _canonical_hash(value) != row['row_sha256']:
        raise ValueError('Filter compensation eligibility changed since plan')
    _verify_compensation_artifact(row['current_artifact'], row['expected_hash'])
    return _consume_compensation(token, row)


def claim_first_downstream(config, sid, stage, request_key):
    """Authorize a first visit made possible by an actually consumed upstream credit."""
    token = config['compensation_token']
    plan = load_compensation_plan(token)
    order = {'name': 0, 'scope': 1, 'content': 2}
    body_order = {'explanation': 0, 'definition_zh': 1, 'definition_en': 1, 'alignment': 2}
    candidates = [row for row in plan['eligible'] if row['id'] == sid and
        (order[row['pipeline_stage']] < order[stage] or row['pipeline_stage'] == stage and
         (row.get('vote_phase') == 'initial' and request_key.endswith('/context') or
          stage == 'content' and request_key in body_order and row['stage'] in body_order and
          (body_order[row['stage']] < body_order[request_key] or
           row['stage'].startswith('definition_') and request_key.startswith('definition_')))) and
        _claim_path(token, row).is_file() and read(_claim_path(token, row)).get('plan_sha256') == token['sha256']]
    return bool(candidates) and _consume_compensation(token, candidates[0], stage + ':' + request_key)


def compensation_body_eligible(config, sid):
    plan = load_compensation_plan(config['compensation_token'])
    return any(row['id'] == sid and row['pipeline_stage'] == 'content' for row in plan['eligible'])
