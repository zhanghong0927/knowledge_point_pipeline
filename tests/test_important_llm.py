import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("important_llm_io", ROOT / "adapters/important_llm_io.py")
IO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IO)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def snapshot(root):
    work, delivery = root / "work", root / "delivery"
    md = root / "b.md"
    md.write_text("gear is a toothed wheel.")
    digest = IO.sha(md)
    book = {"book_id": "b", "local_name": str(md), "sha256": digest, "subject": "mechanical_engineering"}
    write(work / "inputs/manifest.json", {"books": [book]})
    manifest = {"book_id": "b", "run_id": "r", "source_sha256": digest, "status": "complete"}
    write(work / "runs/r/manifest.json", manifest)
    write(delivery / "books/b/manifest.json", manifest)
    row = {"name": "gear", "definition": "A toothed wheel.", "record_id": "rec1", "book_id": "b", "run_id": "r",
           "evidence_ids": ["u1"], "candidate_ids": ["c1"], "issues": [], "aliases": ["gear wheel"]}
    write(delivery / "books/b/records.jsonl", row)
    write(delivery / "books/b/units.json", [{"id": "u1", "text": md.read_text()}])
    write(delivery / "books.json", [{"book_id": "b", "run_id": "r", "title": "Test book",
          "records": "books/b/records.jsonl", "units": "books/b/units.json", "accepted_records": 1}])
    write(delivery / "summary.json", {"counts": {"accepted": 1, "excluded": 0, "input": 1}})
    write(delivery / "checksums.json", {str(p.relative_to(delivery)): IO.sha(p) for p in delivery.rglob('*') if p.is_file()})
    return work, delivery, row


class ImportantLLMTests(unittest.TestCase):
    def test_only_N1_N3_enter_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            books = []
            for n in range(4):
                md = root / f"b{n}.md"
                md.write_text("# heading\ntext")
                books.append({"identifier": f"b{n}", "md_path": str(md), "title": f"b{n}"})
            write(root / "books.json", books)
            write(root / "prepared/manifest.json", {"targets": [b['identifier'] for b in books]})
            def classify(_a, _b, book):
                kind = {"b0": "N1", "b1": "N3", "b2": "OTHER", "b3": None}[book["identifier"]]
                return {"status": "classified" if kind else "needs_review", "primary_class": kind}, {
                    "md_path": book["md_path"], "md_sha256": IO.sha(Path(book["md_path"]))}
            with patch.object(IO, 'validate_classification', side_effect=classify):
                report = IO.prepare(root / 'books.json', root / 'prepared', root / 'classification', root / 'out', 'mechanical_engineering')
            self.assertEqual(report['selected_N1_N3'], 2)
            self.assertEqual(report['excluded'], 2)
            manifest = IO.read(root / 'out/manifest.json')
            self.assertEqual([b['book_id'] for b in manifest['books']], ['b0', 'b1'])
            self.assertTrue(all(Path(b['local_name']).is_absolute() for b in manifest['books']))

    def test_duplicate_book_ids_fail(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root / 'books.json', [{'identifier': 'b'}, {'identifier': 'b'}])
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                IO.prepare(root / 'books.json', root / 'p', root / 'c', root / 'out', 'mechanical_engineering')

    def test_consolidation_preserves_native_fields_and_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work, delivery, original = snapshot(root)
            result = IO.consolidate(delivery, work, root/'out', '机械工程', 'mechanical_engineering')
            row = IO.read(root/'out/records.jsonl')
            self.assertEqual(result['records'], 1)
            for k,v in original.items(): self.assertEqual(row[k], v)
            self.assertEqual(row['tag'], '机械工程')
            self.assertEqual(row['source']['record_id'], original['record_id'])
            self.assertEqual(row['source']['evidence_ids'], ['u1'])
            self.assertTrue(row['id'].startswith('llm:'))

    def test_corrupt_snapshot_and_active_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work, delivery, _ = snapshot(root)
            lock = work/'runs/r/.running'
            lock.write_text('active')
            with self.assertRaisesRegex(ValueError, 'stopped'):
                IO.consolidate(delivery, work, root/'out', '机械工程', 'mechanical_engineering')
            lock.unlink()
            (delivery/'books/b/records.jsonl').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                IO.consolidate(delivery, work, root/'out', '机械工程', 'mechanical_engineering')

    def test_changed_run_after_export_and_wrong_subject_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work, delivery, _ = snapshot(root)
            with self.assertRaisesRegex(ValueError, 'Mixed subjects'):
                IO.consolidate(delivery, work, root/'out', '历史学', 'history')
            manifest = IO.read(work/'runs/r/manifest.json')
            manifest['status'] = 'partial'
            write(work/'runs/r/manifest.json',manifest)
            with self.assertRaisesRegex(ValueError, 'changed after delivery'):
                IO.consolidate(delivery, work, root/'out', '机械工程', 'mechanical_engineering')

    def test_plan_selects_independent_llm_python_and_standard_cleaning(self):
        spec = importlib.util.spec_from_file_location('pipeline',ROOT/'pipeline.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        cfg,v = pipeline.load_config(ROOT/'configs/pipeline.important_llm.example.json')
        stages = pipeline.plan(cfg,v)
        task = next(t for t in stages[2]['tasks'] if t['name']=='important_llm_extract')
        self.assertIn('.venv_important_llm', task['command'][0])
        self.assertIn('book_extractor.cli',task['command'])
        self.assertNotIn('extract_N1_N3',str(stages[2]))
        self.assertIn('standard',stages[3]['tasks'][0]['command'])
        cfg['important_extraction']='rule'
        self.assertIn('extract_N1_N3',str(pipeline.plan(cfg,v)[2]))


if __name__ == '__main__':
    unittest.main()
