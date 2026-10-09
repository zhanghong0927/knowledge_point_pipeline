"""One endpoint, one subject/stage at a time; only with-boundary remounting."""
import argparse,json,sys,time,os,shutil,hashlib,subprocess,traceback,fcntl
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from core import load_records,needs_mount,verify_ids,content_key,require_equal
HERE=Path(__file__).resolve().parent
LIB=HERE.parent/'boundary_lib'
sys.path.insert(0,str(HERE.parent/'review_lib'));sys.path.insert(0,str(LIB));sys.path.insert(0,str(HERE))
import generate_semantic_boundaries as generator
import mount_pilot_v3 as reviewer
import ab_workflow as ab
from ab_review import prepare_reviews
BASE=os.environ.get('KNOWLEDGE_ENDPOINT_OVERRIDE','http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com')
MODEL=os.environ.get('KNOWLEDGE_MODEL',ab.MODEL)
NAS=Path(os.environ.get('KNOWLEDGE_DATA_ROOT','/mnt/nas_si002991c1cm/deliver'))
PREV=Path('/home/wangqiyuan/work/semantic_boundaries_full_ab_20260922')
PACKAGE=HERE.parent/'mapping_runtime'
SUBJECTS=['mechanical_engineering','architecture','history','literature','philosophy','economy','military','art','civil_engineering','management','sociology','education']
REUSE={} # Portable package must not reuse unavailable historical runs.
SUFFIX='\nrequest_id请逐字复制。证据使用短的连续原文，不拼接、省略或改变标点空格；推理放reason。'
P1=reviewer.PROMPT+SUFFIX;P2=reviewer.COUNTER_PROMPT+SUFFIX
def js(p,x):
    p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2));tmp.replace(p)
def jl(p,rows):p.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def state(out,**values):
    target=out/os.environ['KNOWLEDGE_SUBJECT'] if os.environ.get('KNOWLEDGE_SUBJECT') else out
    js(target/'status.json',dict(updated=time.time(),pid=os.getpid(),endpoint=BASE,workers=int(os.environ.get('KNOWLEDGE_REVIEW_WORKERS','1024')),**values))
def init(out):
    out.mkdir(exist_ok=False,parents=True);entries=[]
    for s in SUBJECTS:
        folder=NAS/'dictionaries'/s
        candidates=[p for p in folder.iterdir() if p.is_file() and 'knowledge' in p.name and p.suffix in ['.json','.jsonl']]
        if len(candidates)!=1:raise ValueError((s,'ambiguous sources',candidates))
        source=candidates[0];tree=NAS/'taxonomy'/('economics_taxonomy.json' if s=='economy' else s+'_taxonomy.json')
        rows=load_records(source);nodes=generator.normalize(json.loads(tree.read_text(encoding='utf-8-sig')))
        d=out/s;d.mkdir();shutil.copy2(source,d/'source.snapshot');shutil.copy2(tree,d/'taxonomy.json')
        e=dict(subject=s,source=str(source),tree=str(tree),source_sha256=sha(source),tree_sha256=sha(tree),records=len(rows),nodes=len(nodes),status='queued')
        js(d/'manifest.json',e);entries.append(e)
    js(out/'inventory.json',entries);state(out,stage='ready',subjects=len(entries),records=sum(x['records'] for x in entries))
def call(cmd,log):
    with log.open('a') as f:subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,check=True)
def complete_boundary_dir(d):
    for name in ('boundaries_v6_alt','boundaries_v5','boundaries'):
        target=d/name;summary_file=target/'summary.json'
        if summary_file.is_file():
            summary=json.loads(summary_file.read_text())
            if summary.get('total_tree_nodes')==summary.get('accepted_candidate_cards') and summary.get('source_unchanged'):
                return target
    return None
