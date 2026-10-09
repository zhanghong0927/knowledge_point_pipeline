"""Check whether the new cross-layer auditor detects the known v1 conflict."""
import json
from pathlib import Path
import sys
import generate_semantic_boundaries_v2 as m

old=Path(sys.argv[1]);out=Path(sys.argv[2])
nodes=json.loads((old/'normalized_nodes.json').read_text(encoding='utf-8'))
cards={c['node_code']:c for c in map(json.loads,(old/'semantic_cards.jsonl').read_text(encoding='utf-8').splitlines())}
parent=next(n['code'] for n in nodes if n['name_zh']=='建筑学' and n['depth']==1)
payload=m.cross_payload(nodes,parent,cards)
config=json.loads((old/'config.json').read_text(encoding='utf-8'))
result=m.call(config['base'],config['model'],m.CROSS_PROMPT,payload,lambda x:m.validate_cross_review(x,payload))
out.parent.mkdir(parents=True,exist_ok=True)
m.atomic_json(out,dict(payload=payload,output=result))
print(json.dumps(result['result'],ensure_ascii=False),flush=True)
