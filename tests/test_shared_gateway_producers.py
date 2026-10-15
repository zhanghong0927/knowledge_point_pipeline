"""Real loopback HTTP and subprocess clients; never calls a model service."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import web, ClientSession
from adapters.inference_gateway import create_app

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json,os,sys
from urllib.request import Request,urlopen
from shared_request_pool import completed
origin,job,count,output=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]
def work(n):
    req=Request(origin+'/_jobs/'+job+'/v1/chat/completions',data=json.dumps({'n':n}).encode(),headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=10) as r:return json.load(r)['n']
with open(output,'w') as f:
    for _,future in completed(work,range(count),1024):
        f.write(json.dumps(future.result())+'\n');f.flush();os.fsync(f.fileno())
print(json.dumps({'records':count}))
'''


class SharedGatewayProducerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runners=[]; self.session=ClientSession(); self.active=self.peak=0
        async def handler(request):
            self.active+=1; self.peak=max(self.peak,self.active)
            value=await request.json()
            await asyncio.sleep(.025)
            self.active-=1
            return web.json_response(value)
        app=web.Application();app.router.add_post('/v1/chat/completions',handler)
        upstream=await self.serve(app)
        self.url=await self.serve(create_app(upstream,limit=4))

    async def serve(self,app):
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        self.runners.append(runner)
        return 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        await self.session.close()
        for runner in reversed(self.runners):await runner.cleanup()

    async def snapshot(self):
        async with self.session.get(self.url+'/_scheduler/stats') as r:return await r.json()

    async def child(self,job,count,path):
        env={**os.environ,'PIPELINE_SHARED_POOL_URL':self.url,'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'}
        return await asyncio.create_subprocess_exec(sys.executable,'-B','-c',CHILD,self.url,job,str(count),str(path),
                                                   env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)

    async def test_two_book_processes_share_one_cap_and_tail_uses_full_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            a=await self.child('a',3,Path(td)/'a.jsonl')
            b=await self.child('b',30,Path(td)/'b.jsonl')
            outputs=await asyncio.wait_for(asyncio.gather(a.communicate(),b.communicate()),20)
            self.assertEqual((a.returncode,b.returncode),(0,0),outputs)
            self.assertEqual(len((Path(td)/'a.jsonl').read_text().splitlines()),3)
            self.assertEqual(len((Path(td)/'b.jsonl').read_text().splitlines()),30)
        stats=await self.snapshot()
        self.assertEqual(self.peak,4)
        self.assertEqual(stats['jobs']['b']['total']['peak_active'],4)
        self.assertEqual(stats['producer_pool']['active'],0)
        self.assertEqual(stats['producer_pool']['owners'],{})
        self.assertEqual(stats['producer_pool']['waiting'],0)

    async def test_next_global_round_reduces_shared_cap_for_all_children(self):
        async with self.session.post(self.url+'/_scheduler/control',json={'limit':1,'phase':'retry_3'}) as r:
            self.assertEqual(r.status,200)
        with tempfile.TemporaryDirectory() as td:
            a=await self.child('a',4,Path(td)/'a.jsonl')
            b=await self.child('b',4,Path(td)/'b.jsonl')
            outputs=await asyncio.wait_for(asyncio.gather(a.communicate(),b.communicate()),20)
            self.assertEqual((a.returncode,b.returncode),(0,0),outputs)
        self.assertEqual(self.peak,1)
        self.assertEqual((await self.snapshot())['producer_pool']['active'],0)


if __name__=='__main__':unittest.main()