def cards_for(d,s):
    original=d/'boundaries'
    incomplete=original.exists() and (original/'summary.json').exists() and json.loads((original/'summary.json').read_text()).get('total_tree_nodes')!=json.loads((original/'summary.json').read_text()).get('accepted_candidate_cards')
    target=complete_boundary_dir(d) or (d/'boundaries_v5' if incomplete else original)
    if s in REUSE and not target.exists():
        old=PREV/REUSE[s][0]
        if sha(old/'source_tree.json')==sha(d/'taxonomy.json') and (old/'generation_prompt.txt').read_text()==generator.PROMPT and (old/'review_prompt.txt').read_text()==generator.REVIEW_PROMPT:
            shutil.copytree(old,target);js(d/'boundary_reuse.json',dict(source=str(old),verified_source_and_prompts=True))
    for attempt in range(3):
        if target.exists() and (target/'summary.json').exists():
            summary=json.loads((target/'summary.json').read_text())
            if summary['total_tree_nodes']==summary['accepted_candidate_cards']:break
        cmd=[sys.executable,str(HERE/'generate_semantic_boundaries.py'),'--tree',str(d/'taxonomy.json'),'--out',str(target),'--base',BASE,'--model',MODEL,'--workers','1024','--review-mode','advisory','--cross-review','off']
        if s=='education' and target.name=='boundaries_v5':
            cmd+=['--reuse-run',str(d/'boundary_probe_batched_v5')]
        if target.exists():cmd+=['--resume']
        call(cmd,d/'boundaries.log')
    tree=json.loads((target/'knowledge_tree.json').read_text());cards=load_records(target/'semantic_cards.jsonl');ab.check_assets(tree,cards)
    return tree,cards
def cached_reviews(path,items):
    if not path or not (path/'final.jsonl').exists():return {}
    if not (path/'prompt.txt').exists() or (path/'prompt.txt').read_text()!=P1 or (path/'counter_prompt.txt').read_text()!=P2:return {}
    valid={content_key(r['item']):r['review'] for r in load_records(path/'final.jsonl') if r['review']['judgment']!='technical_failure'}
    return {x['request_id']:valid[content_key(x)] for x in items if content_key(x) in valid}
def review_batch(items,d,old=None,chunk_size=4096):
    d.mkdir(exist_ok=True);results=cached_reviews(old,items)
    inp=d/'input.jsonl'
    if inp.exists() and load_records(inp)!=items:raise ValueError('resume review input changed')
    if (d/'prompt.txt').exists():
        require_equal((d/'prompt.txt').read_text(),P1,'review prompt')
        require_equal((d/'counter_prompt.txt').read_text(),P2,'counter prompt')
    jl(inp,items);(d/'prompt.txt').write_text(P1);(d/'counter_prompt.txt').write_text(P2)
    if old:js(d/'reuse.json',dict(source=str(old),reused=len(results)))
    raw=d/'responses.jsonl'
    if raw.exists():
        for r in load_records(raw):
            if r['final']['judgment']!='technical_failure':results[r['request_id']]=r['final']
    expected={x['request_id'] for x in items};assert set(results)<=expected
    with raw.open('a') as f:
        for round_no in range(2):
            pending=[x for x in items if x['request_id'] not in results or results[x['request_id']]['judgment']=='technical_failure']
            extra='' if round_no==0 else '\n证据格式修正：仍阅读全部字段，仅从name、knowledge_point、main_tags引用短连续原文，不翻译或改写。技术失败不暗示合理或不合理。'
            reviewer.PROMPT=P1+extra;reviewer.COUNTER_PROMPT=P2+extra
            if extra:(d/'format_retry_prompt.txt').write_text(extra)
            for start in range(0,len(pending),chunk_size):
                chunk=pending[start:start+chunk_size]
                with ThreadPoolExecutor(max_workers=int(os.environ.get('KNOWLEDGE_REVIEW_WORKERS','1024'))) as pool:
                    for future in as_completed([pool.submit(reviewer.review_one,BASE,MODEL,x) for x in chunk]):
                        r=future.result();r['round']=round_no;results[r['request_id']]=r['final'];f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
                        if len(results)%100==0:js(d/'progress.json',dict(completed=len(results),total=len(items),counts=dict(Counter(r['judgment'] for r in results.values()))))
                # A tiny residual retry set can be mostly technical without the
                # endpoint failing for the subject as a whole; retain those
                # failures in final.jsonl instead of aborting the entire run.
                if (len(chunk)>100 and len(pending)>len(items)*.05
                        and sum(results[x['request_id']]['judgment']=='technical_failure' for x in chunk)>len(chunk)*.5):
                    raise RuntimeError('API_OR_SCHEMA_FAILURE_RATE_OVER_50_PERCENT; stop queue')
    verify_ids([{'id':k} for k in results],expected,'id')
    final=[dict(item=x,review=results[x['request_id']]) for x in items];jl(d/'final.jsonl',final)
    js(d/'progress.json',dict(completed=len(items),total=len(items),finished=True,counts=dict(Counter(r['judgment'] for r in results.values()))))
    return results
