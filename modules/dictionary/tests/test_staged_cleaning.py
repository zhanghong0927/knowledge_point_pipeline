"""Cohort round contracts for native dictionary cleaning."""
import io
import argparse
import importlib.util
import json
import contextlib
from pathlib import Path
import sys
import tempfile
import types
import urllib.error
import unittest
from unittest import mock

import portable_pipeline as p


class StagedCleaningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.md = self.root/'book.md'
        self.md.write_text('# A\nA body', encoding='utf-8')
        self.scope = self.root/'scope.json'
        self.scope.write_text(json.dumps({'subject':'Test','l1_nodes':[]}), encoding='utf-8')
        self.books = [{'identifier':'B001','title':'Test','md_path':str(self.md),
                       'subject_slug':'test','scope_config':str(self.scope)}]
        self.input = self.root/'INPUT.json'
        p.write(self.input,[{'id':'a','knowledge_point':'A','name':'','raw_content':'A body',
                             'source':{'identifier':'B001'}}])
        self.out = self.root/'native'

    def tearDown(self):
        self.tmp.cleanup()

    def test_filter_retries_only_technical_and_new_downstream_ids(self):
        values = [{'id':key,'knowledge_point':key,'name':''} for key in ('a','b','c')]
        calls = []
        def invoke(cmd, **_):
            target = Path(cmd[cmd.index('--out-dir')+1])
            subset = p.loadl(target/'input.jsonl')
            calls.append(([r['id'] for r in subset], cmd[cmd.index('--retries')+1]))
            result = []
            for row in subset:
                status = 'api_error' if row['id']=='b' and len(calls)==1 else 'ok'
                decision = 'review' if row['id']=='c' or row['id']=='b' and len(calls)==3 else 'keep'
                result.append({**row,p.NAME:{'decision':decision,
                                             'api_status':status,
                                             'failure_kind':'transport' if status=='api_error' else ''}})
            p.jsonl(target/'llm_name_title_format_all_full.jsonl',result)
        cfg = {'api_url':'http://unused','model':'test','workers':64,'staged_round':1}
        with mock.patch.object(p.subprocess,'run',side_effect=invoke):
            first = p.filter_stage(values,'name',cfg,self.out/'name')
            self.assertEqual(first[1][p.NAME]['api_status'],'api_error')
            cfg['staged_round'] = 2
            cfg['workers'] = 16
            second = p.filter_stage(values,'name',cfg,self.out/'name')
        self.assertEqual(calls,[(['a','b','c'],'0'),(['c'],'0'),(['b'],'0'),(['b'],'0')])
        self.assertEqual([r['id'] for r in second],[r['id'] for r in values])
        self.assertEqual(second[1][p.NAME]['api_status'],'ok')
        self.assertEqual(second[1][p.NAME]['decision'],'keep')
        self.assertEqual(second[2][p.NAME]['decision'],'review')

    def test_context_transport_retry_does_not_repeat_successful_first_vote(self):
        row = {'id':'a','knowledge_point':'A','name':''}
        calls = []
        def invoke(cmd, **_):
            target = Path(cmd[cmd.index('--out-dir')+1])
            context = int(cmd[cmd.index('--context-chars')+1])
            calls.append(context)
            failed = len(calls)==2
            p.jsonl(target/'llm_name_title_format_all_full.jsonl',
                    [{**row,p.NAME:{'decision':'review' if context==0 or failed else 'keep',
                                     'api_status':'api_error_after_retries' if failed else 'ok',
                                     'failure_kind':'transport' if failed else ''}}])
        cfg = {'api_url':'http://unused','model':'test','workers':64,'staged_round':1}
        with mock.patch.object(p.subprocess,'run',side_effect=invoke):
            first = p.filter_stage([row],'name',cfg,self.out/'name')
            self.assertEqual(first[0][p.NAME]['failure_kind'],'transport')
            cfg['staged_round']=2
            second = p.filter_stage([row],'name',cfg,self.out/'name')
        self.assertEqual(calls,[0,600,600])
        self.assertEqual(p.loadl(self.out/'name/FIRST.jsonl')[0][p.NAME]['decision'],'review')
        self.assertEqual(second[0][p.NAME]['decision'],'keep')

    def test_format_or_unknown_failure_does_not_enter_transport_round(self):
        values = [{'id':key,'knowledge_point':key,'name':''} for key in ('format','unknown')]
        calls = []
        def invoke(cmd, **_):
            target = Path(cmd[cmd.index('--out-dir')+1])
            subset = p.loadl(target/'input.jsonl')
            calls.append([r['id'] for r in subset])
            p.jsonl(target/'llm_name_title_format_all_full.jsonl',[
                {**r,p.NAME:{'decision':'review','api_status':'invalid_field_results',
                             'failure_kind':'content_or_format' if r['id']=='format' else ''}}
                for r in subset])
        cfg = {'api_url':'http://unused','model':'test','workers':64,'staged_round':1}
        with mock.patch.object(p.subprocess,'run',side_effect=invoke):
            p.filter_stage(values,'name',cfg,self.out/'name')
            cfg['staged_round']=2
            p.filter_stage(values,'name',cfg,self.out/'name')
        self.assertEqual(calls,[['format','unknown']])

    def test_native_filters_label_only_transient_wire_failures_retryable(self):
        def load(name,filename):
            spec = importlib.util.spec_from_file_location(name,p.ROOT/'name_filters'/filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        name = load('staged_name_filter','01_name_title_quality.py')
        scope = load('staged_scope_filter','02_subject_scope_filter.py')
        args = argparse.Namespace(model='test',context_chars=0,temperature=0,max_tokens=100,
                                  disable_thinking=True,no_auth=True,api_url='http://unused',
                                  retries=0,timeout=1)
        name_row = {'id':'a','knowledge_point':'A','name':''}
        scope_row = {'request_id':'a','id':'a','knowledge_point':'A','name':'',
                     'source_row':name_row}
        cases = [(lambda *a,**k: io.BytesIO(b'not-json'),'content_or_format'),
                 (lambda *a,**k: (_ for _ in ()).throw(urllib.error.HTTPError('http://unused',413,'too large',{},io.BytesIO(b''))),'content_or_format'),
                 (lambda *a,**k: (_ for _ in ()).throw(TimeoutError('timed out')),'transport')]
        for wire,expected in cases:
            with self.subTest(expected=expected,wire=wire):
                with mock.patch.object(name.urllib.request,'urlopen',side_effect=wire):
                    _,result,_ = name.call_api('batch',[name_row],args)
                self.assertEqual(result[0]['failure_kind'],expected)
                with mock.patch.object(scope.urllib.request,'urlopen',side_effect=wire):
                    _,result,_ = scope.call_api('batch',[scope_row],args,
                                                {'subject':'Test'},'digest','prompt')
                self.assertEqual(result[0]['failure_kind'],expected)

    def test_rounds_preserve_success_and_require_order_before_finalize(self):
        import clean_boundary_v46 as content
        calls = []
        def stage(values, label, *_):
            calls.append((label,[r['id'] for r in values]))
            key = p.NAME if label=='name' else p.SCOPE
            fail = label=='name' and len(calls)==1
            return [{**r,key:{'decision':'review' if fail else 'keep',
                              'api_status':'api_error' if fail else 'ok',
                              'failure_kind':'transport' if fail else ''}} for r in values]
        def batch(values, *_):
            calls.append(('content',[r['id'] for r in values]))
            return [{'id':r['id'],'status':'keep','record':r} for r in values]
        config = {'api_url':'http://unused','model':'test','workers':64,'no_auth':True}
        lock_module = types.SimpleNamespace(book_lock=lambda _: contextlib.nullcontext())
        with mock.patch.dict(sys.modules,{'fullbook_llm_v2':lock_module}), \
             mock.patch.object(p.urllib.request,'urlopen',side_effect=lambda *a,**k: io.BytesIO(b'{"data":[{"id":"test"}]}')), \
             mock.patch.object(p,'filter_stage',side_effect=stage), \
             mock.patch.object(content.Runner,'batch',side_effect=batch):
            status = p.run_cleaning_round(self.input,config,self.out,self.books,1)
            self.assertEqual(status['state'],'awaiting_retry')
            self.assertEqual(status['next_round'],2)
            with self.assertRaises(ValueError):
                p.finalize_cleaning(self.input,config,self.out,self.books)
            config['workers'] = 16
            status = p.run_cleaning_round(self.input,config,self.out,self.books,2)
        self.assertEqual(status['state'],'ready_to_finalize')
        self.assertEqual(calls,[('name',['a']),('content',[]),('name',['a']),
                                ('scope',['a']),('content',['a'])])
        with mock.patch.object(p.urllib.request,'urlopen',side_effect=AssertionError('finalize requested model')):
            p.finalize_cleaning(self.input,config,self.out,self.books)
        self.assertEqual(p.read(self.out/'STATE.json')['status'],'finished')

    def test_content_transport_failure_retries_next_round_once(self):
        import clean_boundary_v46 as content
        cfg = {'api_url':'http://unused','model':'test','workers':2,'staged_round':1}
        (self.out/'content').mkdir(parents=True)
        runner = content.Runner(self.out/'content',cfg)
        response = b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"value\\": 1}"}}]}'
        transmissions = []
        def transport(*_,**__):
            transmissions.append(True)
            if len(transmissions)==1:
                raise ConnectionError('temporary transport loss')
            return io.BytesIO(response)
        def work(row):
            reply = runner.call(row['id'],'explanation',{'id':row['id']},'prompt',lambda v: v['value'])
            return {'id':row['id'],'status':'keep' if reply['status']=='ok' else 'failed'}
        with mock.patch.object(content.urllib.request,'urlopen',side_effect=transport):
            self.assertEqual(runner.batch([{'id':'a'}],'results',work)[0]['status'],'failed')
            self.assertEqual(len(transmissions),1)
            cfg['staged_round']=2
            self.assertEqual(runner.batch([{'id':'a'}],'results',work)[0]['status'],'keep')
            self.assertEqual(runner.batch([{'id':'a'}],'results',work)[0]['status'],'keep')
        self.assertEqual(len(transmissions),2)

    def test_exhausted_api_failure_remains_unresolved(self):
        import clean_boundary_v46 as content
        lock_module = types.SimpleNamespace(book_lock=lambda _: contextlib.nullcontext())
        def failed_name(values, label, *_):
            self.assertEqual(label,'name')
            return [{**r,p.NAME:{'decision':'review','api_status':'api_error',
                                  'failure_kind':'transport'}} for r in values]
        cfg = {'api_url':'http://unused','model':'test','workers':64,'no_auth':True}
        with mock.patch.dict(sys.modules,{'fullbook_llm_v2':lock_module}), \
             mock.patch.object(p.urllib.request,'urlopen',side_effect=lambda *a,**k: io.BytesIO(b'{"data":[{"id":"test"}]}')), \
             mock.patch.object(p,'filter_stage',side_effect=failed_name), \
             mock.patch.object(content.Runner,'batch',return_value=[]):
            self.assertEqual(p.run_cleaning_round(self.input,cfg,self.out,self.books,1)['next_round'],2)
            cfg['workers']=16
            self.assertEqual(p.run_cleaning_round(self.input,cfg,self.out,self.books,2)['next_round'],3)
            cfg['workers']=4
            terminal = p.run_cleaning_round(self.input,cfg,self.out,self.books,3)
        self.assertEqual(terminal['state'],'technical_failure')
        self.assertEqual(terminal['next_round'],None)
        with mock.patch.object(p.urllib.request,'urlopen',side_effect=AssertionError('finalize requested model')):
            p.finalize_cleaning(self.input,cfg,self.out,self.books)
            p.finalize_cleaning(self.input,cfg,self.out,self.books)
        self.assertEqual(p.loadl(self.out/'FINAL_RECORDS.jsonl'),[])
        self.assertEqual(p.read(self.out/'STATE.json')['status'],'finished_with_pending')
        self.assertEqual(p.read(self.out/'ORCHESTRATION_STATUS.json')['outcome'],'technical_failure')

    def test_content_format_revision_stays_within_first_round(self):
        import clean_boundary_v46 as content
        cfg = {'api_url':'http://unused','model':'test','workers':2,'staged_round':1}
        runner = content.Runner(self.out/'content',cfg)
        (self.out/'content').mkdir(parents=True)
        transmissions = []
        def malformed(*_,**__):
            transmissions.append(True)
            return io.BytesIO(b'not-json')
        with mock.patch.object(content.urllib.request,'urlopen',side_effect=malformed):
            result = runner.call('a','explanation',{'id':'a'},'prompt',lambda _: None)
            self.assertEqual(result['failure_kind'],'format')
            cfg['staged_round']=2
            self.assertEqual(runner.call('a','explanation',{'id':'a'},'prompt',lambda _: None)['failure_kind'],'format')
        self.assertEqual(len(transmissions),2)

    def test_content_http_413_is_not_retried_at_lower_worker_cap(self):
        import clean_boundary_v46 as content
        cfg = {'api_url':'http://unused','model':'test','workers':64,'staged_round':1}
        runner = content.Runner(self.out/'content',cfg)
        (self.out/'content').mkdir(parents=True)
        calls = []
        def reject(*_,**__):
            calls.append(True)
            raise urllib.error.HTTPError('http://unused',413,'too large',{},io.BytesIO(b''))
        with mock.patch.object(content.urllib.request,'urlopen',side_effect=reject):
            self.assertEqual(runner.call('a','explanation',{'id':'a'},'prompt',lambda _: None)['failure_kind'],
                             'content_or_format')
            cfg['staged_round']=2
            runner.call('a','explanation',{'id':'a'},'prompt',lambda _: None)
        self.assertEqual(len(calls),1)

    def test_adapter_round_and_reconciliation_never_export_early_or_call_model(self):
        import dictionary_cleaning as adapter
        self.out.mkdir()
        p.write(self.out/'MANIFEST.json',{'kind':'cleaning_staged'})
        p.write(self.out/'ORCHESTRATION_STATUS.json',{'state':'finalized'})
        books_file = self.root/'books.json'
        p.write(books_file,self.books)
        argv = ['dictionary_cleaning.py','--input',str(self.input),'--books',str(books_file),
                '--out',str(self.out),'--export',str(self.root/'export'),
                '--subject','Test','--slug','test','--api-url','http://unused','--model','test']
        with mock.patch.object(sys,'argv',argv+['--only-round','1']), \
             mock.patch.object(p,'validate_books',return_value=self.books), \
             mock.patch.object(adapter,'verify_sources'), \
             mock.patch.object(adapter,'verify_snapshot'), \
             mock.patch.object(p,'run_cleaning_round',return_value={'state':'awaiting_retry'}), \
             mock.patch.object(adapter,'export_records',side_effect=AssertionError('early export')):
            self.assertEqual(adapter.main(),0)
        with mock.patch.object(sys,'argv',argv), \
             mock.patch.object(p,'validate_books',return_value=self.books), \
             mock.patch.object(adapter,'verify_sources'), \
             mock.patch.object(adapter,'verify_snapshot'), \
             mock.patch.object(p,'validate_staged_run'), \
             mock.patch.object(p,'verify_staged_checkpoint'), \
             mock.patch.object(p,'run_cleaning',side_effect=AssertionError('model requested')), \
             mock.patch.object(adapter,'export_records',return_value={'records':0}):
            self.assertEqual(adapter.main(),0)

    def test_changed_checkpoint_blocks_retry_before_service_contact(self):
        import clean_boundary_v46 as content
        lock_module = types.SimpleNamespace(book_lock=lambda _: contextlib.nullcontext())
        def failed_name(values, label, *_):
            return [{**r,p.NAME:{'decision':'review','api_status':'api_error',
                                  'failure_kind':'transport'}} for r in values]
        cfg = {'api_url':'http://unused','model':'test','workers':64,'no_auth':True}
        with mock.patch.dict(sys.modules,{'fullbook_llm_v2':lock_module}), \
             mock.patch.object(p.urllib.request,'urlopen',side_effect=lambda *a,**k: io.BytesIO(b'{"data":[{"id":"test"}]}')), \
             mock.patch.object(p,'filter_stage',side_effect=failed_name), \
             mock.patch.object(content.Runner,'batch',return_value=[]):
            p.run_cleaning_round(self.input,cfg,self.out,self.books,1)
        p.write(self.out/'SUMMARY.json',{'altered':True})
        cfg['workers']=16
        with mock.patch.object(p.urllib.request,'urlopen',side_effect=AssertionError('service contacted')):
            with self.assertRaisesRegex(ValueError,'checkpoint changed'):
                p.run_cleaning_round(self.input,cfg,self.out,self.books,2)


if __name__ == '__main__':
    unittest.main()
