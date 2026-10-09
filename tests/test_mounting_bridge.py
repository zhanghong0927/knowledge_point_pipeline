import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters'))
import mounting_bridge as bridge
from mounting_assets import normalize


def fixture(root, records=None):
    tree={'name':'机械工程','children':[{'name':'传动','children':[{'name':'齿轮','children':[]}, {'name':'传动设计','children':[]}]}]}
    tree_path=root/'tree.json';tree_path.write_text(json.dumps(tree,ensure_ascii=False))
    records=records if records is not None else [
        {'id':'1','name':'齿轮','knowledge_point':'gear','definition':'齿轮用于传递运动。',
         'en_definition':'','description':'','en_description':'','source':{'book':'示例','line':4},
         'tag':'机械工程','main_tags':'旧路径','related_tags':['旧关联']},
        {'id':'2','name':'齿轮传动','knowledge_point':'','definition':'齿轮传动。','tag':'机械工程','source':'b'},
        {'id':'3','name':'传动设计','knowledge_point':'','definition':'设计。','tag':'机械工程','source':'b'}]
    input_path=root/'input.jsonl';bridge.write_rows(input_path,records)
    args=SimpleNamespace(input=input_path,out=root/'mount',registry=bridge.REGISTRY,taxonomy=tree_path,
                         taxonomy_dir=root,subject_slug='mechanical_engineering',threshold=.85,min_depth=2,
                         max_depth=5,max_related=2,limit=0,api_url='http://offline.invalid/v1',model='mock',
                         api_key_env='',workers=2,timeout=10,max_tokens=1024)
    bridge.prepare(args)
    return args,records


def fake_routes(args):
    folder=args.out/'groups/mechanical_engineering'
    items=bridge.rows(folder/'input.jsonl');index=bridge.read(folder/'node_index.json')
    nodes=[n for n in index.values() if n['depth']==2]
    result=[]
    for i,item in enumerate(items):
        label={'status':'ok','decision':'accepted_leaf','best_path_codes':nodes[0]['chain_codes'][1:],
               'path_score':.95,'top_paths':[{'path_codes':nodes[1]['chain_codes'][1:],'path_score':.92,'truncated':False}]}
        if i==2:label={'status':'failed','error':'mock failure'}
        result.append({**item,'knowledge_labeling':label})
    bridge.write_rows(folder/'routed.jsonl',result)


def fake_review(base, model, item):
    ambiguous=item['name']=='齿轮传动'
    result={'request_id':item['request_id'],'judgment':'uncertain' if ambiguous else 'reasonable',
            'problem_type':'insufficient_information' if ambiguous else 'none',
            'reason':'mock evidence-based review','meaning_status':'clear','needs_boundary':False,
            'support_evidence':[] if ambiguous else [{'field':'name','text':item['name']}],'conflict_evidence':[]}
    return {'request_id':item['request_id'],'first':{'result':result,'attempts':[]},'second':None,
            'final':{'judgment':'reasonable'}}  # Export must recompute, not trust this field.


