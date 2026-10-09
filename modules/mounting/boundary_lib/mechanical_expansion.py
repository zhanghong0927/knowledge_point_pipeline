"""Read-only original-mount audit followed by paired remounting of nonpasses."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib,json,sys
from pathlib import Path
import ab_workflow as ab
from ab_review import prepare_reviews
from accepted_review import metrics

BASE='http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
ROOT=Path('/home/wangqiyuan/work/semantic_boundaries_full_ab_20260922')
SOURCE=Path('/mnt/nas_si002991c1cm/deliver/dictionaries/mechanical_engineering/mechanical_engineering_knowledge_point.json')
TREE_SOURCE=Path('/mnt/nas_si002991c1cm/deliver/taxonomy/mechanical_engineering_taxonomy.json')

def full_indices(rows):
    if not isinstance(rows,list) or not all(isinstance(r,dict) for r in rows):raise ValueError('full source must be an array of objects; no rows silently skipped')
    return list(range(len(rows)))

def choose_indices(rows,n,excluded,seed):
    available=[i for i,r in enumerate(rows) if i not in excluded and isinstance(r,dict) and (r.get('name') or r.get('knowledge_point'))]
    if len(available)<n:raise ValueError('insufficient new records')
    return sorted(available,key=lambda i:hashlib.sha256((seed+'|'+str(i)+'|'+str(rows[i].get('id'))).encode()).hexdigest())[:n]

def cohort(judgment):
    return judgment if judgment in ('unreasonable','uncertain') else None

def add_original_reviews(items,links,selected,baseline):
    existing={(r['record_id'],r.get('path')):r['request_id'] for r in links if r['request_id']}
    for r in selected:
        item=dict(baseline[r['request_id']]);ctx=item.get('taxonomy_context',{})
        if ctx.get('exact_path_exists') and ctx.get('path_names'):item['main_tags']='/'.join(ctx['path_names'])
        key=existing.get((r['request_id'],item['main_tags']))
        if key is None:
            key='original_'+r['request_id'];item['request_id']=key;items.append(item)
        links.append(dict(record_id=r['request_id'],variant='original',request_id=key,path=item['main_tags'],decision='existing',status='ok'))

def lines(p):return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]

def review_batch(items,out,reviewer):
    out.mkdir(exist_ok=False);ab.write_lines(out/'input.jsonl',items)
    ab.write_json(out/'config.json',dict(base=BASE,model=ab.MODEL,workers=64,records=len(items),prompt_sha256=hashlib.sha256((reviewer.PROMPT+reviewer.COUNTER_PROMPT).encode()).hexdigest()))
    (out/'prompt.txt').write_text(reviewer.PROMPT,encoding='utf-8');(out/'counter_prompt.txt').write_text(reviewer.COUNTER_PROMPT,encoding='utf-8')
    results={}
    with (out/'responses.jsonl').open('w',encoding='utf-8') as f:
        with ThreadPoolExecutor(max_workers=64) as pool:
            tasks=[pool.submit(reviewer.review_one,BASE,ab.MODEL,x) for x in items]
            for future in as_completed(tasks):
                r=future.result();results[r['request_id']]=r;f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
                if len(results)%100==0:
                    ab.write_json(out/'progress.json',dict(completed=len(results),total=len(items),counts=dict(Counter(x['final']['judgment'] for x in results.values()))))
                    print(out.name,len(results),'/',len(items),flush=True)
        # Retry serialization failures without changing data or semantic policy.
        failed=[x for x in items if results[x['request_id']]['final']['judgment']=='technical_failure']
        if failed:
            old1,old2=reviewer.PROMPT,reviewer.COUNTER_PROMPT
            extra='\n本轮修正证据格式：仍阅读全部字段判断，evidence仅从name、knowledge_point、main_tags选短连续原文，推理写reason。不要重排字符。技术失败不暗示合理或不合理。'
            reviewer.PROMPT+=extra;reviewer.COUNTER_PROMPT+=extra
            (out/'format_retry_prompt.txt').write_text(extra,encoding='utf-8')
            with ThreadPoolExecutor(max_workers=64) as pool:
                tasks=[pool.submit(reviewer.review_one,BASE,ab.MODEL,x) for x in failed]
                for future in as_completed(tasks):
                    r=future.result();r['format_retry']=True;results[r['request_id']]=r;f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
            reviewer.PROMPT,reviewer.COUNTER_PROMPT=old1,old2
    assert set(results)=={x['request_id'] for x in items}
    ab.write_lines(out/'final.jsonl',[dict(item=x,review=results[x['request_id']]['final']) for x in items])
    ab.write_json(out/'summary.json',metrics([r['final']['judgment'] for r in results.values()]))
    ab.write_json(out/'progress.json',dict(completed=len(results),total=len(items),finished=True,counts=dict(Counter(x['final']['judgment'] for x in results.values()))))
    return results

def main():
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['audit','mount','evaluate']);p.add_argument('--out',type=Path,required=True);p.add_argument('--all',action='store_true');a=p.parse_args()
    ab.BASE=BASE
    sys.path.insert(0,'/home/wangqiyuan/work/knowledge_rule_checks_20260922');import mount_pilot_v3 as reviewer
    extra='\nrequest_id请逐字复制。证据使用短的连续原文，不拼接、省略或改变标点空格；推理放reason。'
    reviewer.PROMPT+=extra;reviewer.COUNTER_PROMPT+=extra
    full=ROOT/'mechanical_complete';tree=json.loads((full/'knowledge_tree.json').read_text(encoding='utf-8'))
    if TREE_SOURCE.read_bytes()!=(full/'source_tree.json').read_bytes():raise ValueError('taxonomy changed since semantic card generation')
    if a.stage=='audit':
        a.out.mkdir(parents=True,exist_ok=False)
        raw=SOURCE.read_bytes();rows=json.loads(raw.decode('utf-8-sig'))
        if a.all:
            excluded=set();indices=full_indices(rows)
        else:
            old=json.loads((ROOT/'ab_mechanical/manifest.json').read_text())
            if old['sha256']!=hashlib.sha256(raw).hexdigest():raise ValueError('source changed; old row exclusion requires remapping')
            excluded={r['source_index'] for r in json.loads((ROOT/'ab_mechanical/sample_provenance.json').read_text())}
            indices=choose_indices(rows,1000,excluded,'mechanical_new1000_20260922')
        (a.out/'source_snapshot.json').write_bytes(raw)
        paths={}
        def walk(n,chain):
            chain=chain+[n];names=[x['name_zh'] for x in chain]
            context=dict(exact_path_exists=True,path_names=names,children_names=[x['name_zh'] for x in n.get('children',[])])
            paths['/'.join(names)]=context;paths['/'.join(names[1:])]=context
            for c in n.get('children',[]):walk(c,chain)
        walk(tree,[])
        items=[];provenance=[]
        for j,i in enumerate(indices):
            r=rows[i];record={k:r[k] for k in ab.KEEP if k in r};rid='M'+str(j+1).zfill(4)
            item=dict(record,request_id=rid,main_tags=r.get('main_tags',''))
            item['taxonomy_context']=paths.get(item['main_tags'],dict(exact_path_exists=False))
            items.append(item);provenance.append(dict(request_id=rid,source_index=i,record=dict(record,record_id=rid),original_main_tags=item['main_tags']))
        ab.write_json(a.out/'provenance.json',provenance)
        ab.write_json(a.out/'manifest.json',dict(source=str(SOURCE),source_sha256=hashlib.sha256(raw).hexdigest(),source_records=len(rows),sample_records=len(indices),full_corpus=a.all,excluded_previous_sample=len(excluded),base=BASE,model=ab.MODEL,workers=64,taxonomy_sha256=hashlib.sha256(TREE_SOURCE.read_bytes()).hexdigest(),selection='all source rows in original order' if a.all else 'SHA256 ranking excluding previous 100 rows'))
        models=reviewer.request_json(BASE+'/v1/models');assert ab.MODEL in [m['id'] for m in models['data']]
        ab.write_json(a.out/'models.json',models)
        probe=reviewer.review_one(BASE,ab.MODEL,items[0]);ab.write_json(a.out/'probe.json',probe)
        if probe['final']['judgment']=='technical_failure':raise RuntimeError('probe failed')
        results=review_batch(items,a.out/'baseline',reviewer)
        selected=[dict(r,cohort=cohort(results[r['request_id']]['final']['judgment'])) for r in provenance if cohort(results[r['request_id']]['final']['judgment'])]
        ab.write_json(a.out/'remount_cohort.json',selected)
        mount=a.out/'remount';mount.mkdir();ab.write_lines(mount/'input.jsonl',[r['record'] for r in selected])
        assert SOURCE.read_bytes()==raw
        print('BASELINE',json.dumps(metrics([r['final']['judgment'] for r in results.values()]),ensure_ascii=False),'REMOUNT',len(selected),flush=True)
    elif a.stage=='mount':
        selected=json.loads((a.out/'remount_cohort.json').read_text())
        if not selected:print('No semantic nonpasses to remount');return
        ab.prepare_profiles(full,a.out/'remount',ROOT/'mapping_runtime','mechanical_engineering')
        ab.run_mounts(a.out/'remount',ROOT/'mapping_runtime',32)
    else:
        mount=a.out/'remount';inputs=lines(mount/'input.jsonl');outputs={v:lines(mount/(v+'.jsonl')) for v in ['with_cards','without_cards']}
        items,links=prepare_reviews(inputs,outputs,tree);by={x['request_id']:x for x in items}
        selected=json.loads((a.out/'remount_cohort.json').read_text());baseline={r['item']['request_id']:r['item'] for r in lines(a.out/'baseline/final.jsonl')}
        add_original_reviews(items,links,selected,baseline)
        # Opaque short IDs; prior labels, cohort membership, and arm are not sent.
        remap={}
        for j,item in enumerate(sorted(items,key=lambda x:hashlib.sha256(x['request_id'].encode()).hexdigest())):
            old=item['request_id'];new='E'+str(j+1).zfill(4);remap[old]=new;item['request_id']=new
        for ref in links:
            if ref['request_id']:ref['request_id']=remap[ref['request_id']]
        results=review_batch(items,a.out/'effect_review',reviewer)
        groups={r['request_id']:r['cohort'] for r in selected};byitem={x['request_id']:x for x in items}
        detail=[dict(ref,cohort=groups[ref['record_id']],item=byitem.get(ref['request_id']),review=results[ref['request_id']]['final'] if ref['request_id'] else dict(judgment='not_mounted')) for ref in links]
        ab.write_lines(a.out/'effect_details.jsonl',detail)
        report={g:{v:metrics([r['review']['judgment'] for r in detail if (g=='all' or r['cohort']==g) and r['variant']==v]) for v in ['original','with_cards','without_cards']} for g in ['all','unreasonable','uncertain']}
        ab.write_json(a.out/'effect_summary.json',report);print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
