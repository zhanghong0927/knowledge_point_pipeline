"""Read-only comparison of model evidence against exact input fields."""
import json,sys,re
from pathlib import Path
for name in sys.argv[1:]:
    d=Path(name)
    items={r['request_id']:r for r in (json.loads(x) for x in (d/'input.jsonl').read_text().splitlines() if x.strip())}
    latest={}
    for line in (d/'responses.jsonl').open():
        r=json.loads(line);latest[r['request_id']]=r
    print('\nSTAGE',d.name,'requests',len(latest))
    for rid,r in latest.items():
        if r['final']['judgment']!='technical_failure':continue
        item=items[rid]
        attempt=next((a for a in r['first']['attempts'] if a.get('raw')),None)
        if not attempt:print(rid,'no raw response');continue
        content=attempt['raw']['choices'][0]['message']['content']
        try:model=json.loads(content)
        except Exception as exc:print(rid,'json error',str(exc));continue
        wrong=[];valid=[]
        for key in ('support_evidence','conflict_evidence'):
            for e in model.get(key,[]):
                field=e.get('field');text=e.get('text');raw=item.get(field)
                if not isinstance(raw,str) or not isinstance(text,str):wrong.append((field,str(text)[:60],'field/type mismatch'));continue
                if text in raw:valid.append(field);continue
                compact=lambda s:re.sub(r'\s+','',s)
                reason='whitespace difference' if compact(text) in compact(raw) else 'not a source substring'
                wrong.append((field,text[:70],reason,raw[:110]))
        print(rid,'judgment',model.get('judgment'),'valid',valid,'invalid',wrong)
        print('  attempts',[(a.get('error'), (a.get('raw') or {}).get('choices',[{}])[0].get('finish_reason')) for a in r['first']['attempts']])
        if r.get('second'):
            print('  second',r['second'].get('result'),[(a.get('error'), (a.get('raw') or {}).get('choices',[{}])[0].get('finish_reason')) for a in r['second']['attempts']])
