"""Fresh model review of every accepted A/B mount; preserve unknown denominators."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
import sys
from ab_workflow import BASE,MODEL,write_json,write_lines
from ab_review import prepare_reviews

def metrics(verdicts):
    c=Counter(verdicts);n=len(verdicts);decided=c['reasonable']+c['unreasonable']
    return dict(total=n,counts=dict(c),reasonable_rate_all=c['reasonable']/n if n else None,
                reasonable_rate_decided=c['reasonable']/decided if decided else None)

def main():
    p=argparse.ArgumentParser();p.add_argument('--base-dir',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--review-lib',type=Path,required=True);p.add_argument('--workers',type=int,default=64);a=p.parse_args()
    if not 1<=a.workers<=64:raise ValueError('workers must be 1..64')
    sys.path.insert(0,str(a.review_lib));import mount_pilot_v3 as reviewer
    # Only clarify the existing exact-quotation schema, not judgment criteria.
    reviewer.PROMPT+='\n格式提醒：request_id较短，请逐字复制。证据优先选取短的连续原文，不抄整段目录，不重排空格、换行或标点，不加入省略号。'
    reviewer.COUNTER_PROMPT+='\n格式提醒：request_id请逐字复制；证据仅选短的连续原文，不拼接或改写。'
    a.out.mkdir(parents=True,exist_ok=False)
    def lines(f):return [json.loads(x) for x in f.read_text(encoding='utf-8').splitlines() if x.strip()]
    items=[];links=[];hashes={}
    for subject,dirname in [('mechanical','ab_mechanical_new_endpoint'),('architecture','ab_architecture')]:
        d=a.base_dir/dirname
        inputs=lines(d/'input.jsonl');outputs={v:lines(d/(v+'.jsonl')) for v in ['with_cards','without_cards']}
        tree=json.loads((d/'profiles/with_cards/data/knowledge_tree.json').read_text(encoding='utf-8'))
        batch,refs=prepare_reviews(inputs,outputs,tree)
        remap={}
        for item in batch:
            short='R'+str(len(items)+1).zfill(4);remap[item['request_id']]=short
            items.append(dict(item,request_id=short))
        for ref in refs:
            if ref['request_id']:
                links.append(dict(ref,subject=subject,original_request_id=ref['request_id'],request_id=remap[ref['request_id']]))
        for f in [d/'input.jsonl',d/'with_cards.jsonl',d/'without_cards.jsonl']:
            hashes[str(f)]=hashlib.sha256(f.read_bytes()).hexdigest()
    write_lines(a.out/'input.jsonl',items);write_json(a.out/'links.json',links)
    write_json(a.out/'config.json',dict(base=BASE,model=MODEL,workers=a.workers,unique_requests=len(items),accepted_mounts=len(links),input_hashes=hashes,previous_verdicts_sent=False,semantic_cards_sent=False))
    (a.out/'prompt.txt').write_text(reviewer.PROMPT,encoding='utf-8')
    (a.out/'counter_prompt.txt').write_text(reviewer.COUNTER_PROMPT,encoding='utf-8')
    models=reviewer.request_json(BASE+'/v1/models');write_json(a.out/'models.json',models)
    assert MODEL in [r['id'] for r in models['data']]
    results={}
    with (a.out/'responses.jsonl').open('w',encoding='utf-8') as log:
        probe=reviewer.review_one(BASE,MODEL,items[0]);log.write(json.dumps(probe,ensure_ascii=False)+'\n');log.flush()
        if probe['final']['judgment']=='technical_failure':raise RuntimeError('probe failed; inspect raw output')
        results[probe['request_id']]=probe
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            tasks=[pool.submit(reviewer.review_one,BASE,MODEL,item) for item in items[1:]]
            for future in as_completed(tasks):
                r=future.result();results[r['request_id']]=r;log.write(json.dumps(r,ensure_ascii=False)+'\n');log.flush()
                if len(results)%20==0:print('review',len(results),'/',len(items),flush=True)
    assert set(results)=={x['request_id'] for x in items}
    by={x['request_id']:x for x in items};details=[dict(link,record=by[link['request_id']],review=results[link['request_id']]['final']) for link in links]
    write_lines(a.out/'details.jsonl',details)
    summary={}
    for subject in ['mechanical','architecture']:
        summary[subject]={v:metrics([r['review']['judgment'] for r in details if r['subject']==subject and r['variant']==v]) for v in ['with_cards','without_cards']}
    write_json(a.out/'summary.json',summary)
    assert all(hashlib.sha256(Path(f).read_bytes()).hexdigest()==h for f,h in hashes.items())
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