class MountingBridgeTests(unittest.TestCase):
    def test_review_uses_short_wire_id_and_retries_only_technical_failures(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));fake_routes(args)
            reviewer=bridge.reviewer_module();seen=[]
            def fail_one(base,model,item):
                seen.append(item['request_id'])
                if item['name']=='齿轮传动':
                    return {'request_id':item['request_id'],'first':{'result':None,'attempts':[]},
                            'second':None,'final':{'judgment':'technical_failure'}}
                return fake_review(base,model,item)
            with patch.object(reviewer,'review_one',side_effect=fail_one):bridge.review(args)
            self.assertTrue(all(len(k)==8 and k.startswith('R') for k in seen))
            initial=bridge.rows(args.out/'review_responses.jsonl')
            count=sum(r['final']['judgment']=='technical_failure' for r in initial)
            args.retry_failed_reviews=True;retried=[]
            def recover(base,model,item):
                retried.append(item['request_id']);return fake_review(base,model,item)
            with patch.object(reviewer,'review_one',side_effect=recover):bridge.review(args)
            self.assertEqual(len(retried),count)
            final=bridge.rows(args.out/'review_responses.jsonl')
            self.assertEqual({r['request_id'] for r in initial},{r['request_id'] for r in final})
            self.assertTrue(all(len(r['request_id'])==64 for r in final))
            report=bridge.read(args.out/'REVIEWED.json')
            self.assertEqual(report['reused'],len(initial)-count)

    def test_prepare_route_review_export_preserves_original_fields(self):
        with tempfile.TemporaryDirectory() as d:
            args,original=fixture(Path(d))
            reviewer=bridge.reviewer_module()
            with patch('socket.socket.connect',side_effect=AssertionError('No network allowed')), \
                 patch.object(bridge.subprocess,'run',side_effect=lambda *a,**kw:fake_routes(args)), \
                 patch.object(reviewer,'review_one',side_effect=fake_review):
                bridge.route(args)
                bridge.review(args)
                report=bridge.export(args)
            self.assertEqual(report['counts'],{'mounted':1,'review':1,'not_mounted':0,'technical_failure':1,'unconfigured':0})
            kept=bridge.rows(args.out/'mounted_standard.jsonl')[0]
            for k,v in original[0].items():
                if k not in ('main_tags','related_tags'):self.assertEqual(kept[k],v)
            self.assertEqual(kept['main_tags'],'机械工程/传动/齿轮')
            self.assertEqual(kept['related_tags'],['机械工程/传动/传动设计'])
            self.assertEqual(report['mounted_by_depth'],{'2':1})
            cmd=bridge.read(args.out/'groups/mechanical_engineering/route_command.json')
            self.assertEqual(cmd[cmd.index('--threshold')+1],'0.85')
            self.assertEqual(cmd[cmd.index('--min-mount-depth')+1],'2')
            self.assertEqual(cmd[cmd.index('--max-depth')+1],'5')
            import subprocess
            subprocess.run([sys.executable,ROOT/'modules/dedup/dedup.py','run','--mode','length',
                            '--subject','mechanical_engineering','--input',args.out/'mounted_standard.jsonl',
                            '--out',Path(d)/'dedup'],check=True,capture_output=True)
            self.assertEqual(bridge.rows(Path(d)/'dedup/retained.jsonl'),[kept])

    def test_low_score_and_wrong_chain_not_reviewed_as_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));fake_routes(args)
            p=args.out/'groups/mechanical_engineering/routed.jsonl';records=bridge.rows(p)
            records[0]['knowledge_labeling']['path_score']=.84
            records[1]['knowledge_labeling']['best_path_codes']=['invented-node']
            bridge.write_rows(p,records)
            items,decisions=bridge.review_plan(args.out,bridge.frozen(args.out))
            self.assertFalse(items)
            self.assertEqual([d['status'] for d in decisions],['review','review','technical_failure'])

    def test_missing_water_taxonomy_never_falls_back_to_civil(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'water.jsonl'
            bridge.write_rows(path,[{'id':'1','name':'水利','tag':'水利工程'}])
            a=SimpleNamespace(input=path,out=root/'mount',registry=bridge.REGISTRY,taxonomy=None,taxonomy_dir=root,
                              subject_slug=None,threshold=.85,min_depth=2,max_depth=5,max_related=2,limit=0)
            report=bridge.prepare(a)
            self.assertEqual(report['groups'],0);self.assertEqual(report['unconfigured'],1)
            self.assertFalse(bridge.read(a.out/'PREPARED.json')['groups'])

    def test_modified_assets_or_missing_native_ids_stop_export(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));fake_routes(args)
            p=args.out/'groups/mechanical_engineering/routed.jsonl'
            records=bridge.rows(p);bridge.write_rows(p,records[:-1])
            with self.assertRaisesRegex(ValueError,'coverage'):
                bridge.review_plan(args.out,bridge.frozen(args.out))
            (args.out/'groups/mechanical_engineering/profile/data/knowledge_tree.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'changed'):
                bridge.frozen(args.out)

    def test_registry_and_tree_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            nested={'name':'学科','children':[{'name':'一级','children':[{'name':'二级'}]}]}
            shapes=[nested,{'root':nested},{'nodes':[
                {'node_code':'0','name':'学科','parent_code':None},
                {'node_code':'1','name':'一级','parent_code':'0'},
                {'node_code':'2','name':'二级','parent_code':'1'}]},
                {'name':'学科','hierarchy':[{'name':'一级','l2_nodes':[{'name':'二级'}]}]}]
            for i,shape in enumerate(shapes):
                p=root/f'{i}.json';p.write_text(json.dumps(shape))
                tree,index,cards,info=normalize(p,'学科')
                self.assertEqual(len(index),3);self.assertEqual(info['max_depth'],2)
                self.assertEqual(tree['children'][0]['children'][0]['path'],'学科/一级/二级')
            p=root/'electronic.json';p.write_text(json.dumps([{'l1':'基础','l2':'数学/微积分','l3':'分析','l4_nodes':[{'l4_name':'导数'}]}]))
            tree,index,cards,info=normalize(p,'电子信息工程')
            self.assertEqual(info['max_depth'],4)
            self.assertEqual(len(index),5)
            self.assertTrue(info['synthetic_subject_routing_root'])

    def test_review_evidence_must_be_in_original_fields(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));fake_routes(args)
            bridge.review_plan(args.out,bridge.frozen(args.out))
            reviewer=bridge.reviewer_module()
            with patch.object(reviewer,'review_one',side_effect=fake_review):bridge.review(args)
            p=args.out/'review_responses.jsonl';responses=bridge.rows(p)
            responses[0]['first']['result']['support_evidence']=[{'field':'definition','text':'fabricated text'}]
            bridge.write_rows(p,responses)
            with self.assertRaisesRegex(ValueError,'substring'):bridge.export(args)


if __name__=='__main__':unittest.main()
