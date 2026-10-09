"""Real CLI/HTTP transport tests using only a synthetic localhost model stub."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]


@contextmanager
def model_stub():
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(payload)
            if self.path!='/v1/chat/completions':
                self.send_error(404); return
            if len(received)<3:
                self.send_error(504,'Synthetic test timeout'); return
            task=json.loads(payload['messages'][1]['content'])
            reply={'group_id':task['group_id'],'clusters':[{'member_ids':['1','2'],
                   'canonical_record_id':'2','reason':'Synthetic transport test chooses record 2'}],'singletons':[]}
            body=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(reply)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield 'http://127.0.0.1:'+str(server.server_port),received
    finally: server.shutdown(); server.server_close(); thread.join()


class CliTests(unittest.TestCase):
    def run_cli(self,args):
        result=subprocess.run([sys.executable,str(ROOT/'dedup.py'),*map(str,args)],
                              cwd=ROOT,capture_output=True,text=True,encoding='utf-8',
                              env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_length_cli_preserves_same_name_in_another_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'length output'
            result=self.run_cli(['run','--input',ROOT/'examples/input.json','--subject','education',
                                 '--mode','length','--out',out])
            self.assertEqual(result['input_records'],3)
            self.assertEqual(result['removed_records'],1)
            self.assertTrue(self.run_cli(['verify','--out',out])['original_payloads_unchanged'])

    def test_llm_cli_real_local_transport_retries_and_resumes_without_call(self):
        with tempfile.TemporaryDirectory() as tmp,model_stub() as (url,received):
            folder=Path(tmp); source=folder/'synthetic input.json'; out=folder/'model output'
            rows=[{'id':'1','name':'same','knowledge_point':'term','main_tags':'root/a',
                   'definition':'longer but not better','en_definition':'English original',
                   'description':'Original description','source':{'book_id':'synthetic'}},
                  {'id':'2','name':'same','knowledge_point':'term','main_tags':'root/a',
                   'definition':'precise','description':'Short original description'},
                  {'id':'3','name':'same','main_tags':'root/b','definition':'other branch'}]
            source.write_text(json.dumps(rows),encoding='utf-8')
            args=['run','--input',source,'--subject','test','--mode','llm','--out',out,
                  '--api-url',url,'--model','synthetic-model','--workers','4','--retries','2']
            result=self.run_cli(args)
            self.assertEqual(len(received),3)
            self.assertEqual(result['model_attempts_this_run'],3)
            self.assertEqual(result['technical_failure_groups'],0)
            self.assertEqual(json.loads(received[0]['messages'][1]['content'])['members'],rows[:2])
            kept=json.loads((out/'retained.json').read_text(encoding='utf-8'))
            self.assertEqual(kept,rows[1:])
            resumed=self.run_cli([*args,'--resume'])
            self.assertEqual(len(received),3)
            self.assertEqual(resumed['reused_groups'],1)
            self.assertTrue(self.run_cli(['verify','--out',out])['selection_policy_consistent'])


if __name__=='__main__': unittest.main()
