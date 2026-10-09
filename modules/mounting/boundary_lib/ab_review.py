"""Blind path-only quality review; no generated semantic cards enter the reviewer."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
import sys
from ab_workflow import KEEP,BASE,MODEL,write_json,write_lines

def prepare_reviews(inputs,outputs,tree):
    inputs_by={x['record_id']:x for x in inputs}
    if len(inputs_by)!=len(inputs):raise ValueError('duplicate input ID')
    by={}
    def walk(n,chain):
        by[n['code']]=dict(node=n,chain=chain+[n])
        for c in n.get('children',[]):walk(c,chain+[n])
    walk(tree,[])
    items={};links=[]
    for variant,rows in outputs.items():
        if len(rows)!=len(inputs) or {x['record_id'] for x in rows}!=set(inputs_by):raise ValueError('incomplete/duplicate mapping outputs')
        for row in rows:
            rid=row['record_id'];label=row['knowledge_labeling'];codes=label.get('best_path_codes',[])
            link=dict(record_id=rid,variant=variant,decision=label.get('decision'),status=label.get('status'),path_codes=codes,request_id=None)
            if label.get('status')=='ok' and label.get('decision') in ('accepted_leaf','accepted_parent'):
                if not codes or codes[-1] not in by:raise ValueError('unknown mounted node')
                chain=by[codes[-1]]['chain']
                if [n['code'] for n in chain[1:]]!=codes:raise ValueError('invalid path chain')
                key=hashlib.sha256((rid+'|'+json.dumps(codes)).encode()).hexdigest()[:24]
                item={k:inputs_by[rid][k] for k in KEEP if k in inputs_by[rid]}
                item.update(request_id=key,main_tags='/'.join(n['name_zh'] for n in chain),taxonomy_context=dict(exact_path_exists=True,path_names=[n['name_zh'] for n in chain],children_names=[n['name_zh'] for n in by[codes[-1]]['node'].get('children',[])]))
                items[key]=item;link['request_id']=key;link['path']=item['main_tags']
            links.append(link)
    return [items[k] for k in sorted(items)],links

def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--review-lib',type=Path,required=True);p.add_argument('--workers',type=int,default=8);a=p.parse_args()
    sys.path.insert(0,str(a.review_lib));import mount_pilot_v3 as reviewer
    def lines(path):return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
    inputs=lines(a.run/'input.jsonl');outputs={v:lines(a.run/(v+'.jsonl')) for v in ['with_cards','without_cards']}
    tree=json.loads((a.run/'profiles'/'with_cards'/'data'/'knowledge_tree.json').read_text(encoding='utf-8'))
    items,links=prepare_reviews(inputs,outputs,tree);out=a.run/'blind_review';out.mkdir(exist_ok=False)
    write_lines(out/'input.jsonl',items);write_json(out/'arm_links.json',links)
    write_json(out/'config.json',dict(model=MODEL,base=BASE,workers=a.workers,prompt_sha256=hashlib.sha256((reviewer.PROMPT+reviewer.COUNTER_PROMPT).encode()).hexdigest(),blind_to_variant=True,semantic_cards_in_review=False))
    (out/'prompt.txt').write_text(reviewer.PROMPT,encoding='utf-8');(out/'counter_prompt.txt').write_text(reviewer.COUNTER_PROMPT,encoding='utf-8')
    results={}
    with (out/'responses.jsonl').open('w',encoding='utf-8') as f,ThreadPoolExecutor(max_workers=a.workers) as pool:
        tasks=[pool.submit(reviewer.review_one,BASE,MODEL,item) for item in items]
        for future in as_completed(tasks):
            result=future.result();results[result['request_id']]=result
            f.write(json.dumps(result,ensure_ascii=False)+'\n');f.flush()
            if len(results)%20==0:print('review',len(results),'/',len(items),flush=True)
    table={}
    for variant,rows in outputs.items():
        selected=[x for x in links if x['variant']==variant]
        judged=[results[x['request_id']]['final']['judgment'] for x in selected if x['request_id']]
        table[variant]=dict(total=len(rows),decisions=dict(Counter(x['knowledge_labeling']['decision'] for x in rows)),accepted=len(judged),quality=dict(Counter(judged)),model_reasonable_rate_all=judged.count('reasonable')/len(rows),model_reasonable_rate_accepted=judged.count('reasonable')/len(judged) if judged else None,expert_audited=False)
    paired=[]
    for record in inputs:
        pair=dict(record=record)
        for variant in outputs:
            link=next(x for x in links if x['record_id']==record['record_id'] and x['variant']==variant)
            pair[variant]=dict(link,review=results[link['request_id']]['final'] if link['request_id'] else None)
        paired.append(pair)
    write_json(out/'summary.json',table);write_lines(out/'paired_results.jsonl',paired)
    print(json.dumps(table,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
