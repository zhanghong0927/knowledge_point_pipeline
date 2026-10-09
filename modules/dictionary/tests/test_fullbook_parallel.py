import concurrent.futures
import threading
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fullbook_llm_v2 import dispatch_ranges, Runner


class ParallelTests(unittest.TestCase):
    def test_runner_same_book_parallel_and_resume(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'source.md';path.write_text('One\n\nTwo\n',encoding='utf-8')
            args=SimpleNamespace(api_url='http://unused',tokenizer=None,workers=3,
                out=Path(root)/'out',model='test',context=100000,output_tokens=16000,
                overlap=0,timeout=10,server_context=None)
            runner=Runner(args);self.addCleanup(runner.close)
            barrier=threading.Barrier(3)
            runner.fits=lambda p:p['hi']-p['lo']<=1
            def api(*a):
                barrier.wait(timeout=3)
                return {'choices':[{'finish_reason':'stop','message':{'content':'{"entries":[],"scanned_all":true}'}}],
                        'usage':{'prompt_tokens':10,'completion_tokens':5}}
            runner.api=api
            spec={'identifier':'one','title':'One','md_path':str(path)}
            result=runner.run_book(spec)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['execution']['requests'],3)
            self.assertEqual(result['execution']['prompt_tokens'],30)
            self.assertEqual(runner.peak_requests,3)
            runner.api=lambda *a:self.fail('Resume must not call API')
            self.assertEqual(runner.run_book(spec)['execution']['cache_hits'],3)

    def test_dynamic_split_parallel_and_coverage(self):
        barrier=threading.Barrier(2)
        leaves=[]
        def process(lo,hi):
            if hi-lo>1:
                mid=(lo+hi)//2
                return [(lo,mid),(mid,hi)]
            barrier.wait(timeout=3)
            leaves.append(lo)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            dispatch_ranges(process,[(0,4)],pool,2)
        self.assertEqual(sorted(leaves),[0,1,2,3])

    def test_two_books_share_global_limit(self):
        lock=threading.Lock();active=0;peak=0
        def process(lo,hi):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
            time.sleep(.02)
            with lock:active-=1
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as requests:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as books:
                futures=[books.submit(dispatch_ranges,process,[(i,i+1) for i in range(8)],requests,3) for _ in range(2)]
                for f in futures:f.result()
        self.assertEqual(peak,3)

    def test_single_worker_does_not_deadlock(self):
        leaves=[]
        def process(lo,hi):
            if hi-lo>1:return [(lo,lo+1),(lo+1,hi)]
            leaves.append(lo)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            dispatch_ranges(process,[(0,3)],pool,1)
        self.assertEqual(leaves,[0,1,2])

    def test_worker_error_propagates(self):
        def process(*args):raise OSError('disk failed')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            with self.assertRaises(OSError):dispatch_ranges(process,[(0,1)],pool,2)


if __name__=='__main__':unittest.main()
