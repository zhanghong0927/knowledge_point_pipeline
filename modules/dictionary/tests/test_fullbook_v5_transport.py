import argparse
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

import fullbook_llm_v5 as v5


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        md = self.root / 'book.md'
        md.write_text('Alpha means a thing.\n')
        self.spec = dict(identifier='test', title='Test', md_path=str(md))
        self.calls = []

    def run_round(self, number, fail=False):
        args = argparse.Namespace(api_url='http://unused', model='test', tokenizer=None,
            context=100000, output_tokens=16000, overlap=4, workers=2, timeout=3,
            server_context=None, out=self.root/'out', attempts=3, transport_round=number)
        runner = v5.Runner(args)
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                return [0] * 100
        runner.tokenizer = Tokenizer()
        runner.backoff = lambda *a: None
        def api(*a):
            self.calls.append(a)
            if hasattr(self,'responder'):return self.responder(*a)
            if fail:
                raise HTTPError('http://unused', 504, 'Gateway Timeout', {}, None)
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                {'scanned_all':True, 'entries':[]})}}]}
        runner.api = api
        try:
            return runner.run_book(self.spec)
        finally:
            runner.close()

    def test_same_round_does_not_retry_and_second_round_recovers(self):
        first = self.run_round(1, True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first['http504_pending_chunks'], 1)
        self.run_round(1, True)
        self.assertEqual(len(self.calls), 1)
        second = self.run_round(2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['http504_unresolved_chunks'], 0)
        self.run_round(3)
        self.assertEqual(len(self.calls), 2)

    def test_three_failures_exhaust_without_compounded_retry(self):
        for number in (1, 2, 3):
            result = self.run_round(number, True)
            self.assertEqual(len(self.calls), number)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['http504_pending_chunks'], 0)
        self.assertEqual(result['http504_unresolved_chunks'], 1)
        self.run_round(3, True)
        self.assertEqual(len(self.calls), 3)

    def test_repair_timeout_keeps_valid_neighbor_and_only_retries_repair(self):
        good={'head':[{'unit':0,'quote':'Alpha'}],
            'knowledge_point':[{'unit':0,'quote':'Alpha'}],'name':[],
            'body':[{'start':{'unit':0,'quote':'means'},'end':{'unit':0,'quote':'thing.'}}],
            'body_complete':True}
        bad={**good,'head':[{'unit':0,'quote':'NOT IN SOURCE'}]}
        def response(data):
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
        def api(*a):
            if len(self.calls)==1:return response({'scanned_all':True,'entries':[good,bad]})
            if len(self.calls)==2:raise HTTPError('http://unused',504,'Gateway Timeout',{},None)
            request=json.loads(a[-1]['messages'][1]['content'])
            rid=request['repair_tasks'][0]['repair_id']
            return response({'repairs':[{'repair_id':rid,'entry':None,'reason':'No source head'}]})
        self.responder=api
        first=self.run_round(1)
        self.assertEqual(first['entries'],1)
        self.assertEqual(first['status'],'partial')
        self.assertEqual(len(self.calls),2)
        second=self.run_round(2)
        self.assertEqual(second['entries'],1)
        self.assertEqual(second['status'],'completed')
        self.assertEqual(len(self.calls),3)

    def test_empty_object_is_bounded_not_silently_successful(self):
        self.responder=lambda *a: {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        result=self.run_round(1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['http504_pending_chunks'],0)
        self.assertEqual(len(self.calls),3)
        self.run_round(2)
        self.assertEqual(len(self.calls),3)

    def test_unchanged_bad_location_is_not_reported_as_model_rename(self):
        Path(self.spec['md_path']).write_text('Earlier Alpha means a thing.\n')
        bad={'head':[{'unit':0,'quote':'Alpha'}],
             'knowledge_point':[{'unit':0,'quote':'Alpha'}],'name':[],
             'body':[],'body_complete':True}
        def response(data):
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
        def api(*args):
            request=json.loads(args[-1]['messages'][1]['content'])
            if 'repair_tasks' not in request:
                return response({'scanned_all':True,'entries':[bad]})
            rid=request['repair_tasks'][0]['repair_id']
            return response({'repairs':[{'repair_id':rid,'entry':bad,'reason':'unchanged'}]})
        self.responder=api
        result=self.run_round(1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['entries'],0)
        caches=list((self.root/'out').glob('*/chunks/*.json'))
        self.assertEqual(len(caches),1)
        pending=json.loads(caches[0].read_text())['unresolved']
        self.assertEqual(len(pending),1)
        self.assertIn('source validation failed',pending[0]['error'])
        self.assertNotIn('changed candidate identity',pending[0]['error'])


if __name__ == '__main__':
    unittest.main()