def audit_items(rows,tree):
    paths={}
    def walk(n,chain):
        chain=chain+[n];names=[v['name_zh'] for v in chain];ctx=dict(exact_path_exists=True,path_names=names,children_names=[c['name_zh'] for c in n['children']])
        paths['/'.join(names)]=ctx;paths['/'.join(names[1:])]=ctx
        for c in n['children']:walk(c,chain)
    walk(tree,[])
    items=[]
    for i,r in enumerate(rows):
        tags=r.get('main_tags','');ctx=paths.get(tags,dict(exact_path_exists=False)) if isinstance(tags,str) else dict(exact_path_exists=False)
        items.append(dict({k:r[k] for k in ab.KEEP if k in r},request_id='M'+str(i+1).zfill(4),main_tags=tags,taxonomy_context=ctx))
    return items
def mount(items,d,tree,cards,old):
    d.mkdir(exist_ok=True);profile=d/'profile'
    if (d/'input.jsonl').exists():require_equal(load_records(d/'input.jsonl'),items,'mount input')
    if profile.exists():
        require_equal(json.loads((profile/'data/knowledge_tree.json').read_text()),tree,'mount tree')
        require_equal(load_records(profile/'data/semantic_cards.jsonl'),cards,'mount cards')
    jl(d/'input.jsonl',items)
    if not profile.exists():
        shutil.copytree(PACKAGE/'profiles/template',profile);(profile/'data').mkdir(exist_ok=True)
        js(profile/'data/knowledge_tree.json',tree);jl(profile/'data/semantic_cards.jsonl',cards)
        conf=json.loads((profile/'profile.json').read_text());conf.update(subject_name=tree['name_zh'],subject_scope='以所提供分类树实际覆盖范围为准，包括：'+'、'.join(c['name_zh'] for c in tree['children'])+'。不得仅因跨学科排除。');js(profile/'profile.json',conf)
    cached={};by={r['record_id']:r for r in items}
    # Reuse historical routes only when tree and full cards are byte-identical and item input agrees.
    if old and (old/'remount/with_cards.jsonl').exists():
        op=old/'remount/profiles/with_cards/data'
        if (op/'semantic_cards.jsonl').is_file() and (op/'knowledge_tree.json').is_file() and (old/'remount/input.jsonl').is_file() and load_records(op/'semantic_cards.jsonl')==cards and json.loads((op/'knowledge_tree.json').read_text())==tree:
            oi={r['record_id']:r for r in load_records(old/'remount/input.jsonl')}
            cached={r['record_id']:r for r in load_records(old/'remount/with_cards.jsonl') if r['record_id'] in by and oi.get(r['record_id'])==by[r['record_id']] and r['knowledge_labeling']['status']=='ok'}
    result=d/'with_cards.jsonl'
    if result.exists():
        for r in load_records(result):
            if r['knowledge_labeling']['status']=='ok':cached[r['record_id']]=r
    if (d/'new.jsonl').exists():
        previous_pending={r['record_id']:r for r in load_records(d/'pending.jsonl')}
        for r in load_records(d/'new.jsonl'):
            if r['record_id'] in by and previous_pending.get(r['record_id'])==by[r['record_id']] and r['knowledge_labeling']['status']=='ok':cached[r['record_id']]=r
        # Preserve raw partial responses before the next mount subprocess overwrites its output.
        shutil.copy2(d/'new.jsonl',d/('new.before_resume_'+str(time.time_ns())+'.jsonl'))
    pending=[r for r in items if r['record_id'] not in cached];jl(d/'pending.jsonl',pending)
    if pending:
        mount_workers=os.environ.get('KNOWLEDGE_MOUNT_WORKERS','1024')
        cmd=[sys.executable,str(PACKAGE/'scripts/hierarchical-knowledge-labeling-beam-v3.py'),'--input',str(d/'pending.jsonl'),'--output',str(d/'new.jsonl'),'--profile-dir',str(profile),'--api-url',BASE+'/v1/chat/completions','--model',MODEL,'--workers',mount_workers,'--retry-failed-workers',mount_workers,'--retry-failed-rounds','1','--retries','1','--timeout','180','--temperature','0','--max-tokens','4096','--max-seed-examples','0','--no-progress','--progress-interval','100']
        js(d/'command.json',cmd);call(cmd,d/'mount.log')
        for r in load_records(d/'new.jsonl'):cached[r['record_id']]=r
    final=[cached[r['record_id']] for r in items];verify_ids(final,set(by),'record_id');jl(result,final);return final
