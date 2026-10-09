"""Opt-in global permit hook; all children inherit it through PYTHONPATH."""
import os,socket,json,io,urllib.request,urllib.parse
_original=urllib.request.urlopen
_host='jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
class BufferedResponse(io.BytesIO):
    def __init__(self,data,response):
        super().__init__(data);self.status=response.status;self.headers=response.headers;self.url=response.geturl()
    def getcode(self):return self.status
    def geturl(self):return self.url
    def info(self):return self.headers
def gated_open(url,*args,**kwargs):
    address=url.full_url if isinstance(url,urllib.request.Request) else url
    parsed=urllib.parse.urlsplit(address)
    if parsed.hostname!=_host or parsed.path!='/v1/chat/completions':return _original(url,*args,**kwargs)
    with socket.create_connection(('127.0.0.1',18764),timeout=10) as lease:
        lease.settimeout(None)
        lease.sendall((json.dumps({'role':os.environ.get('KNOWLEDGE_REQUEST_ROLE','primary')})+'\n').encode())
        line=b''
        while not line.endswith(b'\n'):
            part=lease.recv(1)
            if not part:raise RuntimeError('shared gate disconnected before grant')
            line+=part
        if line!=b'OK\n':raise RuntimeError('shared gate refused request')
        with _original(url,*args,**kwargs) as response:return BufferedResponse(response.read(),response)
if os.environ.get('KNOWLEDGE_SHARED_GATE')=='1':urllib.request.urlopen=gated_open
