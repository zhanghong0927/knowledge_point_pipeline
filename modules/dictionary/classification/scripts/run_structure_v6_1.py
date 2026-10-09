"""Paired 22-book regression. Reuses frozen v6 windows and PDF annotations."""
import argparse
import collections
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import time
import structure_v6_1 as core
import run_md_primary_v5 as io

REFS='S022 S024 S030 S032 S046 S059 S075 S079 S088 S089 S092 S094 S096 S100 S109 S110 S112 S017 S035 S040 S060 S081'.split()
CODE=['run_structure_v6_1.py','structure_v6_1.py','structure_v6.py','md_primary_v5.py','run_md_primary_v5.py']


def process(ref,base,out,cfg):
    dest=out/'results'/f'{ref}.json'
    if dest.exists():return io.read(dest)
    prep=io.read(base/'prepared'/f'{ref}.json')
    for s in prep['source_stats']:
        stat=Path(s['path']).stat()
        if (stat.st_size,stat.st_mtime_ns)!=(s['size'],s['mtime_ns']):raise ValueError('source_changed')
    md=next(Path(s['path']) for s in prep['source_stats'] if s['path'].endswith('.md'))
    if hashlib.sha256(md.read_bytes()).hexdigest()!=prep['md_sha256']:raise ValueError('md_hash_changed')
    windows=core.add_context(prep['windows'],md.read_text(encoding='utf-8-sig',errors='replace').splitlines())
    prep['windows']=windows;io.write(out/'prepared'/f'{ref}.json',prep)
    messages=[{'role':'system','content':core.PROMPT},
              {'role':'user','content':json.dumps({'ref':ref,'windows':windows},ensure_ascii=False,separators=(',',':'))}]
    errors=[]
    for attempt in range(2):
        try:
            count=io.http(cfg['api_url']+'/tokenize',{'model':cfg['model'],'messages':messages,'add_generation_prompt':True})['count']
            if count+16500>cfg['context_limit']:raise ValueError('context_budget_exceeded')
            payload={'model':cfg['model'],'messages':messages,'temperature':0,'max_tokens':16000,
                     'chat_template_kwargs':{'enable_thinking':False},'response_format':{'type':'json_object'}}
            io.write(out/'sent'/f'{ref}_{attempt}.json',{'prompt_tokens':count,'payload':payload})
            raw=io.http(cfg['api_url']+'/v1/chat/completions',payload,timeout=900)
            io.write(out/'raw'/f'{ref}_{attempt}.json',raw)
            choice=raw['choices'][0]
            if choice['finish_reason']!='stop':raise ValueError('incomplete_response')
            result=core.validate(json.loads(choice['message']['content']),windows)
            result.update(ref=ref,title=prep['title'],errors=errors,prompt_tokens=count)
            io.write(dest,result);return result
        except Exception as exc:
            errors.append(repr(exc))
            messages=messages[:2]+[{'role':'user','content':'仅修复格式和原文引证，不改原文。错误：'+str(exc)[:250]}]
    result={'ref':ref,'status':'technical_failed','errors':errors};io.write(dest,result);return result


def execute(base,out,workers):
    import fcntl
    out.mkdir(parents=True,exist_ok=True)
    with (out/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cfg=io.read(base/'manifest.json')['config']
        manifest={'base':str(base),'targets':REFS,'workers':workers,'config':cfg,
                  'design':'same 22 books and same v6 windows; additional preceding context; no extraction',
                  'code_sha256':{n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in CODE}}
        if (out/'manifest.json').exists():assert io.read(out/'manifest.json')==manifest,'manifest_changed'
        else:
            io.write(out/'manifest.json',manifest);(out/'code').mkdir(exist_ok=True)
            for n in CODE:shutil.copy2(Path(__file__).with_name(n),out/'code'/n)
        models=io.http(cfg['api_url']+'/v1/models')['data']
        assert cfg['model'] in [m['id'] for m in models],'configured_model_unavailable'
        def work(ref):
            try:return process(ref,base,out,cfg)
            except Exception as exc:
                r={'ref':ref,'status':'technical_failed','errors':[repr(exc)]}
                io.write(out/'results'/f'{ref}.json',r);return r
        # One successful request must precede concurrent dispatch.
        first='S017';result=work(first);print(first,result['status'],flush=True)
        if result['status']=='technical_failed':raise RuntimeError('preflight_failed')
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending={pool.submit(work,r):r for r in REFS if r!=first}
            for future in concurrent.futures.as_completed(pending):
                r=future.result();print(r['ref'],r['status'],r.get('reason'),flush=True)
                io.write(out/'status.json',{'phase':'model','completed':len(list((out/'results').glob('*.json'))),'total':len(REFS)})
        results=[io.read(out/'results'/f'{r}.json') for r in REFS]
        comparisons=[]
        for r in results:
            old=io.read(base/'results'/f"{r['ref']}.json")
            comparisons.append({'ref':r['ref'],'title':r.get('title',old.get('title')),
                                'old':{k:old.get(k) for k in ['status','primary_organization','md_position']},
                                'new':{k:r.get(k) for k in ['status','primary_organization','md_position','reason']},
                                'excluded_heads':sum(len(w.get('excluded_entries',[])) for w in r.get('records',[])),
                                'damaged_windows':[w['window_id'] for w in r.get('records',[]) if not w['md_usable']]})
        io.write(out/'COMPARISON.json',comparisons)
        summary={'books':len(results),'statuses':dict(collections.Counter(r['status'] for r in results)),
                 'human_verified':False,'semantic_review_complete':False,'time':time.time()}
        io.write(out/'DONE.json',summary);io.write(out/'status.json',{'phase':'complete',**summary})
        print(json.dumps(summary),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path);p.add_argument('--workers',type=int,default=16)
    a=p.parse_args()
    try:execute(a.base,a.out,a.workers)
    except Exception as exc:
        io.write(a.out/'FAILED.json',{'error':repr(exc),'time':time.time()});raise
