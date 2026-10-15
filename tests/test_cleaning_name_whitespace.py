"""Names may be repaired at clean export; accepted IDs and original evidence may not."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters'))
import dictionary_cleaning
import cleaning_handoff
import test_dictionary_cleaning as dictionary_fixtures
import test_cleaning_handoff as important_fixtures


def load_policy(test):
    path=ROOT/'adapters/name_whitespace.py'
    if not path.exists():test.fail('Conservative name-whitespace repair is not implemented')
    spec=importlib.util.spec_from_file_location('tested_name_whitespace',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class CleaningWhitespaceTests(unittest.TestCase):
    def test_known_chinese_layout_is_fixed_without_modifying_other_fields(self):
        policy=load_policy(self)
        original={'id':'a','name':' 氩\u3000弧  焊 ','knowledge_point':'gas tungsten arc welding',
                  'definition':'原 定义 20  V','description':'原 说明',
                  'source':{'book':'原书','spans':[[2,8]]},'extra':[None,'原值']}
        before=copy.deepcopy(original)
        row,audit=policy.normalize_record(original)
        self.assertEqual(row,{**before,'name':'氩弧焊'})
        self.assertEqual(original,before)
        self.assertEqual(audit['fields']['name']['before'],' 氩\u3000弧  焊 ')
        self.assertEqual(audit['fields']['name']['after'],'氩弧焊')

    def test_english_units_models_formulas_and_code_keep_normal_spaces(self):
        policy=load_policy(self)
        values=['mass flow meter','mass flowmeter','Press Work','presswork','re sign','resign',
                '20 V','10 kN·m','ISO 9001','CNC 802 系列','埋沟 MOSFET',
                '$x  + y$','x  = y + z','\\(x  y\\)','`a  b`','x y']
        for value in values:
            with self.subTest(value=value):
                original={'id':'a','name':value,'knowledge_point':value,'definition':'原 文'}
                row,audit=policy.normalize_record(original)
                self.assertEqual(row,original)
                self.assertEqual(audit['fields'],{})

    def test_unknown_chinese_word_separator_is_preserved_and_flagged(self):
        policy=load_policy(self)
        row,audit=policy.normalize_record({'name':'甲 乙','knowledge_point':''})
        self.assertEqual(row['name'],'甲 乙')
        self.assertIn('unconfirmed_cjk_spacing',audit['warnings']['name'])

    def test_original_unspaced_title_can_support_a_new_term(self):
        policy=load_policy(self)
        row,audit=policy.normalize_record({'name':'复 合 试 验 装 置','source':'book'},
                                         originals={'name':'复合试验装置'})
        self.assertEqual(row['name'],'复合试验装置')
        self.assertIn('cjk_spacing_supported_by_original_title',audit['fields']['name']['rules'])

    def test_repeated_layout_space_keeps_one_english_word_separator(self):
        policy=load_policy(self)
        row,audit=policy.normalize_record({'name':'','knowledge_point':'  heat\t  flow  '})
        self.assertEqual(row['knowledge_point'],'heat flow')
        self.assertTrue(audit['fields'])


class CleaningExportWhitespaceTests(unittest.TestCase):
    def dictionary_fixture(self,root):
        inp,native,originals,kept=dictionary_fixtures.finished_fixture(root)
        kept={**kept,'name':' 氩 弧 焊 ','knowledge_point':'mass flow meter'}
        important_fixtures.rows(native/'FINAL_RECORDS.jsonl',[kept])
        return inp,native,originals,kept

    def test_dictionary_final_export_repairs_names_and_preserves_native_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp,native,originals,kept=self.dictionary_fixture(root)
            saved={p.name:p.read_bytes() for p in native.iterdir() if p.is_file()}
            report=dictionary_cleaning.export_records(inp,native,root/'export','Mechanical','mechanical_engineering')
            records=important_fixtures.mounting_bridge.rows(root/'export/records.jsonl')
            self.assertEqual(records[0]['name'],'氩弧焊')
            self.assertEqual(records[0]['knowledge_point'],'mass flow meter')
            self.assertEqual(records[0]['source'],originals[0]['source'])
            self.assertEqual([r['id'] for r in records],['kept'])
            trace=important_fixtures.mounting_bridge.rows(root/'export/trace.jsonl')[0]
            self.assertEqual(trace['cleaned_record'],kept)
            self.assertEqual(trace['original_record'],originals[0])
            self.assertEqual(trace['name_whitespace']['fields']['name']['before'],' 氩 弧 焊 ')
            self.assertEqual(report['name_whitespace']['changed_records'],1)
            self.assertEqual({p.name:p.read_bytes() for p in native.iterdir() if p.is_file()},saved)
            cleaning_handoff.verify_export(root/'export/records.jsonl')

    def test_important_real_restore_cli_repairs_only_model_keep_records(self):
        fixture=important_fixtures.CleaningHandoffTests('test_important_restore_binds_only_native_keep_records')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture.fixture(root,source={'book_title':'原书','page':9})
            rows=list(important_fixtures.normalize.records(root/'clean.jsonl'))
            rows[0]['name']=' 氩 弧 焊 ';rows[0]['knowledge_point']='20 V'
            important_fixtures.rows(root/'clean.jsonl',rows)
            judgments=list(important_fixtures.normalize.records(root/'judgments.jsonl'))
            judgments[0]['model_cleaning'].update(name=' 氩 弧 焊 ',knowledge_point='20 V')
            important_fixtures.rows(root/'judgments.jsonl',judgments)
            before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
            call=subprocess.run([sys.executable,'-B','-X','utf8',str(ROOT/'adapters/normalize_records.py'),'--restore',
                '--input',str(root/'clean.jsonl'),'--out',str(root/'export'),'--subject','Mechanical',
                '--slug','mechanical','--trace',str(root/'normalized/trace.jsonl'),
                '--normalization-report',str(root/'normalized/report.json'),
                '--cleaning-report',str(root/'model_report.json')],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(call.returncode,0,call.stderr)
            records=list(important_fixtures.normalize.records(root/'export/records.jsonl'))
            self.assertEqual([r['id'] for r in records],['keep'])
            self.assertEqual(records[0]['name'],'氩弧焊')
            self.assertEqual(records[0]['knowledge_point'],'20 V')
            self.assertEqual(records[0]['source'],{'book_title':'原书','page':9})
            trace=list(important_fixtures.normalize.records(root/'export/trace.jsonl'))[0]
            self.assertEqual(trace['name_whitespace']['fields']['name']['before'],' 氩 弧 焊 ')
            report=cleaning_handoff.verify_export(root/'export/records.jsonl')
            self.assertEqual(report['name_whitespace']['changed_records'],1)
            self.assertEqual({p.name:p.read_bytes() for p in root.iterdir() if p.is_file()},before)

    def test_export_cannot_reuse_an_output_with_a_different_name_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp,native,_,_=self.dictionary_fixture(root)
            out=root/'export'
            dictionary_cleaning.export_records(inp,native,out,'Mechanical','mechanical_engineering')
            report=json.loads((out/'report.json').read_text('utf-8'))
            self.assertIn('name_whitespace',report['identity'])
            report['identity']['name_whitespace']={'version':'different-policy'}
            (out/'report.json').write_text(json.dumps(report),'utf-8')
            saved=(out/'records.jsonl').read_bytes()
            with self.assertRaises(ValueError):
                dictionary_cleaning.export_records(inp,native,out,'Mechanical','mechanical_engineering')
            self.assertEqual((out/'records.jsonl').read_bytes(),saved)

    def test_verifier_rejects_unbound_name_policy_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp,native,_,_=self.dictionary_fixture(root);out=root/'export'
            dictionary_cleaning.export_records(inp,native,out,'Mechanical','mechanical_engineering')
            report=json.loads((out/'report.json').read_text('utf-8'))
            self.assertIn('name_whitespace',report)
            report['name_whitespace']['policy']={'version':'unknown'}
            (out/'report.json').write_text(json.dumps(report),'utf-8')
            with self.assertRaises(ValueError):cleaning_handoff.verify_export(out/'records.jsonl')

    def test_malformed_name_policy_metadata_is_a_validation_error(self):
        for malformed in (None,[], 'invalid'):
            with self.subTest(metadata=malformed),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);inp,native,_,_=self.dictionary_fixture(root);out=root/'export'
                dictionary_cleaning.export_records(inp,native,out,'Mechanical','mechanical_engineering')
                report=json.loads((out/'report.json').read_text('utf-8'))
                report['name_whitespace']=malformed
                (out/'report.json').write_text(json.dumps(report),'utf-8')
                try:cleaning_handoff.verify_export(out/'records.jsonl')
                except Exception as error:self.assertIsInstance(error,ValueError)
                else:self.fail('Malformed name policy must be rejected')


if __name__=='__main__':unittest.main()
