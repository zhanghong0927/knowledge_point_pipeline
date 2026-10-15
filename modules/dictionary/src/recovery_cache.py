"""Read immutable source cache with its sealed per-file provenance."""
import functools
import hashlib
import json
from pathlib import Path
import re

@functools.lru_cache(maxsize=64)
def provenance(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def read_source(relative, cfg):
    if not cfg.get('recovery_source'):
        return None
    root = Path(cfg['recovery_source']).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('Recovery cache path escapes source')
    p = root / rel
    if not p.exists():
        return None
    proof = provenance(cfg['recovery_provenance'])
    if Path(proof['source']).resolve() != root:
        raise ValueError('Recovery provenance names a different source')
    expected = proof['files'].get(rel.as_posix())
    data = p.read_bytes()
    if not expected or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Original cache changed: ' + rel.as_posix())
    return json.loads(data.decode('utf-8-sig'))

def expired_404(value):
    return value.get('status') == 'failed' and any(
        isinstance(a,dict) and re.search(r'(?:<HTTPError\s+404:|HTTPError\(code=404\b)',str(a.get('error','')))
        for a in value.get('attempts',[]))

def source_calls(sid, cfg):
    if not cfg.get('recovery_source'):
        return []
    root = Path(cfg['recovery_source'])
    return [read_source(str(p.relative_to(root)),cfg)
            for p in (root/'content/calls').glob('*/'+sid+'.json')]

def source_retryable(sid, cfg):
    return any(v and (v.get('failure_kind') == 'transport' or expired_404(v))
               for v in source_calls(sid,cfg))
