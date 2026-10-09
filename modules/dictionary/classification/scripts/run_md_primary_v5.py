"""MD-led classification with aligned semantic font annotations; no extraction."""
import argparse
import bisect
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import unicodedata
import urllib.request
import md_primary_v5 as core


def read(p):return json.loads(p.read_text(encoding='utf-8'))
def write(p,v):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,p)
def norm(t):return ''.join(c.lower() for c in unicodedata.normalize('NFKC',t) if c.isalnum())
def is_pdf(path):
    with path.open('rb') as f:return b'%PDF-' in f.read(1024)
def http(url,payload=None,timeout=600):
    data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    req=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as f:return json.load(f)


def font_annotations(rows,pdf):
    weights=collections.Counter()
    for line in pdf:
        for s in line['spans']:weights[round(s['size'],1)]+=len(s['text'].strip())
    body_size=weights.most_common(1)[0][0] if weights else 1
    if body_size<=0:body_size=1
    normalized=[norm(l['text']) for l in pdf]
    md=[norm(r['text']) for r in rows]
    for row,text in zip(rows,md):
        annotations=[]
        for idx,line in enumerate(pdf):
            query=normalized[idx]
            minimum=2 if any('\u4e00'<=c<='\u9fff' for c in query) else 4
            if len(query)<minimum or normalized.count(query)!=1:continue
            if query not in text or text.count(query)!=1 or sum(query in t for t in md)!=1:continue
            spans=[]
            for s in line['spans']:
                item={'text':s['text'],'size_ratio':round(s['size']/body_size,2),
                      'bold':bool(s['flags']&16),'italic':bool(s['flags']&2),'superscript':bool(s['flags']&1)}
                if spans and all(spans[-1][k]==item[k] for k in ['size_ratio','bold','italic','superscript']):
                    spans[-1]['text']+=item['text']
                else:spans.append(item)
            if any(s['bold'] or s['italic'] or s['superscript'] or abs(s['size_ratio']-1)>.08 for s in spans):
                annotations.append({'page':line['page'],'spans':spans})
        row['pdf_format']=annotations


def attach_format(windows,pdfpath):
    import fitz
    with fitz.open(pdfpath) as doc:
        combined='';ends=[]
        for page in doc:
            combined+=norm(page.get_text());ends.append(len(combined))
        for w in windows:
            hits=[]
            for row in w['lines']:
                text=norm(row['text'])
                if len(text)<30:continue
                query=text[:55];at=combined.find(query)
                if at>=0 and combined.find(query,at+1)<0:
                    pi=bisect.bisect_right(ends,at)
                    if pi<len(ends) and at+len(query)<=ends[pi]:hits.append(pi)
            counts=collections.Counter(hits)
            if not counts:w['pdf_format_status']='unaligned';continue
            best=counts.most_common(1)[0][0]
            if sum(c for p,c in counts.items() if abs(p-best)<=1)<2:
                w['pdf_format_status']='insufficient_unique_anchors';continue
            selected=sorted({p for p in counts if abs(p-best)<=1});pdf=[]
            for pi in selected:
                for b in doc[pi].get_text('dict')['blocks']:
                    for line in b.get('lines',[]):
                        pdf.append({'text':''.join(s['text'] for s in line['spans']),'page':pi+1,'spans':line['spans']})
            font_annotations(w['lines'],pdf)
            w['pdf_format_status']='unique_text_matched_annotations_only'
            w['pdf_anchor_pages']=[p+1 for p in selected]
            w['format_attached_lines']=sum(bool(r.get('pdf_format')) for r in w['lines'])


def prepare(ref,base,out):
    old=read(base/'prepared'/f'{ref}.json');stats=old['source_stats']
    for st in stats:
        s=Path(st['path']).stat()
        if (s.st_size,s.st_mtime_ns)!=(st['size'],st['mtime_ns']):raise ValueError('source_changed')
    mdpath=next(Path(s['path']) for s in stats if s['path'].lower().endswith('.md'))
    rows=mdpath.read_text(encoding='utf-8-sig',errors='replace').splitlines()
    windows=core.sample_md(rows)
    pdfpath=next((Path(s['path']) for s in stats if not s['path'].lower().endswith('.md') and is_pdf(Path(s['path']))),None)
    format_error=None
    if pdfpath:
        try:attach_format(windows,pdfpath)
        except Exception as e:format_error=repr(e)
    for w in windows:
        for row in w['lines']:assert row['text']==rows[int(row['id'].split(':')[1])-1]
    result={'ref':ref,'title':old['title'],'windows':windows,'source_stats':stats,'md_sha256':hashlib.sha256(mdpath.read_bytes()).hexdigest(),
            'candidate_count':len(core.candidates(rows)),'pdf_format_error':format_error,
            'scope':'MD primary; PDF semantic formatting only; not extraction'}
    write(out/'prepared'/f'{ref}.json',result);return result


