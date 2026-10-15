"""Exercise real selection, CLI and verification for whitespace-only spelling variants."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import dedup


def record(identifier, name='', english='', path='机械工程/焊接', definition=''):
    return {'id':identifier, 'name':name, 'knowledge_point':english,
            'main_tags':path, 'definition':definition, 'en_definition':None,
            'source':{'book':'原书', 'page':12}, 'extra':['原值', None]}


class WhitespaceTests(unittest.TestCase):
    def test_chinese_and_layout_spacing_are_removed_from_comparison_only(self):
        pairs=[('氩弧焊','氩 弧 焊'), ('物理气相沉积(PVD)','物理气相沉积 (PVD)'),
               ('电枢绕组180°抽头','电枢绕组 180° 抽头'),
               ('数-模转换器','数 - 模转换器'), ('埋沟MOSFET','埋沟 MOSFET'),
               ('横向双扩散MOS晶体管','横向双扩散 MOS 晶体管'),
               ('氩弧焊','氩\u00a0弧\u3000焊'), ('$ \\pi $ 形滤波器','$ \\pi $  形滤波器')]
        for plain,spaced in pairs:
            with self.subTest(spaced=spaced):
                rows=[record('short',plain,definition='短'), record('long',spaced,definition='更完整的定义')]
                before=copy.deepcopy(rows)
                kept,audit=dedup.length_dedup(rows,{})
                self.assertEqual([r['id'] for r in kept],['long'])
                self.assertEqual(kept,[before[1]])
                self.assertEqual(rows,before)
                self.assertEqual(audit[0]['matched_fields'],['name'])

    def test_english_word_boundaries_and_known_different_senses_survive(self):
        for first,second in [('mass flow meter','mass flowmeter'),('presswork','Press Work'),
                             ('a b','ab'),('re sign','resign')]:
            with self.subTest(pair=(first,second)):
                rows=[record('a',english=first),record('b',english=second)]
                self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))

    def test_repeated_english_layout_spaces_do_not_change_word_boundaries(self):
        rows=[record('a',english='heat flow'),record('b',english='heat\t  flow')]
        self.assertEqual([r['id'] for r in dedup.length_dedup(rows,{})[0]],['a'])

    def test_reviewed_english_spelling_is_scoped_to_field_and_full_path(self):
        path='电气工程/电机与电工磁性机械/特种与微特电机/控制微电机/交流与直流伺服电动机'
        rows=[record('a',english='aircraft servomotor',path=path),
              record('b',english='aircraft servo motor',path=path)]
        self.assertEqual([r['id'] for r in dedup.length_dedup(rows,{})[0]],['a'])
        other=[{**r,'main_tags':'另一完整分支'} for r in rows]
        self.assertEqual(dedup.length_dedup(other,{}),(other,[]))
        different_fields=[record('c',name='aircraft servomotor',path=path),rows[1]]
        self.assertEqual(dedup.length_dedup(different_fields,{}),(different_fields,[]))

    def test_normalization_does_not_merge_different_branches_or_use_removed_aliases(self):
        rows=[record('a','氩弧焊'),record('b','氩 弧 焊',path='机械工程/另一路径')]
        self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))
        chain=[record('a','氩弧焊','x'),record('b','电阻焊','y'),record('c','氩 弧 焊','y')]
        self.assertEqual([r['id'] for r in dedup.length_dedup(chain,{})[0]],['a','b'])

    def test_chinese_context_does_not_erase_english_word_boundaries(self):
        rows=[record('a','重新签名 re sign'),record('b','重新签名 resign')]
        self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))


class WhitespaceRunTests(unittest.TestCase):
    def test_real_cli_normalizes_and_verifies_without_rewriting_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.json';out=root/'output'
            rows=[record('a','氩弧焊'),record('b','氩 弧 焊'),
                  record('c',english='re sign'),record('d',english='resign')]
            payload=json.dumps(rows,ensure_ascii=False).encode('utf-8');source.write_bytes(payload)
            cli=Path(dedup.__file__)
            call=subprocess.run([sys.executable,'-B',str(cli),'run','--subject','mechanical_engineering',
                                 '--mode','length','--input',str(source),'--out',str(out)],
                                capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(call.returncode,0,call.stderr)
            summary=json.loads((out/'SUMMARY.json').read_text('utf-8'))
            self.assertEqual(summary['removed_records'],1)
            self.assertEqual(summary['model_attempts_this_run'],0)
            self.assertEqual(dedup.read_rows(out/'retained.jsonl'),[rows[0],rows[2],rows[3]])
            self.assertEqual(source.read_bytes(),payload)
            collisions=dedup.read_rows(out/'whitespace_collisions.jsonl')
            self.assertEqual(len(collisions),1)
            self.assertEqual(set(collisions[0]['member_ids']),{'c','d'})
            self.assertEqual(collisions[0]['resolution'],'not_auto_merged')
            verify=subprocess.run([sys.executable,'-B',str(cli),'verify','--out',str(out)],
                                  capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(verify.returncode,0,verify.stderr)
            self.assertTrue(json.loads(verify.stdout)['original_payloads_unchanged'])

    def test_resume_and_verify_reject_changed_normalization_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.json';out=root/'output'
            source.write_text(json.dumps([record('a','氩弧焊'),record('b','氩 弧 焊')]),'utf-8')
            config=dedup.Config(inputs=[source],out=out,subject='mechanical_engineering')
            dedup.execute(config)
            meta=json.loads((out/'RUN.json').read_text('utf-8'))
            self.assertIn('name_normalization',meta)
            meta['name_normalization']={'version':'different-policy'}
            (out/'RUN.json').write_text(json.dumps(meta),'utf-8')
            with self.assertRaises(ValueError):dedup.verify_run(out)
            config.resume=True
            with self.assertRaises(ValueError):dedup.execute(config)


if __name__=='__main__':unittest.main()
