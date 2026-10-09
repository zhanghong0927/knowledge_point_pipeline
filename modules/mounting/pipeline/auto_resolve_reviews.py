"""Automatically recover technical review failures with immutable overlays.

Only exact source excerpts can be salvaged. Unresolved items receive a fresh
model review; remaining failures stay technical and are explicitly reported.
"""
import argparse
import json
import os
from pathlib import Path
import time

from core import load_records
from runner import SUBJECTS, review_batch, js, mount, complete_boundary_dir
from salvage_exact_evidence import process as salvage_process
from consolidate_retry_results import merge_stage
from retry_technical_alt import mapping_excerpt_items
from ab_review import prepare_reviews

ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')


def stage_files(d, stage):
    source=d/stage
    return source, d/('auto_salvage_'+stage), d/('auto_retry_'+stage), d/('auto_resolved_'+stage)


def resolve_stage(d, stage):
    source,salvage_dir,retry_dir,resolved_dir=stage_files(d,stage)
    original=load_records(source/'final.jsonl')
    technical={r['item']['request_id'] for r in original if r['review']['judgment']=='technical_failure'}
    if not technical:return dict(stage=stage,records=len(original),initial_technical=0,remaining_technical=0)
    existing=d/('resolved_'+stage+'_20260923')/'report.json'
    if existing.is_file():
        report=json.loads(existing.read_text())
        if report.get('records')==len(original) and not report.get('technical_ids'):
            return dict(stage=stage,records=len(original),initial_technical=len(technical),remaining_technical=0,reused=str(existing))
    if (resolved_dir/'report.json').is_file():return json.loads((resolved_dir/'report.json').read_text())
    if not (salvage_dir/'report.json').is_file():salvage_process(source,salvage_dir)
    repaired=load_records(salvage_dir/'repaired.jsonl')
    repaired_ids={r['request_id'] for r in repaired}
    pending=[r['item'] for r in original if r['item']['request_id'] in technical-repaired_ids]
    retry_rows=[]
    if pending:
        verdicts=review_batch(pending,retry_dir,chunk_size=80)
        retry_rows=[dict(request_id=x['request_id'],final=verdicts[x['request_id']]) for x in pending]
    merged=merge_stage(original,[('recovered','exact_evidence_salvage',repaired),('responses','fresh_retry',retry_rows)])
    resolved_dir.mkdir(exist_ok=False)
    with (resolved_dir/'final_reviews.jsonl').open('w') as stream:
        for row in merged:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    remaining=[r['item']['request_id'] for r in merged if r['review']['judgment']=='technical_failure']
    report=dict(stage=stage,records=len(original),initial_technical=len(technical),salvaged=len(repaired),
                retried=len(pending),remaining_technical=len(remaining),remaining_ids=remaining,
                source=str(source),output=str(resolved_dir))
    js(resolved_dir/'report.json',report)
    return report


def run_subject(root,subject):
    d=root/subject
    if not (d/'done.json').is_file():return None
    stages=[resolve_stage(d,stage) for stage in ('audit','review')]
    return dict(subject=subject,stages=stages,mount=resolve_mount(d))


def resolve_mount(d):
    source=d/'remount/with_cards.jsonl'
    if not source.is_file():return dict(stage='mount',status='no_mount_output')
    rows=load_records(source)
    failed={r['record_id'] for r in rows if r['knowledge_labeling']['status']!='ok'}
    if not failed:return dict(stage='mount',initial_technical=0,remaining_technical=0)
    old=d/'technical_retry_alt_mount_excerpt_v1/summary.json'
    if old.is_file():
        report=json.loads(old.read_text())
        if report.get('remaining')==0 and report.get('review_technical')==0:
            return dict(stage='mount',initial_technical=len(failed),remaining_technical=0,reused=str(old))
    dest=d/'auto_retry_mount_excerpt';summary=dest/'summary.json'
    if summary.is_file():return json.loads(summary.read_text())
    items=[r for r in load_records(d/'remount/input.jsonl') if r['record_id'] in failed]
    if len(items)!=len(failed):raise ValueError('mount input IDs do not cover failures')
    cards_dir=complete_boundary_dir(d)
    if cards_dir is None:raise ValueError('no complete boundary cards for mount retry')
    tree=json.loads((cards_dir/'knowledge_tree.json').read_text())
    cards=load_records(cards_dir/'semantic_cards.jsonl')
    mounted=mount(mapping_excerpt_items(items),dest,tree,cards,d)
    checks,_=prepare_reviews(items,{'with_cards':mounted},tree)
    judged=review_batch(checks,d/'auto_retry_mount_review',chunk_size=80) if checks else {}
    report=dict(stage='mount',initial_technical=len(failed),remaining_technical=sum(r['knowledge_labeling']['status']!='ok' for r in mounted),
                reviewed=len(checks),review_technical=sum(r['judgment']=='technical_failure' for r in judged.values()),
                source=str(source),output=str(dest))
    js(summary,report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--poll-seconds',type=int,default=180)
    p.add_argument('--max-hours',type=float,default=18)
    a=p.parse_args();started=time.time();reports={};errors={}
    while time.time()-started<a.max_hours*3600:
        for subject in SUBJECTS:
            if subject in reports:continue
            try:
                result=run_subject(a.root,subject)
                if result is not None:reports[subject]=result;print(json.dumps(result,ensure_ascii=False),flush=True)
            except Exception as exc:
                errors[subject]=str(exc)
        js(a.root/'auto_resolve_reviews_status.json',dict(updated=time.time(),reports=reports,errors=errors))
        if len(reports)==len(SUBJECTS):break
        time.sleep(a.poll_seconds)

if __name__=='__main__':main()
