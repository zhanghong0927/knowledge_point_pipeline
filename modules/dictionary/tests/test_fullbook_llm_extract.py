import argparse
import json
from pathlib import Path
import tempfile
import unittest

from fullbook_llm_extract import Runner, units_for, packet, validate, read


def answer(body=True):
    return {'scanned_all':True,'entries':[{
        'head':[{'unit':0,'quote':'Learning'}],
        'knowledge_point':[{'unit':0,'quote':'Learning'}],'name':[],
        'body':[{'start':{'unit':1,'quote':'A process.'},'end':{'unit':1,'quote':'A process.'}}] if body else [],
        'body_complete':True}]}


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.text = 'Learning\nA process.\n'
        self.book = {'identifier':'test','title':'Test','md_path':'test.md','md_sha256':'abc'}
        self.part = packet(units_for(self.text),0,2,0)

    def test_exact_body(self):
        result = validate(answer(),self.part,self.text,self.book)[0]
        self.assertEqual(result['raw_content'],'A process.')
        self.assertFalse(result['ready_for_delivery'])

    def test_bilingual(self):
        text = 'Learning 学习\nA process.\n'
        data = answer()
        data['entries'][0]['head'][0]['quote'] = 'Learning 学习'
        data['entries'][0]['name'] = [{'unit':0,'quote':'学习'}]
        row = validate(data,packet(units_for(text),0,2,0),text,self.book)[0]
        self.assertEqual(row['name'],'学习')
        self.assertEqual(row['knowledge_point'],'Learning')

    def test_no_invented_name(self):
        data = answer()
        data['entries'][0]['name'] = [{'unit':0,'quote':'学习'}]
        with self.assertRaises(ValueError):
            validate(data,self.part,self.text,self.book)

    def test_incomplete_scan(self):
        data = answer(); data['scanned_all'] = False
        with self.assertRaises(ValueError):
            validate(data,self.part,self.text,self.book)

    def test_empty_body(self):
        self.assertEqual(validate(answer(False),self.part,self.text,self.book)[0]['raw_content'],'')

    def test_overlap_rejected(self):
        data = answer(); data['entries'] *= 2
        with self.assertRaises(ValueError):
            validate(data,self.part,self.text,self.book)

    def test_ownership(self):
        self.part['lo'] = 1
        with self.assertRaises(ValueError):
            validate(answer(),self.part,self.text,self.book)

    def test_giant_line_lossless(self):
        text = 'X'*10000+'\r\nEND'
        units = units_for(text)
        self.assertEqual(''.join(u['text'] for u in units),text)
        for row in units:
            self.assertEqual(text[row['offset']:row['offset']+len(row['text'])],row['text'])

    def test_background_not_name(self):
        data = answer(); data['entries'][0]['name'] = [{'unit':1,'quote':'A process.'}]
        with self.assertRaises(ValueError):
            validate(data,self.part,self.text,self.book)

    def make_runner(self,root):
        args = argparse.Namespace(api_url='http://unused',tokenizer=None,out=Path(root)/'out',
            model='test',context=100000,output_tokens=16000,overlap=1,timeout=1,server_context=None)
        return Runner(args)

    def test_server_context_rejected(self):
        runner = self.make_runner('.')
        runner.api = lambda *a: {'data':[{'id':'test','max_model_len':32768}]}
        with self.assertRaises(ValueError): runner.check_server()

    def test_split_resume_and_full_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'book.md'; path.write_text(self.text,encoding='utf-8')
            runner = self.make_runner(root)
            calls = []
            def api(route,payload):
                part = json.loads(payload['messages'][1]['content'])
                calls.append((part['lo'],part['hi']))
                if part['hi']-part['lo']>1:
                    return {'choices':[{'finish_reason':'length'}]}
                data = answer() if part['lo']==0 else {'scanned_all':True,'entries':[]}
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
            runner.api = api
            spec = {'identifier':'book','title':'Book','md_path':str(path)}
            result = runner.run_book(spec)
            self.assertEqual(result['completed_units'],2)
            self.assertEqual(result['entries'],1)
            self.assertEqual(calls,[(0,2),(0,1),(1,2)])
            runner.api = lambda *a: self.fail('Resume should use checkpoint')
            self.assertEqual(runner.run_book(spec)['entries'],1)
            path.write_text('changed',encoding='utf-8')
            with self.assertRaises(ValueError): runner.run_book(spec)

    def test_failure_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'book.md';path.write_text(self.text,encoding='utf-8')
            runner = self.make_runner(root);calls=[]
            def fail(*args):
                calls.append(1);raise TimeoutError('test')
            runner.api=fail
            result=runner.run_book({'identifier':'book','title':'Book','md_path':str(path)})
            self.assertEqual(len(calls),2)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['completed_units'],0)


if __name__ == '__main__':
    unittest.main()
