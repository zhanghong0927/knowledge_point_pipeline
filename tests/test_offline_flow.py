"""Offline handoff audit. Model and mounting decisions are explicit fixtures."""

import csv
import os
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class OfflineFlowTests(unittest.TestCase):
    def test_failed_task_retry_is_explicit_and_preserves_failure_history(self):
        pipeline=load(ROOT/'pipeline.py','retry_pipeline')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg=root/'config.json';cfg.write_text('{}')
            script=root/'worker.py';script.write_text('raise SystemExit(1)\n')
            task={'name':'worker','command':[sys.executable,str(script)],'requires':[],
                  'produces':[],'cwd':str(root),'env':{},'blocked':None}
            stages=[{'stage':'02','tasks':[task]}];values={'run':str(root/'run')}
            with self.assertRaises(RuntimeError):pipeline.run_stages(cfg,{},values,stages,False)
            script.write_text('print("repaired")\n')
            with self.assertRaises(ValueError):pipeline.run_stages(cfg,{},values,stages,True)
            pipeline.run_stages(cfg,{},values,stages,True,retry_failed=True)
            state=json.loads((root/'run/pipeline_state.json').read_text())['02.worker']
            self.assertEqual(state['status'],'completed')
            self.assertEqual(state['previous_failed_attempt']['returncode'],1)

    def test_native_important_audit_receives_its_required_pilot_file(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);run=root/'run';(run/'04_tracks').mkdir(parents=True)
            source=run/'04_tracks/important.csv'
            source.write_text('identifier,title\nb1,Example\n')
            cfg=root/'scope.json';cfg.write_text(json.dumps({'subject_name':'Test','subject_slug':'test'}))
            bindir=root/'bin';bindir.mkdir();shim=bindir/'python3'
            shim.write_text('#!'+sys.executable+'\n'+
                'import os,sys,csv\nfrom pathlib import Path\n'+
                'if sys.argv[1]=="-c": os.execv(sys.executable,[sys.executable]+sys.argv[1:])\n'+
                'out=Path(sys.argv[sys.argv.index("--output")+1])\n'+
                'rows=list(csv.DictReader((out/"MD审核试验样本.csv").open(encoding="utf-8-sig")))\n'+
                'assert rows==[{"identifier":"b1","title":"Example"}]\n'+
                '(out/(sys.argv[2]+"_called")).write_text("ok")\n')
            shim.chmod(0o755)
            env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],
                 'SUBJECT_CONFIG':str(cfg),'RUN_DIR':str(run),'API_URL':'http://offline.invalid/v1/chat/completions','MODEL':'fixture'}
            result=subprocess.run(['bash',str(ROOT/'modules/book_screening/examples/run_stages.sh'),'5'],
                                  env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((run/'05_important_md/audit_called').exists())
            self.assertTrue((run/'05_important_md/materialize_called').exists())

    def test_numbered_references_do_not_damage_english_content(self):
        rule = load(ROOT / 'modules/cleaning/scripts/rule_clean.py', 'flow_rules')
        model = load(ROOT / 'modules/cleaning/scripts/model_clean.py', 'flow_models')
        for value in ('Stable angina', 'Configuration', 'configurable system', 'table tennis',
                      'The figure shows stability.', '万用表R挡'):
            with self.subTest(value=value):
                self.assertEqual(rule.clean_context(value, 6000)[0], value)
                self.assertEqual(model.clean_model_text(value), value.rstrip('。'))
        for value in ('(Fig. 2-1)', '(Table 3)', '(图 2-2)'):
            self.assertEqual(rule.clean_context(value, 6000)[0], '')
            self.assertEqual(model.clean_model_text(value), '')

    def test_llm_plan_has_no_missing_internal_handoff(self):
        pipeline = load(ROOT / 'pipeline.py', 'flow_pipeline')
        config, values = pipeline.load_config(ROOT / 'configs/pipeline.important_llm.example.json')
        stages = pipeline.plan(config, values)
        available = set()
        run = str(Path(values['run'])) + '/'
        for stage in stages:
            for task in stage['tasks']:
                for requirement in task['requires']:
                    if requirement.startswith(run):
                        self.assertIn(requirement, available, task['name'] + ': ' + requirement)
                available.update(task['produces'])
        self.assertTrue(all(not t['blocked'] for t in stages[4]['tasks']))

    def test_missing_executable_recorded_as_failed(self):
        pipeline = load(ROOT / 'pipeline.py', 'flow_runtime')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config = root/'config.json'; config.write_text('{}')
            task = {'name':'missing_program','command':[str(root/'not_an_executable')],
                    'requires':[],'produces':[],'cwd':str(root),'env':{},'blocked':None}
            with self.assertRaises(OSError):
                pipeline.run_stages(config, {}, {'run':str(root/'run')}, [{'stage':'03','tasks':[task]}], False)
            state = json.loads((root/'run/pipeline_state.json').read_text())
            self.assertEqual(state['03.missing_program']['status'],'failed')

    def test_mounting_gap_blocks_instead_of_faking_completion(self):
        pipeline = load(ROOT/'pipeline.py','flow_gap')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg_path=root/'config.json';cfg_path.write_text('{}')
            task={'name':'mounting','command':[],'requires':[],'produces':[],
                  'cwd':str(root),'env':{},'blocked':'Configure hooks.mounting'}
            with self.assertRaisesRegex(ValueError,'mounting'):
                pipeline.run_stages(cfg_path,{}, {'run':str(root/'run')},[{'stage':'05','tasks':[task]}],False)
            self.assertFalse((root/'run/pipeline_state.json').exists())

    def test_rules_track_local_chain_with_explicit_model_fixtures(self):
        manifest = load(ROOT/'adapters/screened_books_to_manifest.py','flow_manifest')
        normalizer = load(ROOT/'adapters/normalize_records.py','flow_normalize')
        sys.path.insert(0,str(ROOT/'modules/important_books/scripts'))
        import layout_model_validation as important
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            n1=root/'n1.md'; n1.write_text('# 机械工程\n\n## 齿轮传动\n齿轮传动用于传递运动。\n',encoding='utf-8')
            n3=root/'n3.md'; n3.write_text('# 操作规程\n1. 岗位责任制：明确职责。\n2. 风险评估：评估风险。\n',encoding='utf-8')
            csv_path=root/'audit.csv'
            with csv_path.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['identifier','title','md_path','final_decision','book_track'])
                w.writeheader()
                for ref,path in [('n1',n1),('n3',n3)]:
                    w.writerow({'identifier':ref,'title':ref,'md_path':str(path),'final_decision':'PASS','book_track':'其他重要书籍'})
            args=manifest.parser().parse_args(['--input',str(csv_path),'--out',str(root/'book_manifest'),
                 '--track','important','--subject-slug','mechanical_engineering'])
            books,_,_,unresolved,_=manifest.convert(args)
            self.assertFalse(unresolved)
            # Classification decisions are fixtures, not calls to a model interface.
            prep=root/'prepared';cls=root/'classification'
            important.write_json(prep/'manifest.json',{'targets':['n1','n3']})
            for book in books:
                ref=book['identifier']
                important.write_json(prep/'prepared'/f'{ref}.json',book)
                important.write_json(cls/'results'/f'{ref}.json',{'ref':ref,'status':'classified','primary_class':ref.upper()})
            extraction=root/'extracted'
            with patch('socket.socket.connect',side_effect=AssertionError('Network forbidden in offline flow')):
                result=important.route_extractors(prep,cls,extraction)
            self.assertGreater(result['knowledge_points_extracted'],0)
            normalized=[];traces=[]
            for i,row in enumerate(normalizer.records(extraction/'knowledge_points.csv'),1):
                row,trace=normalizer.standardize(row,'important','机械工程','mechanical_engineering',i)
                normalized.append(row);traces.append(trace)
            (root/'normalized.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in normalized))
            subprocess.run([sys.executable,ROOT/'modules/cleaning/scripts/rule_clean.py','--input',root/'normalized.jsonl',
                            '--output-dir',root/'rules'],check=True,capture_output=True)
            kept=[json.loads(l) for l in (root/'rules/rule_pass.jsonl').read_text().splitlines()]
            self.assertGreater(len(kept),0)
            # Model keep and mounted path are synthetic, verifying the data contract only.
            standards=[]
            for row in kept:
                row={k:v for k,v in row.items() if k in {'id','name','knowledge_point','definition','en_definition','description','en_description','main_tags','related_tags','source','tag'}}
                row['main_tags']='机械工程/示例路径'
                standards.append(row)
            duplicate={**standards[0],'id':'duplicate_fixture','definition':'更长的定义，仅用作离线去重选择测试。'}
            standards.append(duplicate)
            mounted=root/'mounted.jsonl';mounted.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in standards))
            subprocess.run([sys.executable,ROOT/'modules/dedup/dedup.py','run','--mode','length',
                            '--subject','mechanical_engineering','--input',mounted,'--out',root/'dedup'],check=True,capture_output=True)
            retained=[json.loads(l) for l in (root/'dedup/retained.jsonl').read_text().splitlines()]
            self.assertEqual(len(retained),len(standards)-1)
            self.assertIn('duplicate_fixture',{r['id'] for r in retained})
            self.assertTrue(all(r['tag']=='机械工程' for r in retained))


if __name__ == '__main__':
    unittest.main()
