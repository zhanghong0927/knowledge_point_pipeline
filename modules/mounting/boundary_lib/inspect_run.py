import json
from pathlib import Path
import sys
from collections import Counter

base=Path(sys.argv[1]);counts=Counter();cards=0
for file in (base/'groups').glob('*.json'):
    g=json.loads(file.read_text(encoding='utf-8'));o=g['output'];counts[o['status']]+=1;cards+=len(o.get('cards',[]))
    if o['status']=='technical_failure':
        print(json.dumps(dict(file=str(file),targets=[n['name_zh'] for n in g['payload']['targets']],errors=[a.get('error') for r in o.get('rounds',[]) for v in r.values() for a in v.get('attempts',[]) if a.get('error')]),ensure_ascii=False))
print(json.dumps(dict(cards=cards,groups=counts),ensure_ascii=False))