def subject(out,e):
    s=e['subject'];d=out/s
    if sha(d/'source.snapshot')!=e['source_sha256'] or sha(d/'taxonomy.json')!=e['tree_sha256']:raise ValueError('snapshot changed')
    old=PREV/REUSE[s][1] if s in REUSE else None
    if old:
        manifest=json.loads((old/'manifest.json').read_text())
        if manifest['source_sha256']!=e['source_sha256'] or manifest['taxonomy_sha256']!=e['tree_sha256']:old=None
    state(out,subject=s,stage='boundaries');tree,cards=cards_for(d,s)
    state(out,subject=s,stage='audit');rows=load_records(d/'source.snapshot');items=audit_items(rows,tree)
    results=review_batch(items,d/'audit',old/'baseline' if old else None)
    selected=[dict({k:x[k] for k in ab.KEEP if k in x},record_id=x['request_id']) for x in items if needs_mount(results[x['request_id']]['judgment'])]
    state(out,subject=s,stage='mount_with_cards',selected=len(selected));mounted=mount(selected,d/'remount',tree,cards,old)
    state(out,subject=s,stage='review_new_mount');checks,links=prepare_reviews(selected,{'with_cards':mounted},tree)
    judged=review_batch(checks,d/'review',old/'effect_review' if old else None)
    details=[dict(r,review=judged[r['request_id']] if r['request_id'] else {'judgment':'not_mounted'}) for r in links];jl(d/'details.jsonl',details)
    verify_ids(details,{r['record_id'] for r in selected},'record_id')
    technical=sum(r['judgment']=='technical_failure' for r in results.values())+sum(r['knowledge_labeling']['status']!='ok' for r in mounted)+sum(r['judgment']=='technical_failure' for r in judged.values())
    summary=dict(subject=s,records=len(rows),nodes=len(cards),baseline=dict(Counter(r['judgment'] for r in results.values())),remount=len(selected),final=dict(Counter(r['review']['judgment'] for r in details)),technical_failures=technical,source_unchanged=sha(Path(e['source']))==e['source_sha256'],taxonomy_unchanged=sha(Path(e['tree']))==e['tree_sha256'])
    js(d/'summary.json',summary);js(d/'done.json',dict(completed=True,status='completed_with_issues' if technical else 'completed',requires_targeted_retry=bool(technical),coverage_ok=True,**summary));return summary
def run(out):
    lock=(HERE/'endpoint.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    inventory=json.loads((out/'inventory.json').read_text());probe=reviewer.request_json(BASE+'/v1/chat/completions',dict(model=MODEL,max_tokens=32,temperature=0,chat_template_kwargs={'enable_thinking':False},messages=[dict(role='user',content='Reply OK')]))
    js(out/'api_probe.json',probe)
    summaries=[]
    for e in inventory:
        d=out/e['subject']
        if (d/'done.json').exists():summaries.append(json.loads((d/'done.json').read_text()));continue
        try:summaries.append(subject(out,e));e['status']='completed'
        except Exception as ex:
            e['status']='blocked';e['error']=str(ex);(d/'error.txt').write_text(traceback.format_exc())
            if 'stop queue' in str(ex):state(out,stage='paused_on_failures',subject=e['subject'],error=str(ex));js(out/'inventory.json',inventory);return
        js(out/'inventory.json',inventory);js(out/'summaries.json',summaries)
    state(out,stage='finished_with_issues' if any(e['status']=='blocked' for e in inventory) or any(s['technical_failures'] for s in summaries) else 'completed',subjects=len(summaries),blocked=[e['subject'] for e in inventory if e['status']=='blocked'])
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['init','run','subject']);p.add_argument('--out',type=Path,required=True);p.add_argument('--subject');a=p.parse_args()
    if a.action=='subject':
        os.environ['KNOWLEDGE_SUBJECT']=a.subject
        lock=(a.out/a.subject/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        e=next(e for e in json.loads((a.out/'inventory.json').read_text()) if e['subject']==a.subject)
        try:
            if not (a.out/a.subject/'done.json').exists():subject(a.out,e)
            state(a.out,stage='completed',subject=a.subject)
        except Exception:
            state(a.out,stage='blocked',subject=a.subject,error=traceback.format_exc());raise
    else:(init if a.action=='init' else run)(a.out)
