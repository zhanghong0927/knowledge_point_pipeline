import argparse
import json
from pathlib import Path
import tempfile
import unittest

import fullbook_llm_v3 as v3


def item(unit,head):
    return {'head':[{'unit':unit,'quote':head}], 'knowledge_point':[{'unit':unit,'quote':head}],
            'name':[],'body':[],'body_complete':True}


class V3Tests(unittest.TestCase):
    def make_runner(self,root):
        args=argparse.Namespace(api_url='http://unused',model='test',tokenizer=None,
            context=100000,output_tokens=16000,overlap=0,workers=4,timeout=5,
            server_context=None,out=Path(root)/'out',attempts=3)
        runner=v3.Runner(args);self.addCleanup(runner.close)
        return runner

    def test_partial_success_only_repairs_bad_entry(self):
        with tempfile.TemporaryDirectory() as root:
            md=Path(root)/'book.md';md.write_text('Alpha\n\nBeta\n',encoding='utf-8')
            runner=self.make_runner(root);calls=[]
            def api(route,payload):
                calls.append(payload)
                if len(calls)==1:
                    data={'scanned_all':True,'entries':[item(0,'Alpha'),item(2,'Not in source')]}
                else:
                    request=json.loads(payload['messages'][1]['content'])
                    repairs=request['repair_tasks']
                    self.assertEqual(len(repairs),1)
                    self.assertNotIn('Alpha',json.dumps(repairs))
                    data={'repairs':[{'repair_id':repairs[0]['repair_id'],'entry':item(2,'Beta'),'reason':'literal correction'}]}
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
            runner.api=api
            summary=runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})
            self.assertEqual(summary['entries'],2)
            self.assertEqual(len(calls),2)
            self.assertEqual(summary['status'],'completed')

    def test_exactly_three_technical_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            md=Path(root)/'book.md';md.write_text('Alpha\n',encoding='utf-8')
            runner=self.make_runner(root);calls=[]
            def api(*args):calls.append(1);raise TimeoutError('test')
            runner.api=api
            runner.backoff=lambda attempt:None
            s=runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})
            self.assertEqual(len(calls),3)
            self.assertEqual(s['status'],'partial')

    def test_keep_valid_when_two_repairs_fail(self):
        with tempfile.TemporaryDirectory() as root:
            md=Path(root)/'book.md';md.write_text('Alpha\n\nBeta\n',encoding='utf-8')
            runner=self.make_runner(root);calls=[]
            def api(route,payload):
                calls.append(payload)
                if len(calls)==1:data={'scanned_all':True,'entries':[item(0,'Alpha'),item(2,'bad')]}
                else:data={'repairs':[{'repair_id':'wrong_id','entry':None,'reason':'no'}]}
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
            runner.api=api
            s=runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})
            self.assertEqual(len(calls),3)
            self.assertEqual(s['entries'],1)
            self.assertEqual(s['status'],'partial')
            runner.api=lambda *a:self.fail('Checkpoint must not re-call API')
            self.assertEqual(runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})['entries'],1)

    def test_layout_cleanup_preserves_source_spans(self):
        text='## Alpha\n\nText before\n\n![](x.png)\n\n## Alpha\n\ntext after.\n'
        rows=v3.structural_units(text)
        entry={'head':'Alpha','knowledge_point':'Alpha','name':'',
               'source':{'body_spans':[(text.index('Text before'),len(text))]}}
        cleaned=v3.cleanup_body(entry,rows,text)
        self.assertNotIn('![]',cleaned['raw_content'])
        self.assertNotIn('## Alpha',cleaned['raw_content'])
        self.assertIn('text after.',cleaned['raw_content'])
        self.assertEqual(cleaned['raw_content'],'\n\n'.join(text[a:b] for a,b in cleaned['source']['body_spans']))

    def test_neighbor_entry_does_not_fail_whole_chunk(self):
        text='Alpha\n\nBeta\n'
        units=v3.structural_units(text)
        part={'lo':0,'hi':1,'units':units}
        accepted,rejected,excluded=v3.validate_items([item(0,'Alpha'),item(2,'Beta')],part,text,
            {'identifier':'test','md_sha256':'abc'})
        self.assertEqual(len(accepted),1)
        self.assertFalse(rejected)
        self.assertEqual(excluded[0]['reason'],'context_only')

    def test_appendix_zone_does_not_hide_body(self):
        text='## Alpha\n\nDefinition.\n\n## 附录一 英语缩略语汇编\n\nABC example\n\n## 附录二\n\nData\n'
        rows=v3.annotate_units(v3.structural_units(text))
        self.assertFalse(rows[0].get('excluded_zone'))
        abc=next(r for r in rows if 'ABC example' in r['text'])
        self.assertEqual(abc['excluded_zone'],'abbreviation_appendix')
        data=next(r for r in rows if r['text'].strip()=='Data')
        self.assertFalse(data.get('excluded_zone'))

    def test_empty_ordinary_head_not_dropped(self):
        rows=v3.annotate_units(v3.structural_units('## Alpha\n'))
        self.assertFalse(rows[0].get('excluded_zone'))

    def test_displaced_table_removed_by_explicit_caption(self):
        text='## PREVIOUS ENTRY\n\nPrior.\n\n## CURRENT ENTRY\n\nDefinition.\n\n<table><tr><td>X</td></tr></table>\n\n## Previous entry\n\nMore.\n'
        rows=v3.annotate_units(v3.structural_units(text))
        e={'head':'CURRENT ENTRY','knowledge_point':'CURRENT ENTRY','name':'','body_complete':True,
           'source':{'body_spans':[(text.index('Definition.'),len(text))]}}
        e=v3.cleanup_body(e,rows,text)
        self.assertNotIn('<table',e['raw_content'])
        self.assertNotIn('Previous entry',e['raw_content'])
        self.assertIn('More.',e['raw_content'])

    def test_title_case_dictionary_not_rejected(self):
        text=''.join(f'## Term {i}\n\nBody.\n\n' for i in range(30))
        rows=v3.annotate_units(v3.structural_units(text))
        self.assertFalse(any(r.get('head_role')=='secondary_style' for r in rows))

    def test_mixed_case_head_is_not_hard_deleted(self):
        text=''.join(f'## TERM {i}\n\nBody.\n\n' for i in range(20))+'## Beta\n'
        rows=v3.annotate_units(v3.structural_units(text))
        row=next(r for r in rows if r['text'].strip()=='## Beta')
        part={'lo':0,'hi':len(rows),'units':rows}
        good,bad,excluded=v3.validate_items([item(row['unit'],'Beta')],part,text,{'identifier':'test','md_sha256':'abc'})
        self.assertEqual(len(good),1)

    def test_repair_cannot_replace_valid_head(self):
        rows=v3.structural_units('Beta\n\nGamma\n')
        self.assertFalse(v3.repair_matches(item(0,'Beta'),item(2,'Gamma'),{'units':rows}))
        self.assertTrue(v3.repair_matches(item(0,'Beta'),item(0,'Beta'),{'units':rows}))

    def test_bad_repair_does_not_discard_good_repair(self):
        with tempfile.TemporaryDirectory() as root:
            md=Path(root)/'book.md';md.write_text('Alpha\n\nBeta\n\nGamma\n',encoding='utf-8')
            runner=self.make_runner(root);calls=[]
            def api(route,payload):
                calls.append(payload)
                if len(calls)==1:data={'scanned_all':True,'entries':[item(0,'Alpha'),item(2,'bad'),item(4,'bad')]}
                elif len(calls)==2:data={'repairs':[{'repair_id':'e00001','entry':None},
                    {'repair_id':'e00002','entry':item(4,'Gamma'),'reason':'fixed'}]}
                else:data={'repairs':[]}
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]}
            runner.api=api
            result=runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})
            self.assertEqual(result['entries'],2)
            self.assertEqual(result['status'],'partial')

    def test_section_exclusion_respects_hierarchy(self):
        rows=v3.annotate_units(v3.structural_units('## Contents\n\n### A\n\nlist\n\n## Main Entries\n\nBody\n'))
        self.assertEqual(next(r for r in rows if r['text'].strip()=='list')['excluded_zone'],'contents_or_index')
        self.assertFalse(next(r for r in rows if r['text'].strip()=='Body').get('excluded_zone'))

    def test_complete_flag_downgraded_for_visible_continuation(self):
        text='## Alpha\n\nFirst.\n\nSecond.\n\n## Beta\n'
        rows=v3.annotate_units(v3.structural_units(text))
        e={'head':'Alpha','knowledge_point':'Alpha','name':'','body_complete':True,
           'source':{'body_spans':[(text.index('First.'),text.index('First.')+6)]}}
        self.assertFalse(v3.cleanup_body(e,rows,text)['body_complete'])


if __name__=='__main__':unittest.main()
