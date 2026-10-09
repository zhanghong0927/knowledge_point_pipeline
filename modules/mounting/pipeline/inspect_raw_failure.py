import json,sys
from pathlib import Path
folder=Path(sys.argv[1]);rid=sys.argv[2]
latest={}
for row in map(json.loads,(folder/'responses.jsonl').open()):latest[row['request_id']]=row
row=latest[rid]
for name in ('first','second'):
    call=row.get(name)
    if not call:continue
    for i,attempt in enumerate(call['attempts']):
        raw=attempt.get('raw') or {}
        choice=(raw.get('choices') or [{}])[0]
        content=(choice.get('message') or {}).get('content') or ''
        print(name,i,'error',attempt.get('error'),'finish',choice.get('finish_reason'),'length',len(content),
              'head',content[:220],'tail',content[-150:])
