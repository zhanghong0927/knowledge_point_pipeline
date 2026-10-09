"""Retry only technical failures from completed subjects, preserving originals."""
import json,sys,time
from pathlib import Path
from runner import review_batch,js,jl
from core import load_records
ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')

def retry(subject,stage):
    source=ROOT/subject/stage/'final.jsonl'
    rows=load_records(source)
    pending=[r['item'] for r in rows if r['review']['judgment']=='technical_failure']
    dest=ROOT/subject/('technical_retry_'+stage)
    if (dest/'summary.json').exists() and (dest/'amended_final.jsonl').exists():
        summary=json.loads((dest/'summary.json').read_text())
        if summary['total']==len(rows) and summary['original_failures']==len(pending):return summary
    if not pending:return dict(subject=subject,stage=stage,original_failures=0,remaining=0)
    results=review_batch(pending,dest,chunk_size=80)
    amended=[dict(r,review=results.get(r['item']['request_id'],r['review'])) for r in rows]
    jl(dest/'amended_final.jsonl',amended)
    remaining=sum(r['review']['judgment']=='technical_failure' for r in amended)
    summary=dict(subject=subject,stage=stage,original_failures=len(pending),remaining=remaining,total=len(rows),finished=time.time())
    js(dest/'summary.json',summary)
    return summary

if __name__=='__main__':
    results=[retry(subject,stage) for subject in ('history','literature') for stage in ('audit','review')]
    js(ROOT/'technical_retry_reviews_summary.json',results)
    print(json.dumps(results,ensure_ascii=False),flush=True)
