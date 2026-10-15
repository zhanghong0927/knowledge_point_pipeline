"""Finish native mounting/dedup from isolated cleaning exports."""
import json
from pathlib import Path
import staged_benchmark as bench
from benchmark_support import Recorder
from stage_scheduling import run_simple_phase

def finish_dictionary(root,cleaned,service,gateway,profile):
    root=Path(root);recorder=Recorder(root,profile)
    subjects=sorted({j['subject'] for j in cleaned})
    boundaries,cache=bench.generate_boundaries(root,subjects,service,gateway,profile)
    bench.save(root/'dictionary/BOUNDARY_RESULTS.json',boundaries)
    mounts=bench.prepare_mounts(root,'dictionary',cleaned,service,gateway,profile,boundaries,cache)
    mount_prep=run_simple_phase(mounts,lambda j:bench.selected_job(j,'05','prepare_mounting'),profile.mount_workers)
    bench.save(root/'dictionary/MOUNT_PREPARATION.json',{'results':[
      {'job_id':j['job_id'],**r} for j,r in zip(mounts,mount_prep)]})
    prepared=[j for j,r in zip(mounts,mount_prep) if r['status']=='completed']
    with_boundaries,boundary_rounds=bench.run_mount_transport(root,'dictionary',gateway,profile,recorder,prepared,'generate_mounting_boundaries')
    verified=run_simple_phase(with_boundaries,lambda j:bench.selected_job(j,'05','verify_mounting_boundaries'),profile.mount_workers)
    ready=[j for j,r in zip(with_boundaries,verified) if r['status']=='completed']
    routed,route_rounds=bench.run_mount_transport(root,'dictionary',gateway,profile,recorder,ready,'route_mounting')
    reviewed,review_rounds=bench.run_mount_transport(root,'dictionary',gateway,profile,recorder,routed,'review_mounting')
    exported=run_simple_phase(reviewed,lambda j:bench.selected_job(j,'05','export_mounting'),profile.mount_workers)
    ready=[j for j,r in zip(reviewed,exported) if r['status']=='completed']
    dedup=run_simple_phase(ready,lambda j:bench.selected_job(j,'06','dedup','verify_dedup'),profile.mount_workers)
    totals=bench.final_output_totals(ready)
    transport,dispositions,semantic=bench.mounting_dispositions(ready)
    errors=sum(bench.count_jsonl(Path(j['out'])/'TECHNICAL_FAILURES.jsonl') for j in cleaned)
    unresolved=['original_extraction_partial_and_2_frozen_books']
    if errors:unresolved.append('cleaning_technical_failures')
    if any(v.get('status')!='completed' for v in boundaries.values()):unresolved.append('taxonomy_boundaries')
    if not mounts:unresolved.append('no_mounting_input')
    if len(prepared)!=len(mounts):unresolved.append('mounting_prepare')
    if len(ready)!=len(mounts) or transport or semantic or any(dispositions.get(k,0) for k in ['review','technical_failure','unconfigured','missing_summary']):unresolved.append('mounting_route_review')
    if totals.get('missing_exports') or any(r['status']!='completed' for r in dedup):unresolved.append('dedup_export')
    return {'track':'dictionary','status':'finished_with_unresolved' if unresolved else 'completed_native_stages',
      'source_root':str(bench.ROOT.parent.parent/'pipeline_staged_30books_16card_20261012_01'),
      'selected_books':30,'source_verified_extraction_books':28,'completed_cleaning_books':len(cleaned),
      'cleaning_technical_errors':errors,'mounting_jobs':len(mounts),'prepared_mounting_jobs':len(prepared),'completed_mounting_subjects':len(ready),
      'mounting_transport_unresolved':transport,'mounting_dispositions':dispositions,
      'unresolved_stages':unresolved,'semantic_quality_approved':False,'important_successor_separate':True,**totals}
