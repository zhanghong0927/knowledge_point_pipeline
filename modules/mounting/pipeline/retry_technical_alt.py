"""Retry only unresolved technical items on the user-selected alternate endpoint."""
import json,os,time
from pathlib import Path
from runner import BASE,MODEL,review_batch,mount,js,jl,reviewer
from core import load_records
from ab_review import prepare_reviews

ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
SUBJECTS=('mechanical_engineering','architecture','history','literature')

def mapping_excerpt_items(items,limit=3200):
    """Bound long semantic fields only in isolated remount requests; retain originals."""
    result=[]
    for item in items:
        copy=dict(item)
        for field in ('definition','en_definition','description','en_description'):
            value=copy.get(field)
            if isinstance(value,str) and len(value)>limit:
                copy[field]=value[:limit]
        result.append(copy)
    return result

def retry_review(subject,stage):
    d=ROOT/subject
    previous=d/('technical_retry_'+stage)/'amended_final.jsonl'
    source=previous if previous.exists() else d/stage/'final.jsonl'
    rows=load_records(source)
    pending=[r['item'] for r in rows if r['review']['judgment']=='technical_failure']
    dest=d/('technical_retry_alt_'+stage)
    if not pending:return dict(subject=subject,stage=stage,source=str(source),pending=0,remaining=0)
    if (dest/'summary.json').exists() and (dest/'amended_final.jsonl').exists():
        report=json.loads((dest/'summary.json').read_text())
        if report['source']==str(source) and report['pending']==len(pending):return report
    results=review_batch(pending,dest,chunk_size=80)
    amended=[dict(r,review=results.get(r['item']['request_id'],r['review'])) for r in rows]
    jl(dest/'amended_final.jsonl',amended)
    report=dict(subject=subject,stage=stage,source=str(source),endpoint=BASE,pending=len(pending),remaining=sum(r['review']['judgment']=='technical_failure' for r in amended),total=len(rows),finished=time.time())
    js(dest/'summary.json',report)
    return report

def retry_mount(subject):
    d=ROOT/subject
    original=load_records(d/'remount/with_cards.jsonl')
    failed={r['record_id'] for r in original if r['knowledge_labeling']['status']!='ok'}
    if not failed:return dict(subject=subject,stage='mount',pending=0,remaining=0)
    items=[r for r in load_records(d/'remount/input.jsonl') if r['record_id'] in failed]
    assert len(items)==len(failed)
    tree=json.loads((d/'boundaries/knowledge_tree.json').read_text())
    cards=load_records(d/'boundaries/semantic_cards.jsonl')
    dest=d/'technical_retry_alt_mount_excerpt_v1'
    mapping_items=mapping_excerpt_items(items)
    mounted=mount(mapping_items,dest,tree,cards,d)
    remaining=sum(r['knowledge_labeling']['status']!='ok' for r in mounted)
    checks,links=prepare_reviews(items,{'with_cards':mounted},tree)
    judged=review_batch(checks,d/'technical_retry_alt_mount_review') if checks else {}
    report=dict(subject=subject,stage='mount',endpoint=BASE,pending=len(items),remaining=remaining,
        full_input=str(d/'remount/input.jsonl'),mapping_excerpt_limit=3200,
        shortened_record_ids=[r['record_id'] for r,m in zip(items,mapping_items) if r!=m],
        reviewed=len(checks),review_technical=sum(r['judgment']=='technical_failure' for r in judged.values()),finished=time.time())
    js(dest/'summary.json',report)
    return report

def main():
    assert '160662987482878464' in BASE,'alternate endpoint must be explicit'
    probe=reviewer.request_json(BASE+'/v1/chat/completions',dict(model=MODEL,max_tokens=32,temperature=0,chat_template_kwargs={'enable_thinking':False},messages=[dict(role='user',content='Reply OK')]))
    js(ROOT/'technical_retry_alt_probe.json',probe)
    results=[]
    for subject in SUBJECTS:
        for stage in ('audit','review'):
            result=retry_review(subject,stage);results.append(result);js(ROOT/'technical_retry_alt_summary.json',results)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        result=retry_mount(subject);results.append(result);js(ROOT/'technical_retry_alt_summary.json',results)
        print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
