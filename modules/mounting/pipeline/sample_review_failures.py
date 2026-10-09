import json,sys
from pathlib import Path
d=Path(sys.argv[1]);inputs={r['request_id']:r for r in (json.loads(x) for x in (d/'input.jsonl').read_text().splitlines() if x.strip())}
shown=0
for line in (d/'responses.jsonl').open():
    r=json.loads(line)
    if r['final']['judgment']!='technical_failure':continue
    item=inputs[r['request_id']]
    first=r['first']['attempts'][0]
    print('\n',r['request_id'],'error',first.get('error'))
    raw=first.get('raw') or {}
    content=(raw.get('choices') or [{}])[0].get('message',{}).get('content','')
    print('model_response',content[:800])
    print('name',str(item.get('name'))[:100],'en',str(item.get('knowledge_point'))[:100],'path',str(item.get('main_tags'))[:150])
    shown+=1
    if shown>=3:break
