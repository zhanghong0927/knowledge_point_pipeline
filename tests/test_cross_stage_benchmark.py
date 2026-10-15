import importlib
import threading
import unittest
from pathlib import Path
import tempfile
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import patch


class CrossStageBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.run_flow = importlib.import_module('cross_stage_benchmark').run_book_flow

    def test_subject_ready_for_mount_does_not_wait_for_other_subject(self):
        mounted = threading.Event()
        def prepare(item):
            return item
        def process(item):
            if item['subject'] == 'slow':
                if not mounted.wait(5):
                    raise AssertionError('Subject mounting waited for unrelated processing')
            return {**item, 'production_job': item, 'result': {'status': 'completed'}}
        def mount(subject, jobs):
            if subject == 'fast':
                mounted.set()
            return {'subject': subject, 'status': 'completed', 'jobs': jobs}
        specs = [{'key': 'a', 'subject': 'fast'}, {'key': 'b', 'subject': 'slow'}]
        results, mounts = self.run_flow(specs, prepare, process, mount,
            prepare_workers=2, process_workers=2, mount_workers=1, max_pending=2)
        self.assertEqual(len(results), 2)
        self.assertEqual({m['subject'] for m in mounts}, {'fast', 'slow'})

    def test_failed_or_nonadmitted_books_do_not_reach_mounting(self):
        specs = [{'key': k, 'subject': 'same'} for k in ('keep', 'drop', 'failed')]
        def process(item):
            if item['key'] == 'drop':
                return {**item, 'disposition': 'not_admitted'}
            return {**item, 'production_job': item,
                    'result': {'status': 'failed' if item['key'] == 'failed' else 'completed'}}
        def mount(subject, jobs):
            return {'subject': subject, 'status': 'completed', 'ids': [j['key'] for j in jobs]}
        _, mounts = self.run_flow(specs, lambda x: x, process, mount, max_pending=1)
        self.assertEqual(mounts[0]['ids'], ['keep'])

    def test_duplicate_book_keys_fail_before_any_tasks_run(self):
        item = {'key': 'same', 'subject': 'x'}
        with self.assertRaises(ValueError):
            self.run_flow([item, item], lambda x: x, lambda x: x, lambda s, j: None)

    def test_empty_books_do_not_create_mounting_jobs(self):
        self.assertEqual(self.run_flow([], lambda x: x, lambda x: x, lambda s, j: None), ([], []))

    def test_closed_selection_does_not_wait_for_unselected_candidates(self):
        mounted = threading.Event()
        specs = [{'key': key, 'subject': 'same'} for key in ('selected', 'reserve')]
        def prepare(item):
            if item['key'] == 'reserve':
                if not mounted.wait(5):
                    raise AssertionError('Selected books waited for an unselected candidate')
            return item
        def process(item):
            if item['key'] == 'reserve':
                return {**item, 'disposition': 'not_selected'}
            return {**item, 'production_job': item, 'result': {'status': 'completed'}}
        def mount(subject, jobs):
            mounted.set()
            return {'subject': subject, 'status': 'completed'}
        _, mounts = self.run_flow(specs, prepare, process, mount, prepare_workers=2,
            process_workers=2, selection=lambda: (True, {'selected'}))
        self.assertEqual(len(mounts), 1)

    def test_job_route_names_are_bounded_without_changing_book_identity(self):
        module = importlib.import_module('cross_stage_benchmark')
        route = module.compact_job_id('dictionary', 'extract', 'a' * 240)
        self.assertLessEqual(len(route), 128)
        self.assertEqual(route, module.compact_job_id('dictionary', 'extract', 'a' * 240))
        self.assertNotEqual(route, module.compact_job_id('dictionary', 'extract', 'b' * 240))

    def test_ready_boundary_does_not_wait_for_unrelated_boundary(self):
        module = importlib.import_module('cross_stage_benchmark')
        release = threading.Event()
        def generate(subject):
            if subject == 'slow':
                release.wait(5)
            return {'subject': subject, 'status': 'completed'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'run'
            root.mkdir()
            manager = module.BoundaryWarmup(root, ['fast', 'slow'], {}, None, None, None)
            with patch.object(manager, 'generate', side_effect=generate), manager:
                try:
                    self.assertEqual(manager.wait('fast')['status'], 'completed')
                    self.assertFalse(release.is_set())
                    with self.assertRaises(ValueError):
                        manager.wait('missing')
                finally:
                    release.set()

    def test_important_source_revalidation_uses_its_own_manifest_schema(self):
        module = importlib.import_module('cross_stage_benchmark')
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / 'run/02_structure/prepared'
            classified = home / 'run/02_structure/classification'
            (base / 'prepared').mkdir(parents=True)
            classified.mkdir(parents=True)
            md = home / 'book.md'
            md.write_text('Original text', encoding='utf-8')
            def digest(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()
            packet = base / 'prepared/book.json'
            packet.write_text(json.dumps({'md_path': str(md), 'md_sha256': digest(md),
                                         'pdf_path': None}), encoding='utf-8')
            prepared_manifest = base / 'manifest.json'
            prepared_manifest.write_text(json.dumps({'prepared_hashes': {'book': digest(packet)}}), encoding='utf-8')
            (classified / 'manifest.json').write_text(json.dumps({'base': str(base),
                'base_manifest_sha256': digest(prepared_manifest)}), encoding='utf-8')
            proof = classified / 'verification.json'
            proof.write_text('{}', encoding='utf-8')
            admission = {'approval_proof': str(proof), 'approval_sha256': digest(proof),
                         'book': {'identifier': 'book'}, 'parent_job': {'home': str(home)}}
            module.revalidate_admission(admission)
            md.write_text('Changed text', encoding='utf-8')
            with self.assertRaises(ValueError):
                module.revalidate_admission(admission)

    def test_selection_does_not_close_before_last_admission_decision(self):
        module = importlib.import_module('cross_stage_benchmark')
        entered, release = threading.Event(), threading.Event()
        class PausedAdmission(dict):
            def __getitem__(self, key):
                if key == 'admitted':
                    entered.set()
                    release.wait(5)
                return super().__getitem__(key)
        def flow(specs, prepare, process, mount, **options):
            item = PausedAdmission(key='book', subject='test', admitted=[],
                front_result={'status': 'completed'}, classification_technical_failed=0)
            results = []
            thread = threading.Thread(target=lambda: results.append(process(item)))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(options['selection']()[0], 'Selection closed before decision was recorded')
            finally:
                release.set()
                thread.join(5)
            self.assertTrue(options['selection']()[0])
            return results, []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = root / 'candidates.json'
            candidates.write_text(json.dumps([{'identifier': 'book', 'subject_slug': 'test'}]), encoding='utf-8')
            recorder = SimpleNamespace(state={'capacity': {'minimum_available_memory_bytes': 1,
                'memory_reservation_per_job_bytes': 1, 'front_workers': 1, 'production_workers': 1,
                'mount_workers': 1, 'ready_queue_limit': 1}}, update=lambda **kw: None)
            fake_psutil = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=100))
            with patch.object(module, 'run_book_flow', side_effect=flow), patch.dict('sys.modules', {'psutil': fake_psutil}):
                module.run_track(root, 'dictionary', candidates, {}, None, recorder)

    def test_recovery_selects_only_explicit_413_failed_books(self):
        module = importlib.import_module('cross_stage_benchmark')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failures = []
            admissions = []
            for key, error in [('too_big', 'HTTP Error 413: Request Entity Too Large'),
                               ('timeout', 'HTTP Error 504: timeout')]:
                home = root / key
                raw = home / 'run/03_extraction/raw'
                raw.mkdir(parents=True)
                (raw / 'SUMMARY.json').write_text(json.dumps({'books': [{'error': error}]}), encoding='utf-8')
                failures.append({'key': key, 'production_job': {'home': str(home)}})
                admissions.append({'book': {'identifier': key}})
            (root / 'dictionary').mkdir()
            (root / 'dictionary/SUMMARY.json').write_text(json.dumps({'failed_book_jobs': failures}), encoding='utf-8')
            self.assertEqual(module.collect_413_recovery(root, admissions), [admissions[0]])


if __name__ == '__main__':
    unittest.main()
