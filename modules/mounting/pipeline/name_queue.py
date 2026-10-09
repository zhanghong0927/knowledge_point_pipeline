"""Append a sequential stage-one cleaning queue after the mount queue releases its lock."""
import os,json,sys,time,hashlib,shutil,subprocess,importlib.util
from pathlib import Path
def verify_report(report,n):
    if report['records']!=n or report['completed']!=n or sum(report['counts'].values())!=n:raise ValueError('cleaning coverage mismatch')
def can_follow(stage):return stage in ('completed','finished_with_issues')
HERE=Path(__file__).resolve().parent
ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
SCRIPT=Path('/home/wangqiyuan/work/name_clean_benchmark_20260922/package/01_name_title_quality.py')
BENCH=Path('/home/wangqiyuan/work/name_clean_benchmark_20260922/results')
BASE='http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
MODEL='/mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B'
def write(p,data):
    temp=p.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2));temp.replace(p)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    import fcntl
    # Separate guard prevents two followers from overwriting each other's waiting status.
    guard=(HERE/'name_queue.lock').open('a');fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
    root=ROOT/'name_cleaning';root.mkdir(exist_ok=True)
    def status(stage,**kw):write(ROOT/'name_cleaning_status.json',dict(stage=stage,pid=os.getpid(),updated=time.time(),endpoint=BASE,workers=1024,batch_size=10,context_chars=0,**kw))
    if os.environ.get('KNOWLEDGE_SHARED_GATE')=='1':status('running_cooperative')
    else:
        status('waiting_for_mount_queue')
        lock=(HERE/'endpoint.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
        previous=json.loads((ROOT/'status.json').read_text())
        if not can_follow(previous['stage']):status('blocked',reason='mount queue did not finish normally',mount_status=previous);return
    spec=importlib.util.spec_from_file_location('cleaner',SCRIPT);cleaner=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleaner)
    inventory=json.loads((ROOT/'inventory.json').read_text());summary=[]
    for e in inventory:
        s=e['subject'];dest=root/s;source=ROOT/s/'source.snapshot'
        assert sha(source)==e['source_sha256'],'snapshot changed'
        status('cleaning',subject=s,finished_subjects=len(summary))
        try:
            if (dest/'verified.json').exists():
                done=json.loads((dest/'verified.json').read_text());assert done['source_sha256']==sha(source);summary.append(done);continue
            reuse_config=json.loads((BENCH/'config.json').read_text())
            if s=='mechanical_engineering' and not dest.exists() and sha(source)==reuse_config['source_sha256'] and sha(SCRIPT)==reuse_config['script_sha256']:
                shutil.copytree(BENCH/'1024',dest)
                write(dest/'reuse.json',dict(source=str(BENCH/'1024'),original_endpoint=reuse_config['endpoint'],source_and_script_hash_verified=True))
            else:
                dest.mkdir(exist_ok=True)
                cfg=dict(source_sha256=sha(source),script_sha256=sha(SCRIPT),workers=1024,batch_size=10,context_chars=0,endpoint=BASE,model=MODEL)
                if (dest/'run_config.json').exists():assert json.loads((dest/'run_config.json').read_text())==cfg,'resume config changed'
                write(dest/'run_config.json',cfg)
                cmd=[sys.executable,str(SCRIPT),'--input',str(source),'--out-dir',str(dest),'--api-url',BASE+'/v1/chat/completions','--model',MODEL,'--no-auth','--workers','1024','--batch-size','10','--context-chars','0','--resume']
                write(dest/'command.json',cmd)
                with (dest/'run.log').open('a') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
            report=json.loads((dest/'llm_name_title_format_report.json').read_text());verify_report(report,e['records'])
            expected={cleaner.record_key(r) for r in cleaner.build_records(source,0,0,'auto',0)}
            rows=[json.loads(x) for x in (dest/'llm_name_title_format_judgments.jsonl').read_text().splitlines() if x.strip()]
            actual=[cleaner.record_key(r) for r in rows];assert len(actual)==e['records'] and set(actual)==expected
            done=dict(subject=s,records=e['records'],source_sha256=sha(source),counts=report['counts'],technical_unresolved=report['pending'],coverage_ok=True,reused=(dest/'reuse.json').exists())
            write(dest/'verified.json',done);summary.append(done)
            if report['pending']>e['records']*.5:write(root/'summary.json',summary);status('blocked',subject=s,reason='technical failure rate over 50 percent');return
        except Exception as ex:
            summary.append(dict(subject=s,error=str(ex),status='blocked'))
        write(root/'summary.json',summary)
    status('completed_with_issues' if any(r.get('error') or r.get('technical_unresolved') for r in summary) else 'completed',finished_subjects=len(summary))
if __name__=='__main__':main()
