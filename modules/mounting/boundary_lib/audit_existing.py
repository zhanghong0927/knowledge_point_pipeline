"""Run the v2 single-node cross-layer audit on frozen candidate cards."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import generate_semantic_boundaries as m

p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--out',required=True);p.add_argument('--name');p.add_argument('--workers',type=int,default=8);p.add_argument('--reuse-audit')
a=p.parse_args();run=Path(a.run);out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
nodes=json.loads((run/'normalized_nodes.json').read_text(encoding='utf-8'))
raw=(run/'semantic_cards.jsonl').read_bytes()
cards={c['node_code']:c for c in map(json.loads,raw.decode('utf-8').splitlines())}
config=json.loads((run/'config.json').read_text(encoding='utf-8'))
m.atomic_json(out/'input.json',dict(run=str(run),cards_sha256=hashlib.sha256(raw).hexdigest(),script_sha256=hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()))
(out/'cross_prompt.txt').write_text(m.PAIR_PROMPT,encoding='utf-8')
if a.name:
    n=next(n for n in nodes if n['name_zh']==a.name)
    payload=m.single_cross_payload(nodes,n['code'],cards)
    result=m.call(config['base'],config['model'],m.PAIR_PROMPT,payload,lambda x:m.validate_pair_review(x,payload))
    m.atomic_json(out/'regression.json',dict(payload=payload,output=result))
    print(json.dumps(result['result'],ensure_ascii=False),flush=True)
else:
    if a.reuse_audit:
        previous=Path(a.reuse_audit)
        if (previous.parent/'cross_prompt.txt').read_text(encoding='utf-8')!=m.PAIR_PROMPT:raise ValueError('audit prompt changed')
        shutil.copytree(previous,out/'checks')
    keys,results=m.cross_audit(nodes,cards,config['base'],config['model'],a.workers,500000,out/'checks')
    source_summary=json.loads((run/'summary.json').read_text(encoding='utf-8'))
    passed=m.release_gate(source_summary['selected_nodes'],len(cards),len(keys),results)
    issues=[i for r in results if r['result'] for i in r['result']['issues']]
    summary=dict(selected=source_summary['selected_nodes'],generated=len(cards),cross_checked_nodes=len(keys),pass_nodes=sum(bool(r['result']) and r['result']['verdict']=='pass' for r in results),conflict_nodes=sum(bool(r['result']) and r['result']['verdict']=='needs_revision' for r in results),technical_failures=sum(r['result'] is None for r in results),issues=len(issues),release=passed,expert_approved=False)
    m.atomic_json(out/'summary.json',summary);m.atomic_json(out/'issues.json',issues)
    (out/'cross_validated_cards.jsonl').write_bytes(raw if passed else b'')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
