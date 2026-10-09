"""Read-only token-usage audit of boundary group checkpoints."""
import json,sys,statistics
from pathlib import Path
for directory in map(Path,sys.argv[1:]):
    rows=[]
    for path in (directory/'groups').glob('*.json'):
        try: item=json.loads(path.read_text())
        except Exception:continue
        out=item.get('output',{})
        for round_ in out.get('rounds',[]):
            for kind in ('generation','review'):
                call=round_.get(kind) or {}
                for attempt in call.get('attempts',[]):
                    raw=attempt.get('raw') or {}
                    usage=raw.get('usage') or {}
                    if usage:
                        rows.append(dict(kind=kind,status=out.get('status'),cards=len(item.get('payload',{}).get('targets',[])),prompt=usage.get('prompt_tokens',0),completion=usage.get('completion_tokens',0),finish=(raw.get('choices') or [{}])[0].get('finish_reason'),path=path.name))
    print('\n',directory,'groups',len(list((directory/'groups').glob('*.json'))),'usage_records',len(rows))
    for kind in ('generation','review'):
        rr=[r for r in rows if r['kind']==kind]
        if not rr:continue
        ordered=sorted(r['completion'] for r in rr)
        print(kind,'count',len(rr),'cards_range',[min(r['cards'] for r in rr),max(r['cards'] for r in rr)],
              'completion_p50',ordered[len(ordered)//2],'p95',ordered[int(.95*(len(ordered)-1))],
              'max',ordered[-1],'prompt_max',max(r['prompt'] for r in rr),
              'non_stop',sum(r['finish']!='stop' for r in rr))
        print('largest',sorted(rr,key=lambda r:r['completion'],reverse=True)[:5])
