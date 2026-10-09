import argparse
import json
from pathlib import Path
import tempfile
import unittest
import fullbook_llm_v4 as v4

def entry():
    return {'head':[{'unit':0,'quote':'Alpha'}],'knowledge_point':[{'unit':0,'quote':'Alpha'}],
        'name':[],'body':[{'start':{'unit':0,'quote':'means a thing'},'end':{'unit':0,'quote':'Alpha again.'}}],
        'body_complete':True}

class RunnerTests(unittest.TestCase):
    def test_mid_sentence_and_table_heads_rejected(self):
        for text,kind,occurrence in [('A Beta exists.','paragraph',None),('Beta and Beta exist.','paragraph',1),('Beta','table',None)]:
            head={'unit':0,'quote':'Beta'}
            if occurrence is not None:head['occurrence']=occurrence
            item={'head':[head],'knowledge_point':[head],'name':[],'body':[],'body_complete':True}
            part={'lo':0,'hi':1,'units':[{'unit':0,'offset':0,'text':text,'kind':kind}]}
            with self.assertRaises(ValueError):
                v4.anchors.validate_entry(item,part,text,{'identifier':'test','md_sha256':'test'})

    def test_repeated_head_and_partitioned_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);md=root/'book.md';md.write_text('Alpha means a thing called Alpha again.\n')
            args=argparse.Namespace(api_url='http://unused',model='test',tokenizer=None,context=100000,
                output_tokens=16000,overlap=4,workers=2,timeout=3,server_context=None,out=root/'out',attempts=3)
            runner=v4.Runner(args);self.addCleanup(runner.close)
            runner.api=lambda *a: {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'scanned_all':True,'entries':[entry()]})}}]}
            result=runner.run_book({'identifier':'test','title':'Test','md_path':str(md)})
            self.assertEqual(result['entries'],1)
            self.assertEqual(result['status'],'completed')
            folder=next(p for p in args.out.iterdir() if p.is_dir())
            allrows=json.loads((folder/'entries.json').read_text())
            accepted=json.loads((folder/'accepted_entries.json').read_text())
            quarantined=json.loads((folder/'quarantined_entries.json').read_text())
            self.assertEqual(len(allrows),len(accepted)+len(quarantined))
            self.assertEqual(allrows[0]['raw_content'],'means a thing called Alpha again.')

    def test_budget_does_not_discard_forward_context(self):
        text='## A\n\n罗马天\n\n主教。\n\n## B\n'
        rows=v4.structural_units(text);hi=next(r['unit'] for r in rows if '主教' in r['text'])
        part=v4.structure.packet(rows,0,hi,4)
        self.assertTrue(any('主教' in r['text'] for r in part['units']))

if __name__=='__main__':unittest.main()
