"""One-shot, checkpointed closeout of blocked subject runs.

It retries only blocked subjects, preserves failed boundary checkpoints in backups,
and never changes official delivery. Active workers are left alone.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from runner import SUBJECTS, complete_boundary_dir, js

HERE=Path(__file__).resolve().parent
DEFAULT_ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
ALT='http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'


def boundary_dir(d):
    for name in ('boundaries_v6_alt','boundaries_v5','boundaries'):
        p=d/name
        if (p/'config.json').is_file():return p
    return None


def resume_command(run):
    c=json.loads((run/'config.json').read_text())
    cmd=[sys.executable,str(HERE/'generate_semantic_boundaries.py'),'--tree',c['tree'],'--out',str(run),
         '--base',c['base'],'--model',c['model'],'--workers',str(c['workers']),
         '--max-depth',str(c['max_depth']),'--max-context-bytes',str(c['max_context_bytes']),
         '--review-mode',c['review_mode'],'--cross-review',c['cross_review'],'--resume']
    if c.get('reuse_run'):cmd+=['--reuse-run',c['reuse_run']]
    return cmd,c


def run_command(cmd, log, env=None):
    with log.open('a') as f:return subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,env=env).returncode


def close_subject(root, subject, attempts):
    d=root/subject
    if (d/'done.json').is_file():return 'done'
    status_file=d/'status.json'
    if not status_file.is_file():return 'waiting'
    status=json.loads(status_file.read_text())
    if status.get('stage')!='blocked':return 'waiting'
    if attempts.get(subject,0)>=3:return 'exhausted'
    attempts[subject]=attempts.get(subject,0)+1
    log=d/'automatic_closeout.log'
    if not complete_boundary_dir(d):
        run=boundary_dir(d)
        if run is None:return 'no_boundary_run'
        c=json.loads((run/'config.json').read_text())
        env=dict(os.environ,BOUNDARY_MAX_TOKENS=str(c['max_output_tokens']),
                 BOUNDARY_HTTP_TIMEOUT=str(c['http_timeout_seconds']))
        recover=[sys.executable,str(HERE/'recover_boundary_failures.py'),'--run',str(run),'--base',ALT,'--workers','32']
        if run_command(recover,log,env):return 'recovery_error'
        cmd,_=resume_command(run)
        if run_command(cmd,log,env):return 'resume_error'
        if not complete_boundary_dir(d):return 'boundary_incomplete'
    cmd=[sys.executable,str(HERE/'runner.py'),'subject','--out',str(root),'--subject',subject]
    return 'done' if run_command(cmd,log)==0 else 'subject_error'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    p.add_argument('--poll-seconds',type=int,default=120)
    p.add_argument('--max-hours',type=float,default=18)
    a=p.parse_args();attempts={};start=time.time()
    report_file=a.root/'automatic_closeout_status.json'
    while time.time()-start<a.max_hours*3600:
        outcomes={s:close_subject(a.root,s,attempts) for s in SUBJECTS}
        js(report_file,dict(updated=time.time(),attempts=attempts,outcomes=outcomes))
        print(json.dumps(outcomes,ensure_ascii=False),flush=True)
        if all(v in ('done','exhausted','no_boundary_run') for v in outcomes.values()):break
        time.sleep(a.poll_seconds)

if __name__=='__main__':main()
