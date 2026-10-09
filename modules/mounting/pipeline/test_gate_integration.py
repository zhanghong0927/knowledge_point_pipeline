import asyncio,json,unittest
from shared_gate import Gate
class Integration(unittest.IsolatedAsyncioTestCase):
    async def test_cap_disconnect_and_priority(self):
        gate=Gate(2,1)
        server=await asyncio.start_server(gate.handle,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        async def connect(role):
            r,w=await asyncio.open_connection('127.0.0.1',port)
            w.write((json.dumps({'role':role})+'\n').encode());await w.drain();return r,w
        b,bw=await connect('background');self.assertEqual(await b.readline(),b'OK\n')
        p,pw=await connect('primary');self.assertEqual(await p.readline(),b'OK\n')
        q,qw=await connect('primary');await asyncio.sleep(.1)
        self.assertEqual(sum(gate.active.values()),2);self.assertEqual(gate.waiting['primary'],1)
        bw.close();await bw.wait_closed()
        self.assertEqual(await asyncio.wait_for(q.readline(),2),b'OK\n')
        pw.close();qw.close();await pw.wait_closed();await qw.wait_closed();await asyncio.sleep(.1)
        self.assertEqual(sum(gate.active.values()),0);self.assertEqual(gate.peak,2)
        server.close();await server.wait_closed()
