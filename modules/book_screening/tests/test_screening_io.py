import csv
import json
from pathlib import Path
import tempfile
import unittest

import screening_io as io


class ScreeningIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.md = self.root/'source.md'
        self.md.write_text('# Energy\nEnergy is a physical quantity.\n',encoding='utf-8')
        self.scope = self.root/'scope.json'
        self.scope.write_text(json.dumps({'subject_name':'Physics','subject_slug':'physics',
            'boundary':{'strong_terms':['physics'],'core_scope':'Physical quantities'}}),encoding='utf-8')
        self.source = self.root/'books.csv'
        self.rows = [{'identifier':'original-id','title':'Original title','parsed_path':str(self.md),'book_track':'辞海类'}]
        self.write(self.source,self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self,path,rows):
        io.write_csv(path,list(dict.fromkeys(k for r in rows for k in r)),rows)

    def prepared(self):
        target = self.root/'prepared'
        io.prepare_dictionary(self.source,self.scope,target,[])
        return target,json.loads((target/'FILEMAP.json').read_text(encoding='utf-8'))

    def test_split_preserves_both_tracks_and_ids(self):
        rows = self.rows+[{**self.rows[0],'identifier':'important-id','book_track':'其他重要书籍'}]
        self.write(self.source,rows)
        result = io.split_tracks(self.source,self.root/'split')
        self.assertEqual(result['counts'],{'辞海类':1,'其他重要书籍':1})
        self.assertEqual(io.read_csv(self.root/'split/dictionary.csv')[1][0]['identifier'],'original-id')

    def test_duplicate_ids_are_not_silently_merged(self):
        self.write(self.source,self.rows*2)
        with self.assertRaises(ValueError): io.split_tracks(self.source,self.root/'split')

    def test_preparation_preserves_source_and_creates_usable_config(self):
        target,mapping = self.prepared()
        self.assertEqual(mapping['records'][0]['status'],'ready')
        self.assertEqual(Path(mapping['records'][0]['local_path']).read_bytes(),self.md.read_bytes())
        config = json.loads((target/'audit_config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['subjects'][0]['name'],'Physics')
        self.assertIn('Physical quantities',config['subjects'][0]['extraction_focus'])

    def test_missing_md_is_review_not_drop(self):
        self.rows[0]['parsed_path']=str(self.root/'missing.md')
        self.write(self.source,self.rows)
        target,mapping = self.prepared()
        audit = self.root/'audit.csv'
        io.write_csv(audit,['subject','file_name','decision','source_path'],[])
        io.finalize_dictionary(target,audit,self.root/'final')
        row = io.read_csv(self.root/'final/最终审核结果.csv')[1][0]
        self.assertEqual(row['final_decision'],'REVIEW')
        self.assertEqual(row['pipeline_failure_type'],'md_unavailable')

    def test_missing_audit_is_review_not_drop(self):
        target,_ = self.prepared()
        audit = self.root/'audit.csv'
        io.write_csv(audit,['subject','file_name','decision','source_path'],[])
        io.finalize_dictionary(target,audit,self.root/'final')
        self.assertEqual(io.read_csv(self.root/'final/最终审核结果.csv')[1][0]['pipeline_failure_type'],'missing_audit')

    def test_v3_result_returns_original_identifier(self):
        target,mapping = self.prepared()
        item = mapping['records'][0]
        audit = self.root/'audit.csv'
        self.write(audit,[{'subject':'Physics','file_name':item['file_name'],'source_path':item['local_path'],
            'decision':'PASS','summary':'Suitable dictionary'}])
        result = io.finalize_dictionary(target,audit,self.root/'final')
        row = io.read_csv(self.root/'final/最终审核结果.csv')[1][0]
        self.assertEqual(row['identifier'],'original-id')
        self.assertEqual(row['title'],'Original title')
        self.assertEqual(row['final_decision'],'PASS')
        self.assertTrue(result['coverage_ok'])

    def test_changed_source_refuses_finalization(self):
        target,_ = self.prepared()
        self.md.write_text('Changed source',encoding='utf-8')
        audit = self.root/'audit.csv'
        io.write_csv(audit,['subject','file_name','decision','source_path'],[])
        with self.assertRaises(ValueError): io.finalize_dictionary(target,audit,self.root/'final')

    def test_unexpected_audit_record_is_rejected(self):
        target,_ = self.prepared()
        audit = self.root/'audit.csv'
        self.write(audit,[{'subject':'Physics','file_name':'unrelated.md','source_path':str(self.md),'decision':'PASS'}])
        with self.assertRaises(ValueError): io.finalize_dictionary(target,audit,self.root/'final')

    def test_source_path_mismatch_is_rejected(self):
        target,mapping = self.prepared()
        audit = self.root/'audit.csv'
        self.write(audit,[{'subject':'Physics','file_name':mapping['records'][0]['file_name'],
            'source_path':str(self.root/'different.md'),'decision':'PASS'}])
        with self.assertRaises(ValueError): io.finalize_dictionary(target,audit,self.root/'final')

    def test_existing_prepared_directory_is_not_overwritten(self):
        target,_ = self.prepared()
        with self.assertRaises(FileExistsError): io.prepare_dictionary(self.source,self.scope,target,[])

    def test_two_final_tracks_merge_without_reindexing(self):
        d = self.root/'dictionary.csv'
        k = self.root/'important.csv'
        self.write(d,[{**self.rows[0],'final_decision':'PASS','audit_status':'completed'}])
        self.write(k,[{**self.rows[0],'identifier':'important-id','book_track':'其他重要书籍',
            'final_decision':'REVIEW','audit_status':'api_failed'}])
        result = io.merge_results([d,k],self.root/'final')
        self.assertEqual(result['total'],2)
        self.assertEqual(result['technical_failures'],1)
        self.assertEqual([r['identifier'] for r in io.read_csv(self.root/'final/最终审核结果.csv')[1]],
            ['original-id','important-id'])


if __name__=='__main__':
    unittest.main()
