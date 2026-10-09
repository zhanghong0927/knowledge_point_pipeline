import socket,json,urllib.request
from pathlib import Path
ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
with socket.create_connection(('127.0.0.1',18764),timeout=5) as s:
    s.sendall(b'{"action":"status"}\n');print('GATE',s.recv(4096).decode())
for p in sorted(ROOT.glob('*/status.json')):
    r=json.loads(p.read_text());print(p.parent.name,r.get('stage'),r.get('error','')[-400:])
base='http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
with urllib.request.urlopen(base+'/metrics',timeout=10) as r:
    print('\n'.join(x for x in r.read().decode().splitlines() if x.startswith(('vllm:num_requests_running{','vllm:num_requests_waiting{'))))
