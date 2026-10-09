"""Independent subjects, one global request gate."""
import os,sys,json,time,subprocess,fcntl
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
def main():
    if os.environ.get('KNOWLEDGE_SHARED_GATE')!='1':raise RuntimeError('shared gate required')
    lock=(HERE/'endpoint.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    inventory=json.loads((ROOT/'inventory.json').read_text());children={};logs=[]
    for e in inventory:
        s=e['subject']
        if (ROOT/s/'done.json').exists():continue
        log=(ROOT/s/'parallel_worker.log').open('a');logs.append(log)
        children[s]=subprocess.Popen([sys.executable,'-u',str(HERE/'runner.py'),'subject','--out',str(ROOT),'--subject',s],stdout=log,stderr=subprocess.STDOUT)
    while True:
        states={s:{'pid':p.pid,'returncode':p.poll()} for s,p in children.items()}
        running=any(p.poll() is None for p in children.values())
        data=dict(stage='parallel_running' if running else 'parallel_finished',pid=os.getpid(),global_limit=1024,updated=time.time(),subjects=states)
        temp=ROOT/'status.parallel.tmp';temp.write_text(json.dumps(data,indent=2));temp.replace(ROOT/'status.json')
        if not running:break
        time.sleep(10)
    for log in logs:log.close()
if __name__=='__main__':main()
