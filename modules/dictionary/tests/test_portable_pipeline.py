import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import portable_pipeline as p


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.md = self.root / 'book.md'
        self.text = '# Energy\n\nEnergy is a physical quantity.\n\n# Next\nNext body.\n'
        self.md.write_text(self.text, encoding='utf-8')
        self.scope = self.root / 'scope.json'
        self.scope.write_text(json.dumps({'subject':'Physics','l1_nodes':[{'name':'Physics','definition':'Physical objects'}]}),encoding='utf-8')
        self.books = [{'identifier':'B001','title':'Test book','md_path':str(self.md),
                       'subject_slug':'physics','scope_config':str(self.scope)}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_books_rejected(self):
        with self.assertRaises(ValueError):
            p.validate_books(self.books * 2)

    def test_unsafe_book_id_rejected(self):
        with self.assertRaises(ValueError):
            p.validate_books([{**self.books[0],'identifier':'../escape'}])

    def test_md_only_preparation_covers_all_lines(self):
        result = p.prepare_sources(self.books, self.root / 'prepared')
        evidence = p.read_evidence(result[0]['pdf_evidence_path'])
        lines = [line for window in evidence['windows'] for line in window['lines']]
        self.assertEqual(len(lines), len(self.text.splitlines()))
        self.assertEqual([line['text'] for line in lines], self.text.splitlines())
        self.assertEqual(evidence['md_sha256'], hashlib.sha256(self.md.read_bytes()).hexdigest())
        self.assertEqual(result[0]['pdf_mode'], 'md_only_no_pdf')

    def test_wrong_evidence_hash_rejected(self):
        cache = self.root / 'evidence.json'
        cache.write_text(json.dumps({'md_sha256':'wrong','windows':[]}),encoding='utf-8')
        with self.assertRaises(ValueError):
            p.prepare_sources([{**self.books[0],'pdf_evidence_path':str(cache)}], self.root / 'prepared')

    def test_pdf_format_is_auxiliary_and_supports_pdf_bin(self):
        import fitz
        pdf = self.root/'source.bin'
        text = '# Energetics\nFirst unique long body anchor describing stored energy in objects.\nSecond unique long body anchor describing energy measurements.\n'
        self.md.write_text(text,encoding='utf-8')
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((40,50),'Energetics',fontname='hebo',fontsize=16)
        page.insert_text((40,90),text.splitlines()[1],fontsize=10)
        page.insert_text((40,120),text.splitlines()[2],fontsize=10)
        pdf.write_bytes(doc.tobytes())
        doc.close()
        result = p.prepare_sources([{**self.books[0],'pdf_path':str(pdf)}], self.root/'prepared')
        evidence = p.read_evidence(result[0]['pdf_evidence_path'])
        self.assertEqual([r['text'] for w in evidence['windows'] for r in w['lines']],text.splitlines())
        self.assertGreater(p.read(self.root/'prepared/PREPARED.json')['per_book'][0]['formatted_lines'],0)
        self.assertEqual(result[0]['pdf_mode'],'full_pdf_scanned_alignment_partial_allowed')

    def test_resume_rejects_changed_configuration(self):
        out = self.root / 'run'
        p.freeze_run(out, {'config':{'workers':1024}})
        p.freeze_run(out, {'config':{'workers':1024}})
        with self.assertRaises(ValueError):
            p.freeze_run(out, {'config':{'workers':64}})

    def make_extraction(self, raw=None):
        extract = self.root / 'extract'
        folder = extract / 'B001'
        folder.mkdir(parents=True)
        start = self.text.index('Energy is')
        end = self.text.index('\n\n# Next')
        entry = {'id':'entry001','knowledge_point':'Energy','name':'','head':'Energy',
                 'raw_content':self.text[start:end] if raw is None else raw,
                 'leading_context':'# ', 'trailing_context':'# Next\nNext body.',
                 'eligible_for_name_screening':True,
                 'source':{'identifier':'B001','title':'Test book','md_path':str(self.md),
                           'md_sha256':hashlib.sha256(self.md.read_bytes()).hexdigest(),
                           'head_spans':[[2,8]],'body_spans':[[start,end]]}}
        (folder/'accepted_entries.json').write_text(json.dumps([entry]),encoding='utf-8')
        (folder/'quarantined_entries.json').write_text('[]',encoding='utf-8')
        (folder/'units.json').write_text('[]',encoding='utf-8')
        (folder/'SUMMARY.json').write_text(json.dumps({'identifier':'B001','status':'partial'}),encoding='utf-8')
        return extract, entry

    def test_clean_preparation_preserves_body_id_and_context(self):
        extract, entry = self.make_extraction()
        result = p.prepare_cleaning(self.books, extract, self.root/'clean_input')
        self.assertEqual(len(result),1)
        row = result[0]
        self.assertEqual(row['id'], entry['id'])
        self.assertEqual(row['raw_content'], entry['raw_content'])
        self.assertEqual(row['source_context']['trailing_context'], entry['trailing_context'])
        self.assertNotIn('Next body',row['raw_content'])
        self.assertEqual(row['source']['extraction_book_status'],'partial')

    def test_source_roundtrip_mismatch_rejected(self):
        extract, _ = self.make_extraction(raw='invented body')
        with self.assertRaises(ValueError):
            p.prepare_cleaning(self.books, extract, self.root/'clean_input')

    def test_clean_preparation_resume_rejects_modified_context_snapshot(self):
        extract, _ = self.make_extraction()
        out = self.root / 'clean_input'
        result = p.prepare_cleaning(self.books, extract, out)
        self.assertEqual(p.read(out / 'PREPARED.json')['input_sha256'], p.digest(out / 'INPUT.json'))
        self.assertEqual(p.prepare_cleaning(self.books, extract, out), result)
        result[0]['source_context']['head_window'] = 'Invented context'
        p.write(out / 'INPUT.json', result)
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            p.prepare_cleaning(self.books, extract, out)

    def test_missing_scope_is_not_content_drop(self):
        extract, _ = self.make_extraction()
        with self.assertRaises(ValueError):
            p.prepare_cleaning([{k:v for k,v in self.books[0].items() if k!='scope_config'}],extract,self.root/'clean_input')

    def test_technical_keep_does_not_pass(self):
        self.assertFalse(p.is_passed({'decision':'keep','api_status':'api_error'}))
        self.assertFalse(p.is_passed({'decision':'review','api_status':'ok'}))
        self.assertTrue(p.is_passed({'decision':'keep','api_status':'ok'}))
        self.assertTrue(p.is_passed({'decision':'keep','api_status':'local'}))

    def test_only_semantic_review_gets_short_context(self):
        rows = [{'id':sid,'name':'','knowledge_point':'Energy'} for sid in ('a','b','c')]
        calls = []
        def invoke(cmd, **kwargs):
            target = Path(cmd[cmd.index('--out-dir')+1])
            context = int(cmd[cmd.index('--context-chars')+1])
            values = p.loadl(target/'input.jsonl')
            calls.append((context,[r['id'] for r in values]))
            for row in values:
                row[p.NAME] = {'decision':'keep' if context else 'review',
                               'api_status':'api_error' if row['id']=='c' else 'ok'}
                if row['id']=='a': row[p.NAME]['decision']='keep'
            p.jsonl(target/'llm_name_title_format_all_full.jsonl',values)
        with mock.patch.object(p.subprocess,'run',side_effect=invoke):
            result = p.filter_stage(rows,'name',{'api_url':'http://unused','model':'test','workers':2},self.root/'filter')
        self.assertEqual(calls,[(0,['a','b','c']),(600,['b'])])
        self.assertEqual(result[2][p.NAME]['api_status'],'api_error')

    def test_cleaning_adapter_routes_only_passed_names(self):
        import io
        import clean_boundary_v46 as content
        extract, entry = self.make_extraction()
        rows = p.prepare_cleaning(self.books,extract,self.root/'prepared')
        rows.append({**rows[0],'id':'bad_name'})
        input_path = self.root/'input.json'
        p.write(input_path,rows)
        calls = []
        def stage(values, name, *args):
            calls.append((name,[r['id'] for r in values]))
            key = p.NAME if name=='name' else p.SCOPE
            return [{**r,key:{'decision':'drop' if r['id']=='bad_name' else 'keep','api_status':'ok'}} for r in values]
        def batch(values, *args):
            calls.append(('content',[r['id'] for r in values]))
            return [{'id':r['id'],'status':'keep','record':{k:r.get(k,'') for k in p.STANDARD}} for r in values]
        cfg = {'api_url':'http://unused','model':'test','workers':2,'no_auth':True}
        with mock.patch.object(p.urllib.request,'urlopen',return_value=io.BytesIO(b'{"data":[{"id":"test"}]}')), \
             mock.patch.object(p,'filter_stage',side_effect=stage), \
             mock.patch.object(content.Runner,'batch',side_effect=batch):
            result = p.run_cleaning(input_path,cfg,self.root/'clean',self.books)
        self.assertEqual(calls,[('name',['entry001','bad_name']),('scope',['entry001']),('content',['entry001'])])
        self.assertEqual(result['final_keep'],1)
        self.assertFalse(result['semantic_quality_approved'])
        self.assertEqual(set(p.read(self.root/'clean/STANDARD_RECORDS.json')[0]),set(p.STANDARD))

    def test_unsafe_entry_id_rejected(self):
        extract, _ = self.make_extraction()
        file = extract/'B001'/'accepted_entries.json'
        rows = json.loads(file.read_text())
        rows[0]['id'] = '../../escape'
        file.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):
            p.prepare_cleaning(self.books,extract,self.root/'clean_input')


if __name__ == '__main__':
    unittest.main()
