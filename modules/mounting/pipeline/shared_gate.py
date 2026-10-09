import asyncio,json,os
def eligible(role,total,background,waiting_primary,limit,bg_limit):
    return total < limit and (role == 'primary' or (background < bg_limit and waiting_primary == 0))

class Gate:
    def __init__(self,limit=1024,bg_limit=960):
        self.limit=limit;self.bg_limit=bg_limit
        self.active={'primary':0,'background':0};self.waiting={'primary':0,'background':0};self.peak=0
    async def handle(self,reader,writer):
        role=None;granted=False;queued=False
        try:
            req=json.loads(await asyncio.wait_for(reader.readline(),10))
            if req.get('action')=='status':
                writer.write((json.dumps(dict(active=self.active,waiting=self.waiting,peak=self.peak,limit=self.limit,pid=os.getpid()))+'\n').encode());await writer.drain();return
            role=req['role']
            if role not in self.active:raise ValueError('bad role')
            self.waiting[role]+=1;queued=True
            while not eligible(role,sum(self.active.values()),self.active['background'],self.waiting['primary'],self.limit,self.bg_limit):
                if reader.at_eof():return
                await asyncio.sleep(.05)
            self.waiting[role]-=1;queued=False
            self.active[role]+=1;granted=True;self.peak=max(self.peak,sum(self.active.values()))
            writer.write(b'OK\n');await writer.drain();await reader.read()
        finally:
            if queued:self.waiting[role]-=1
            if granted:self.active[role]-=1
            writer.close();await writer.wait_closed()
async def main():
    gate=Gate();server=await asyncio.start_server(gate.handle,'127.0.0.1',18764,backlog=4096)
    async with server:await server.serve_forever()
if __name__=='__main__':asyncio.run(main())
