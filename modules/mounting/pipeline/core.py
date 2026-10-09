"""Contracts implemented under focused regression tests."""
import json,hashlib
def load_records(path):
    text=path.read_text(encoding='utf-8-sig')
    if text.lstrip().startswith('['):data=json.loads(text)
    else:data=[json.loads(s) for s in text.splitlines() if s.strip()]
    if not isinstance(data,list) or any(not isinstance(r,dict) for r in data):raise ValueError('all records must be objects')
    return data
def needs_mount(judgment):return judgment in ('unreasonable','uncertain')
def verify_ids(rows,expected,field):
    ids=[r[field] for r in rows]
    if len(ids)!=len(expected) or set(ids)!=set(expected):raise ValueError('missing, duplicate or unexpected IDs')
def content_key(item):return hashlib.sha256(json.dumps({k:v for k,v in item.items() if k!='request_id'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def require_equal(saved,current,label):
    if saved!=current:raise ValueError(label+' changed; use a new run directory')
