"""Small, fail-closed checkpoints for explicit screening transport cohorts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import urllib.error
import socket
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from round_integrity import digest_file


TRANSPORT_ERRORS = (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, ConnectionError)


def transient_transport(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                        socket.timeout, ConnectionResetError, BrokenPipeError)):
        return True
    return False


def identity(paths, options, *, force_full=False):
    force_full = force_full or os.environ.get('STAGED_SCREENING_FINALIZE') == '1'
    helper = Path(__file__).resolve()
    sources = dict.fromkeys([*(Path(path).resolve() for path in paths), helper,
                             helper.parents[3] / 'round_integrity.py'])
    digest = hashlib.sha256()
    for path in sources:
        digest.update(str(path).encode())
        digest.update(bytes.fromhex(digest_file(path, force_full=force_full)))
    digest.update(json.dumps(options, sort_keys=True, ensure_ascii=False,
                             default=str).encode())
    return digest.hexdigest()


def checkpoint_hashes(paths, *, force_full=False):
    return {str(Path(path).resolve()): digest_file(path, force_full=force_full)
            for path in paths if Path(path).is_file()}


def require_identity(path, value, round_number, checkpoints=()):
    path = Path(path)
    if path.exists():
        prior = json.loads(path.read_text(encoding='utf-8'))
        if prior.get('identity') != value:
            raise ValueError('Staged screening input, source, model or policy changed')
        if prior.get('checkpoint_sha256') != checkpoint_hashes(checkpoints, force_full=round_number is None):
            raise ValueError('Staged screening checkpoint changed')
        if round_number is not None and prior.get('next_round') != round_number:
            raise ValueError('Staged screening round is out of order')
    elif round_number not in (None, 1):
        raise ValueError('Staged screening first round is missing')
    elif round_number == 1 and any(Path(item).is_file() and Path(item).stat().st_size
                                   for item in checkpoints):
        raise ValueError('Staged screening cannot adopt an unbound checkpoint')
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save_status(path, value, round_number, pending, *, terminal=0, checkpoints=()):
    state = ('awaiting_retry' if pending and round_number < 3
             else 'technical_failure' if pending or terminal else 'ready_to_finalize')
    payload = {'identity': value, 'state': state, 'status': state,
               'completed_round': round_number, 'next_round': round_number + 1
               if state == 'awaiting_retry' else None,
               'pending_transport': pending, 'terminal_failures': terminal,
               'checkpoint_sha256': checkpoint_hashes(checkpoints)}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def latest_jsonl(path, key):
    values = {}
    path = Path(path)
    if path.exists():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    item = json.loads(line)
                    values[item[key]] = item
    return values
