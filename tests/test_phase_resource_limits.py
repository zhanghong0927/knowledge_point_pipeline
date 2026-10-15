import threading
import time
import unittest
from unittest.mock import patch

import staged_benchmark
from stage_scheduling import ResourceProfile


class PhaseResourceLimitsTests(unittest.TestCase):
    def test_front_has_book_slots_instead_of_unused_single_book_workers(self):
        profile = ResourceProfile()
        self.assertTrue(hasattr(profile, 'round_limits_for'), 'per-stage limits are required')
        self.assertEqual(profile.round_limits_for('front'), (64, 16, 4))
        self.assertEqual([profile.phase_workers('front', n) for n in (1, 2, 3)], [64, 16, 4])
        self.assertEqual([profile.job_workers('front', n) for n in (1, 2, 3)], [1, 1, 1])
        self.assertEqual(profile.cpu_workers, 8)

    def test_extraction_cleaning_and_mounting_keep_global_recovery_caps(self):
        profile = ResourceProfile()
        self.assertTrue(hasattr(profile, 'round_limits_for'))
        for kind in ('production', 'cleaning', 'mounting', 'boundary'):
            self.assertEqual(profile.round_limits_for(kind), (128, 32, 8))
            self.assertEqual([profile.job_workers(kind, n) for n in (1, 2, 3)], [128, 32, 8])
            for n in (1, 2, 3):
                self.assertLessEqual(profile.phase_workers(kind, n),
                                     profile.round_limits_for(kind)[n - 1])
                self.assertEqual(profile.job_workers(kind, n), profile.round_limits_for(kind)[n - 1])
        self.assertEqual(profile.global_limit, 128)
        self.assertEqual(profile.declared_request_budget, 512)

    def test_candidates_are_configurable_and_not_reported_as_calibrated(self):
        self.assertTrue(hasattr(ResourceProfile(), 'round_limits_for'))
        profile = ResourceProfile(front_limit=32, extraction_limit=96, model_limit=64)
        self.assertEqual(profile.round_limits_for('front'), (32, 8, 2))
        self.assertEqual(profile.round_limits_for('production'), (96, 24, 6))
        self.assertEqual(profile.round_limits_for('cleaning'), (64, 16, 4))
        self.assertFalse(profile.as_dict()['performance_calibrated'])
        self.assertEqual(profile.as_dict()['limit_origin'], 'codex_candidate_pending_calibration')
        self.assertEqual(ResourceProfile(limit=32).round_limits_for('front'), (32, 8, 2))
        self.assertEqual(ResourceProfile(limit=32).round_limits_for('production'), (32, 8, 2))
        for kwargs in ({'front_limit': 0}, {'model_limit': True}, {'extraction_limit': 513}):
            with self.assertRaises(ValueError):
                ResourceProfile(**kwargs)

    def test_calibration_evidence_must_match_applied_stage_limits(self):
        import json
        from pathlib import Path
        import tempfile
        def result(limit):
            return {'selected_limit': limit, 'levels': [{'concurrency': limit,
                'requests': 64, 'protocol_valid': 64, 'transport_errors': 0,
                'wall_seconds': 100, 'p95_seconds': 90, 'population_sha256': 'a' * 64}]}
        report = {'schema': 1, 'status': 'completed', 'front': result(32),
                  'extraction': result(96), 'model_limit': 96,
                  'service': {'api_url': 'http://127.0.0.1:1', 'model': 'fake', 'configured_context': 32768},
                  'semantic_quality_approved': False}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.json'
            path.write_text(json.dumps(report))
            profile = ResourceProfile(front_limit=32, extraction_limit=96, model_limit=96)
            evidence = staged_benchmark.calibration_evidence(path, profile)
            measured = ResourceProfile(front_limit=32, extraction_limit=96, model_limit=96,
                                        calibration_evidence=evidence)
            self.assertTrue(measured.as_dict()['performance_calibrated'])
            self.assertEqual(measured.as_dict()['performance_calibration_scope'],
                             ['front', 'dictionary_extraction'])
            with self.assertRaises(ValueError):
                staged_benchmark.calibration_evidence(path, ResourceProfile())
            with self.assertRaises(ValueError):
                staged_benchmark.calibration_evidence(path, profile,
                    dict(report['service'], model='different-model'))
            with self.assertRaises(ValueError):
                staged_benchmark.calibration_evidence(path,
                    ResourceProfile(limit=32, front_limit=32, extraction_limit=96, model_limit=96))

    def test_front_cpu_pool_eight_does_not_limit_model_pool_to_eight(self):
        self.assertTrue(hasattr(staged_benchmark, 'run_structure_jobs'),
                        'front preparation and model execution must be separate')
        profile = ResourceProfile()
        active_cpu = active_model = peak_cpu = peak_model = 0
        lock = threading.Lock()
        release = threading.Event()
        jobs = [{'job_id': str(index)} for index in range(80)]
        results = []
        failures = []

        def selected(job, stage, *names, **_kwargs):
            nonlocal active_cpu, active_model, peak_cpu, peak_model
            if names == ('convert_audited_books', 'prepare_structure'):
                with lock:
                    active_cpu += 1
                    peak_cpu = max(peak_cpu, active_cpu)
                time.sleep(.002)
                with lock:
                    active_cpu -= 1
            elif names == ('classify',):
                with lock:
                    active_model += 1
                    peak_model = max(peak_model, active_model)
                if not release.wait(5):
                    raise AssertionError('model test release timeout')
                with lock:
                    active_model -= 1
            else:
                raise AssertionError(names)
            return {'job_id': job['job_id'], 'status': 'completed'}

        def run():
            try:
                results.extend(staged_benchmark.run_structure_jobs(jobs, profile))
            except BaseException as exc:
                failures.append(exc)

        with patch('staged_benchmark.selected_job', side_effect=selected):
            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 4
            try:
                while peak_model < 64 and not failures and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertEqual(peak_model, 64)
                self.assertLessEqual(peak_cpu, 8)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertEqual([row['job_id'] for row in results], [job['job_id'] for job in jobs])

    def test_failed_cpu_preparation_never_enters_model_pool(self):
        self.assertTrue(hasattr(staged_benchmark, 'run_structure_jobs'))
        calls = []

        def selected(job, _stage, *names):
            calls.append((job['job_id'], names))
            return {'job_id': job['job_id'],
                    'status': 'failed' if job['job_id'] == 'bad' else 'completed'}

        with patch('staged_benchmark.selected_job', side_effect=selected):
            rows = staged_benchmark.run_structure_jobs(
                [{'job_id': 'bad'}, {'job_id': 'good'}], ResourceProfile())
        self.assertNotIn(('bad', ('classify',)), calls)
        self.assertEqual([row['status'] for row in rows], ['failed', 'completed'])

    def test_important_only_plan_keeps_rule_extraction_and_front_caps(self):
        import contextlib
        import io
        import json
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = staged_benchmark.main(['plan', '--tracks', 'important'])
            except SystemExit:
                self.fail('important-only continuation must be supported')
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue())
        self.assertEqual([track['name'] for track in document['tracks']], ['important'])
        self.assertIn('N1_N3_rule_extraction', document['tracks'][0]['phases'])
        self.assertEqual(document['resource_profile']['phase_round_limits']['front'], [64, 16, 4])
        self.assertEqual(document['model_calls'], 0)

    def test_dictionary_preparation_retains_strict_pdf_checks(self):
        import inspect
        import sys
        import tempfile
        import json
        from pathlib import Path
        directory = Path(__file__).resolve().parents[1] / 'modules/dictionary/classification/scripts'
        sys.path.insert(0, str(directory))
        import classify_books
        self.assertIn('strict_pdf', inspect.signature(classify_books.prepare).parameters)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'book.md').write_text('entry means definition\n' * 30, encoding='utf-8')
            (root / 'book.pdf').write_text('bad PDF', encoding='utf-8')
            (root / 'books.json').write_text(json.dumps([{'identifier': 'b', 'title': 'book',
                'md_path': str(root / 'book.md'), 'pdf_path': str(root / 'book.pdf')}]), encoding='utf-8')
            classify_books.prepare(root / 'books.json', root / 'prepared', strict_pdf=True)
            self.assertTrue((root / 'prepared/preparation_failed/b.json').exists())
            self.assertFalse((root / 'prepared/prepared/b.json').exists())

    def test_empty_important_preparation_is_a_zero_model_call_handoff(self):
        import inspect
        import sys
        import tempfile
        import json
        from pathlib import Path
        directory = Path(__file__).resolve().parents[1] / 'modules/important_books/scripts'
        sys.path.insert(0, str(directory))
        import layout_model_validation
        self.assertIn('allow_empty', inspect.signature(layout_model_validation.prepare).parameters)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'books.json').write_text('[]', encoding='utf-8')
            with patch.object(layout_model_validation, 'http_json',
                              side_effect=AssertionError('model request during CPU preparation')):
                summary = layout_model_validation.prepare(root / 'books.json', None, None,
                                                          root / 'prepared', allow_empty=True)
            self.assertEqual(summary, {'expected': 0, 'prepared': 0, 'failures': 0})
            self.assertEqual(json.loads((root / 'prepared/manifest.json').read_text())['targets'], [])


if __name__ == '__main__':
    unittest.main()
