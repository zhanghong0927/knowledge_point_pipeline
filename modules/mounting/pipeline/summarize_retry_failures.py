"""Read-only classification of latest failed retry responses."""
import json
import sys
from collections import Counter
from pathlib import Path

for arg in sys.argv[1:]:
    folder=Path(arg)
    rows={}
    for line in (folder/'responses.jsonl').open():
        row=json.loads(line)
        rows[row['request_id']]=row
    failed=[r for r in rows.values() if r['final']['judgment']=='technical_failure']
    kinds=Counter()
    for row in failed:
        for name in ('first','second'):
            call=row.get(name)
            if call is None:continue
            if call.get('result') is None:
                for attempt in call['attempts']:
                    kinds[(name,attempt.get('error') or 'no error recorded')]+=1
    print(json.dumps(dict(stage=folder.parent.name+'/'+folder.name,latest=len(rows),technical=len(failed),
                          error_attempts=[dict(pass_name=k[0],error=k[1],count=v) for k,v in kinds.most_common()]),ensure_ascii=False))
