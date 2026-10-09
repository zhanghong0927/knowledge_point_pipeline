"""Replay one failed boundary payload read-only to capture the API error body."""
import json,sys,urllib.request,urllib.error
from pathlib import Path
from generate_semantic_boundaries import PROMPT
source=Path(sys.argv[1]);payload=json.loads(source.read_text())['payload']
body=dict(model='/mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B',temperature=0,max_tokens=int(sys.argv[2]) if len(sys.argv)>2 else 12000,
    chat_template_kwargs={'enable_thinking':False},messages=[dict(role='system',content=PROMPT),dict(role='user',content=json.dumps(payload,ensure_ascii=False))])
encoded=json.dumps(body,ensure_ascii=False).encode()
print('request_bytes',len(encoded),flush=True)
req=urllib.request.Request('http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com/v1/chat/completions',data=encoded,headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=45) as response:print('status',response.status,'reply_bytes',len(response.read()),flush=True)
except urllib.error.HTTPError as error:
    print('status',error.code,'body',error.read(4000).decode(errors='replace'),flush=True)
