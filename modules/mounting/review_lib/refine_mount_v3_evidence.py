"""Revalidate frozen v3 responses; call only previously unperformed counter reviews."""
import argparse,json,hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from mount_pilot_v3 import validate_result,call_one,combine,request_json

def revalidate(call,item):
    if call is None or call['result'] is None: return call
    raw=call['attempts'][-1]['raw'];content=raw['choices'][0]['message']['content'].strip()
    if content.startswith('```'): content=content.split('\n',1)[1].rsplit('```',1)[0]
    return dict(call,result=validate_result(json.loads(content),item))

def main():
    p=argparse.ArgumentParser();p.add_argument('--previous',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    old=Path(a.previous);out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    previous=[json.loads(l) for l in (old/'review_details.jsonl').read_text().splitlines()]
    cfg=json.loads((old/'config.json').read_text());cfg['revalidated_from']=str(old)
    cfg['policy_revision']='v3.1_cross_array_evidence'
    (out/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    for filename in ('samples.jsonl','prompt.txt','counter_prompt.txt','models.json'):
        (out/filename).write_bytes((old/filename).read_bytes())
    models=request_json(cfg['base']+'/v1/models');assert cfg['model'] in [m['id'] for m in models['data']]
    results={};pending=[]
    for item in previous:
        r=item['review_v3'];first=revalidate(r['first'],item);second=revalidate(r['second'],item)
        rec=dict(request_id=item['request_id'],first=first,second=second,reused_first=True)
        if first['result'] and first['result']['judgment']=='unreasonable' and second is None:pending.append((item,rec))
        else:
            rec['final']=combine(first['result'],second['result'] if second else None);results[item['request_id']]=rec
    print('REVALIDATED',len(results),'NEED_COUNTER',len(pending),flush=True)
    with (out/'responses.jsonl').open('w',encoding='utf-8') as journal:
        def save(r):journal.write(json.dumps(r,ensure_ascii=False)+'\n');journal.flush()
        for r in results.values():save(r)
        with ThreadPoolExecutor(max_workers=cfg['workers']) as pool:
            futures={pool.submit(call_one,cfg['base'],cfg['model'],item,True):(item,rec) for item,rec in pending}
            for future in as_completed(futures):
                item,rec=futures[future];rec['second']=future.result();rec['final']=combine(rec['first']['result'],rec['second']['result'])
                results[item['request_id']]=rec;save(rec)
                if len(results)%16==0 or len(results)==len(previous):print('PROGRESS',len(results),'/',len(previous),flush=True)
    assert len(results)==len(previous)==212
    final=[dict(item,review_v3=results[item['request_id']]) for item in previous]
    (out/'review_details.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in final),encoding='utf-8')
    summary=dict(records=212,counts=dict(Counter(r['final']['judgment'] for r in results.values())),
                 second_pass=sum(r['second'] is not None for r in results.values()),new_counter_calls=len(pending),input_unchanged=True)
    assert hashlib.sha256((out/'samples.jsonl').read_bytes()).hexdigest()==cfg['input_sha256']
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
