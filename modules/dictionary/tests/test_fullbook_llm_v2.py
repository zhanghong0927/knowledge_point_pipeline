import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fullbook_llm_v2 as v2
from test_fullbook_llm_extract import ExtractTests, answer


class V2Tests(unittest.TestCase):
    def test_repeated_header_diagnostic(self):
        data=answer()
        data['entries'][0]['head'][0]['unit']=5
        with self.assertRaisesRegex(ValueError,'Entry 0: head unit 5 is AFTER'):
            v2.validate_checked(data,{},'',{})

    def test_structure_and_lossless(self):
        text = '# Title\r\n\r\n| A | B |\r\n|---|---|\r\n| x | y |\r\n\r\n```py\nprint(1)\n```\n\n$$\nx+y\n$$\n'
        rows = v2.structural_units(text)
        self.assertEqual(''.join(r['text'] for r in rows), text)
        kinds = {r['kind'] for r in rows}
        self.assertTrue({'table', 'fence', 'math_block'} <= kinds)
        for row in rows:
            self.assertEqual(text[row['offset']:row['offset']+len(row['text'])],row['text'])

    def test_global_overlap_not_just_neighbors(self):
        entries = [{'source':{'head_spans':[[i,i+1]],'body_spans':body}}
                   for i,body in [(0,[[2,100]]),(10,[]),(20,[])]]
        v2.mark_conflicts(entries)
        self.assertTrue(all(r['structural_review_required'] for r in entries))

    def test_oversize_protected_structure_explicit(self):
        rows = v2.structural_units('```\n'+'x'*10000+'\n```\n')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['kind'],'fence')

    def test_formula_with_blank_lines(self):
        text='$$\nx+y\n\nz+w\n$$\n'
        rows=v2.structural_units(text)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['text'],text)
        self.assertEqual(rows[0]['kind'],'math_block')

    def test_cache_stats_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            args = ExtractTests().make_runner(root).args
            args.overlap = 0
            path = Path(root)/'book.md'
            path.write_text('Learning\n\nA process.\n',encoding='utf-8')
            spec = {'identifier':'test','title':'Test','md_path':str(path)}
            runner = v2.Runner(args)
            data = answer()
            # The blank gap is a separate source unit.
            data['entries'][0]['body'][0]['start']['unit'] = 2
            data['entries'][0]['body'][0]['end']['unit'] = 2
            import json
            runner.api = lambda *a: {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}],
                                    'usage':{'prompt_tokens':123,'completion_tokens':45}}
            first = runner.run_book(spec)
            self.assertEqual(first['entries'],1)
            self.assertEqual(first['execution']['requests'],1)
            self.assertEqual(first['execution']['prompt_tokens'],123)
            runner.api = lambda *a: self.fail('Cache should avoid requests')
            second = runner.run_book(spec)
            self.assertEqual(second['execution']['requests'],0)
            self.assertEqual(second['execution']['cache_hits'],1)

    def test_storage_error_never_reissues_api(self):
        with tempfile.TemporaryDirectory() as root:
            args = ExtractTests().make_runner(root).args
            path = Path(root)/'book.md';path.write_text('Learning\n',encoding='utf-8')
            runner = v2.Runner(args)
            calls = []
            def api(*a):
                calls.append(1)
                return {'choices':[{'finish_reason':'stop','message':{'content':'{"entries":[],"scanned_all":true}'}}]}
            runner.api = api
            original = v2.write
            def broken(path,value):
                if Path(path).parent.name == 'responses':
                    raise OSError('disk test')
                original(path,value)
            with patch.object(v2,'write',broken), self.assertRaises(OSError):
                runner.run_book({'identifier':'test','title':'Test','md_path':str(path)})
            self.assertEqual(len(calls),1)


if __name__ == '__main__':
    unittest.main()
