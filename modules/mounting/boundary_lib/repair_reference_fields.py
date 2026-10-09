"""Preserve known cross-branch text but place it in the correct schema field."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import generate_semantic_boundaries as m

def fix_references(obj,targets,names):
    fixed=copy.deepcopy(obj);allowed={n['code'] for n in targets};changes=[]
    for card in fixed['cards']:
        kept=[]
        for ref in card['sibling_distinctions']:
            if ref['other_code'] in allowed:kept.append(ref);continue
            if ref['other_code'] not in names:raise ValueError('unknown cross reference; cannot repair')
            rule='与'+names[ref['other_code']]+'：'+ref['rule']
            card['cross_boundary_rule']+='；'+rule
            changes.append(dict(node_code=card['node_code'],moved_reference=ref,target_name=names[ref['other_code']]))
        card['sibling_distinctions']=kept
    return fixed,changes

def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    for file in ['source_tree.json','normalized_nodes.json','generation_prompt.txt','review_prompt.txt']:shutil.copy2(a.run/file,a.out/file)
    shutil.copytree(a.run/'groups',a.out/'groups')
    nodes=json.loads((a.run/'normalized_nodes.json').read_text(encoding='utf-8'));names={n['code']:n['name_zh'] for n in nodes};report=[]
    for f in (a.out/'groups').glob('*.json'):
        g=json.loads(f.read_text(encoding='utf-8'));o=g['output']
        if o['status']!='technical_failure':continue
        try:
            raw=o['rounds'][-1]['generation']['attempts'][-1]['raw'];choice=raw['choices'][0]
            if choice['finish_reason']!='stop':raise ValueError('truncated completion')
            text=choice['message']['content'].strip()
            if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0]
            fixed,changes=fix_references(json.loads(text),g['payload']['targets'],names)
            if not changes:raise ValueError('no recognized cross-field reference to fix')
            cards=m.validate_cards(fixed,g['payload']['targets'])
            g['output']=dict(o,status='advisory_candidate',original_status='technical_failure',cards=cards,field_repairs=changes,review_performed_after_repair=False,reason='known cross-branch rules moved from sibling_distinctions to cross_boundary_rule; no text deleted')
            m.atomic_json(f,g);report.append(dict(file=f.name,repaired_nodes=len(cards),changes=changes))
        except Exception as e:report.append(dict(file=f.name,not_repaired=str(e)))
    m.atomic_json(a.out/'field_repair_report.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
