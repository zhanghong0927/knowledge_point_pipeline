"""Controller-lifetime file digests; persisted production checkpoints stay authoritative.

Only a live cohort on POSIX may reuse a verified digest. The local index is
disposable, authenticated, and never used for a new controller or final audit.
This saves content reads, not the directory/file-set and identity checks.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading

_ENV = 'PIPELINE_ROUND_INTEGRITY'
_FULL = ContextVar('full_round_verification', default=False)
_TLS = threading.local()
_CONNECTIONS = []
_REGISTRY_LOCK = threading.Lock()
_SESSION_LOCK = threading.RLock()


def _process_start(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')')+2:].split()
        return fields[19] if fields[0] not in ('Z', 'X') else None
    except (OSError, IndexError):
        return None


def _session():
    if os.name != 'posix' or _FULL.get():
        return None
    try:
        token = json.loads(os.environ.get(_ENV, 'null'))
        if (not isinstance(token, dict) or token.get('start') is None or
                _process_start(token.get('pid')) != token['start'] or
                len(bytes.fromhex(token['key'])) != 32):
            return None
        db = Path(token['db'])
        if (not db.is_absolute() or not db.parent.name.startswith('pipeline-round-integrity-') or
                db.parent.resolve().parent != Path('/tmp').resolve() or
                not db.is_file()):
            return None
        return token
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _fingerprint(path):
    st = path.stat()
    values = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    # Coarse timestamps cannot support unchanged-file reuse. Re-read instead.
    precise = bool(st.st_ino and st.st_mtime_ns % 1_000_000_000 and st.st_ctime_ns % 1_000_000_000)
    return json.dumps(values, separators=(',', ':')), precise


def _read_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _signature(token, path, fingerprint, digest):
    payload = json.dumps([path, fingerprint, digest], separators=(',', ':')).encode()
    return hmac.new(bytes.fromhex(token['key']), payload, hashlib.sha256).hexdigest()


def _connection(token):
    key = (os.getpid(), token['db'], token['key'])
    prior = getattr(_TLS, 'connection', None)
    if prior and prior[0] == key:
        return prior[1]
    db = sqlite3.connect(f"file:{token['db']}?mode=rw", uri=True, timeout=2,
                         isolation_level=None, check_same_thread=False)
    db.execute('PRAGMA synchronous=NORMAL')
    _TLS.connection = (key, db)
    with _REGISTRY_LOCK:
        _CONNECTIONS.append((token['db'], db))
    return db


def digest_file(path, *, force_full=False):
    path = Path(path).resolve(strict=True)
    before, precise = _fingerprint(path)
    token = None if force_full or not precise else _session()
    db = None
    if token:
        try:
            db = _connection(token)
            row = db.execute('SELECT fingerprint,digest,signature FROM digests WHERE path=?',
                             (str(path),)).fetchone()
            if (row and row[0] == before and len(row[1]) == 64 and
                    hmac.compare_digest(row[2], _signature(token, str(path), row[0], row[1]))):
                return row[1]
        except (sqlite3.Error, TypeError, ValueError):
            # A cache is an optimization; a missing/corrupt/busy index never grants trust.
            db = None
    value = _read_digest(path)
    after, _ = _fingerprint(path)
    if before != after:
        raise ValueError('File changed during checkpoint hashing: ' + str(path))
    if db is not None:
        try:
            db.execute('INSERT OR REPLACE INTO digests VALUES(?,?,?,?)',
                       (str(path), after, value, _signature(token, str(path), after, value)))
        except sqlite3.Error:
            pass
    return value


@contextmanager
def verification_session():
    """Scope an index to one joined cohort; finalization runs outside this scope."""
    if os.name != 'posix':
        yield
        return
    with _SESSION_LOCK:
        if _session() is not None:
            yield
            return
        previous = os.environ.get(_ENV)
        with tempfile.TemporaryDirectory(prefix='pipeline-round-integrity-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            if root.parent != Path('/tmp').resolve():
                raise ValueError('Integrity cache must stay in the local temporary directory')
            db_path = root/'digests.sqlite3'
            with sqlite3.connect(db_path) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE digests(path TEXT PRIMARY KEY,fingerprint TEXT,digest TEXT,signature TEXT)')
            token = {'db': str(db_path), 'pid': os.getpid(), 'start': _process_start(os.getpid()),
                     'key': secrets.token_hex(32)}
            os.environ[_ENV] = json.dumps(token, separators=(',', ':'))
            try:
                yield
            finally:
                if previous is None:
                    os.environ.pop(_ENV, None)
                else:
                    os.environ[_ENV] = previous
                with _REGISTRY_LOCK:
                    owned = [item for item in _CONNECTIONS if item[0] == str(db_path)]
                    _CONNECTIONS[:] = [item for item in _CONNECTIONS if item[0] != str(db_path)]
                for _, db in owned:
                    db.close()
                _TLS.connection = None


def with_verification_session(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with verification_session():
            return fn(*args, **kwargs)
    return wrapped


def full_verification(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _FULL.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _FULL.reset(token)
    return wrapped
