import json
from pathlib import Path
import tempfile
import unittest
from classify_books import prepare


class PortableEntryTests(unittest.TestCase):
    def test_prepare_new_book_without_history(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = root/'new.md'
            md.write_text('\n\n'.join('## Term '+str(i)+'\n\nThis is a definition of the term.' for i in range(40)), encoding='utf-8')
            books = root/'books.json'
            books.write_text(json.dumps([{'identifier':'new001','title':'New book','md_path':str(md)}]), encoding='utf-8')
            prepare(books, root/'prepared')
            packet = json.loads((root/'prepared/prepared/new001.json').read_text(encoding='utf-8'))
            self.assertTrue(packet['windows'])
            self.assertEqual(packet['pdf_format_error'], 'pdf_not_supplied')
            rows = md.read_text().splitlines()
            for window in packet['windows']:
                for row in window['lines']:
                    self.assertEqual(row['text'],rows[int(row['id'].split(':')[1])-1])

    def test_reject_path_traversal_id(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'books.json'
            path.write_text(json.dumps([{'identifier':'../unsafe'}]))
            with self.assertRaises(ValueError): prepare(path,Path(d)/'out')

    def test_bad_source_recorded_not_content_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'books.json').write_text(json.dumps([{'identifier':'missing','title':'Missing','md_path':str(root/'missing.md')}]))
            prepare(root/'books.json',root/'out')
            value=json.loads((root/'out/preparation_failed/missing.json').read_text(encoding='utf-8'))
            self.assertEqual(value['status'],'technical_failed')
