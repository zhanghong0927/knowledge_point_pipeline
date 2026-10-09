import unittest,tempfile,json
from pathlib import Path
import core
class CoreTests(unittest.TestCase):
    def test_load_array_and_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data';p.write_text('[{"name":"x"}]');self.assertEqual(core.load_records(p),[{'name':'x'}])
            p.write_text('{"name":"x"}\n{"name":"y"}\n');self.assertEqual(len(core.load_records(p)),2)
    def test_bad_rows_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data';p.write_text('{}\nbroken\n')
            with self.assertRaises(ValueError):core.load_records(p)
            p.write_text('[{},null]')
            with self.assertRaises(ValueError):core.load_records(p)
    def test_only_semantic_nonpasses_selected(self):
        self.assertEqual([x for x in ['reasonable','unreasonable','uncertain','technical_failure'] if core.needs_mount(x)],['unreasonable','uncertain'])
    def test_duplicate_or_missing_ids_rejected(self):
        core.verify_ids([{'id':'a'},{'id':'b'}],{'a','b'},'id')
        for r in [[{'id':'a'},{'id':'a'}],[{'id':'a'}]]:
            with self.assertRaises(ValueError):core.verify_ids(r,{'a','b'},'id')
    def test_cache_key_ignores_only_transport_id(self):
        self.assertEqual(core.content_key({'request_id':'a','name':'x'}),core.content_key({'name':'x','request_id':'b'}))
        self.assertNotEqual(core.content_key({'name':'x'}),core.content_key({'name':'y'}))
    def test_changed_resume_assets_are_rejected(self):
        core.require_equal({'input':'a','prompt':'p'},{'input':'a','prompt':'p'},'review')
        for changed in [{'input':'b','prompt':'p'},{'input':'a','prompt':'q'}]:
            with self.assertRaises(ValueError):core.require_equal({'input':'a','prompt':'p'},changed,'review')
if __name__=='__main__':unittest.main()