def process(ref,base,out,cfg):
    dest=out/'results'/f'{ref}.json'
    if dest.exists():return read(dest)
    path=out/'prepared'/f'{ref}.json';prep=read(path) if path.exists() else prepare(ref,base,out)
    msgs=[{'role':'system','content':core.PROMPT},{'role':'user','content':json.dumps({'ref':ref,'windows':prep['windows']},ensure_ascii=False,separators=(',',':'))}]
    errors=[]
    for attempt in range(2):
        try:
            count=http(cfg['api_url']+'/tokenize',{'model':cfg['model'],'messages':msgs,'add_generation_prompt':True})['count']
            if count+10000>cfg['context_limit']:raise ValueError('context_budget_exceeded')
            payload={'model':cfg['model'],'messages':msgs,'max_tokens':9000,'temperature':0,'chat_template_kwargs':{'enable_thinking':False},'response_format':{'type':'json_object'}}
            write(out/'sent'/f'{ref}_{attempt}.json',{'prompt_tokens':count,'payload':payload})
            raw=http(cfg['api_url']+'/v1/chat/completions',payload);write(out/'raw'/f'{ref}_{attempt}.json',raw)
            c=raw['choices'][0]
            if c['finish_reason']!='stop':raise ValueError('incomplete_response')
            result=core.validate(json.loads(c['message']['content']),prep['windows'])
            result.update(ref=ref,title=prep['title'],prompt_tokens=count,errors=errors)
            write(dest,result);return result
        except Exception as e:
            errors.append(repr(e));msgs=msgs[:2]+[{'role':'user','content':'修复响应格式或引文问题，不改变原文。错误：'+str(e)[:250]}]
    result={'ref':ref,'status':'technical_failed','errors':errors};write(dest,result);return result


def execute(base,out,pilot=False):
    import fcntl
    out.mkdir(parents=True,exist_ok=True)
    with (out/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        catalog=read(base/'acceptance_v2/catalog_113_audited.json')
        refs=[b['ref'] for b in catalog if b.get('acceptance_v2',{}).get('status') in ['review','sample_classified']]
        assert len(refs)==43
        cfg=read(base/'manifest.json')['config']
        hashes={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in ['run_md_primary_v5.py','md_primary_v5.py']}
        manifest={'targets':refs,'config':cfg,'code_sha256':hashes,'baseline':str(base/'acceptance_v2/catalog_113_audited.json'),'scope':'30 review + 13 classified controls; remaining70 untouched'}
        if (out/'manifest.json').exists():assert read(out/'manifest.json')==manifest
        else:
            write(out/'manifest.json',manifest);(out/'code').mkdir(exist_ok=True)
            for n in hashes:shutil.copy2(Path(__file__).with_name(n),out/'code'/n)
        available=http(cfg['api_url']+'/v1/models')['data']
        assert cfg['model'] in [m['id'] for m in available],'configured_model_unavailable'
        chosen=['S017','S035','S040','S081','S090'] if pilot else refs
        def work(ref):
            try:return process(ref,base,out,cfg)
            except Exception as e:
                v={'ref':ref,'status':'technical_failed','errors':[repr(e)]};write(out/'results'/f'{ref}.json',v);return v
        first=work(chosen[0]);print(chosen[0],first['status'],flush=True)
        if first['status']=='technical_failed':raise RuntimeError('preflight_failed')
        done=1
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures={pool.submit(work,r):r for r in chosen[1:]}
            for f in concurrent.futures.as_completed(futures):
                v=f.result();done+=1;print(futures[f],v['status'],done,len(chosen),flush=True)
                write(out/'status.json',{'phase':'running','completed':done,'total':len(chosen),'pilot':pilot})
        results={r:read(out/'results'/f'{r}.json') for r in chosen}
        summary={'selected':len(chosen),'statuses':dict(collections.Counter(v['status'] for v in results.values())),'pilot':pilot,'human_verified':False}
        write(out/('PILOT_DONE.json' if pilot else 'DONE.json'),summary)
        if not pilot:
            for b in catalog:
                if b['ref'] in results:b['md_primary_v5']=results[b['ref']]
            write(out/'catalog_113.json',catalog)
        write(out/'status.json',{'phase':'complete',**summary});print(json.dumps(summary),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--pilot',action='store_true');a=p.parse_args()
    try:execute(a.base,a.out,a.pilot)
    except Exception as e:
        write(a.out/'FAILED.json',{'error':repr(e),'time':time.time()});raise
