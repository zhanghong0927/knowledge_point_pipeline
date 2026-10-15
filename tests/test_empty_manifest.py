import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('empty_manifest', ROOT / 'adapters/screened_books_to_manifest.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class EmptyManifestTests(unittest.TestCase):
    def test_dictionary_empty_admission_requires_matching_completion_proof(self):
        from adapters.approve_dictionary_books import convert
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            classification = base / 'classification'
            classification.mkdir()
            books = base / 'books.json'
            books.write_text('[]', encoding='utf-8')
            (base / 'manifest.json').write_text(json.dumps({'books': []}), encoding='utf-8')
            (classification / 'manifest.json').write_text(json.dumps({'targets': [], 'base': str(base)}), encoding='utf-8')
            (classification / 'DONE.json').write_text(json.dumps({'id_sets_equal': True}), encoding='utf-8')
            with self.assertRaises(ValueError):
                convert(books, classification)
            result = convert(books, classification, allow_empty=True)
            self.assertEqual(result[0], [])
            self.assertEqual(result[-1]['input_books'], 0)
            (classification / 'DONE.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                convert(books, classification, allow_empty=True)

    def test_empty_filtered_not_unresolved_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'audited.json'
            source.write_text('[]', encoding='utf-8')
            argv = ['--input', str(source), '--out', str(Path(directory) / 'out'),
                    '--track', 'dictionary', '--subject-slug', 'test']
            self.assertFalse(mod.convert(mod.parser().parse_args(argv))[-1]['ready_for_classification'])
            self.assertTrue(mod.convert(mod.parser().parse_args(argv + ['--allow-empty']))[-1]['ready_for_classification'])
            source.write_text(json.dumps([{'identifier': 'x', 'title': 'Book',
                'book_track': 'dictionary', 'final_decision': 'PASS', 'md_path': '/not-found.md'}]), encoding='utf-8')
            report = mod.convert(mod.parser().parse_args(argv + ['--allow-empty', '--allow-partial']))[-1]
            self.assertFalse(report['ready_for_classification'])
            self.assertEqual(report['unresolved'], 1)


if __name__ == '__main__':
    unittest.main()
