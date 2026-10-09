"""One-time guarded transition; no delivery files are modified."""
import os,signal,time,json,subprocess,sys,urllib.request,socket
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
BASE='http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
def main():
    for pid,needle in [(2405994,str(HERE/'runner.py')+' run'),(2412601,str(HERE/'name_queue.py'))]:
        p=Path('/proc')/str(pid)/'cmdline'
        if not p.exists():continue
        cmd=p.read_bytes().replace(b'\0',b' ').decode()
        if needle not in cmd:raise RuntimeError(('PID identity changed',pid))
        children=subprocess.check_output(['pgrep','-P',str(pid)],text=True) if subprocess.run(['pgrep','-P',str(pid)],capture_output=True).returncode==0 else ''
        if children.strip():raise RuntimeError(('active children: recheck before switch',pid,children))
        os.kill(pid,signal.SIGTERM)
    for attempt in range(60):
        with urllib.request.urlopen(BASE+'/metrics',timeout=10) as r:lines=r.read().decode().splitlines()
        active=[float(x.rsplit(' ',1)[1]) for x in lines if x.startswith(('vllm:num_requests_running{','vllm:num_requests_waiting{'))]
        if active and sum(active)==0:break
        time.sleep(2)
    else:raise RuntimeError('upstream did not drain; workloads not started')
    env=os.environ.copy();env.update(PYTHONPATH=str(HERE),KNOWLEDGE_SHARED_GATE='1',KNOWLEDGE_REQUEST_ROLE='primary')
    log=(ROOT/'shared_gate.log').open('a')
    broker=subprocess.Popen([sys.executable,'-u',str(HERE/'shared_gate.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    time.sleep(1)
    if broker.poll() is not None:raise RuntimeError('broker failed')
    with socket.create_connection(('127.0.0.1',18764),timeout=5) as s:
        s.sendall(b'{"action":"status"}\n');print(s.recv(4096).decode(),flush=True)
    p=subprocess.Popen([sys.executable,'-u',str(HERE/'parallel_queue.py')],env=env,stdout=(ROOT/'parallel_queue.log').open('a'),stderr=subprocess.STDOUT,start_new_session=True)
    env['KNOWLEDGE_REQUEST_ROLE']='background'
    n=subprocess.Popen([sys.executable,'-u',str(HERE/'name_queue.py')],env=env,stdout=(ROOT/'name_queue.log').open('a'),stderr=subprocess.STDOUT,start_new_session=True)
    result=dict(broker_pid=broker.pid,parallel_pid=p.pid,name_pid=n.pid,started=time.time(),global_limit=1024)
    (ROOT/'parallel_launch.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
if __name__=='__main__':main()
