"""Request producers share real worker threads and clean up failed reservations."""
import ast
import concurrent.futures
from contextlib import nullcontext, suppress
import csv
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import shared_request_pool as shared
from validate_offline import offline_network_guard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules/book_screening/scripts'))
import general_book_screening_pipeline as important_md


def budget_client(budget, limit, release_delay=0):
    class Client:
        def __init__(self, _url):
            self.owner = uuid.uuid4().hex

        def claim(self, count):
            return budget.claim(self.owner, count, limit=limit)

        def release(self, count):
            budget.release(self.owner, count)
            time.sleep(release_delay)

        def close(self):
            budget.close(self.owner)
    return Client


class SharedExecutorFailureTests(unittest.TestCase):
    def test_claim_error_returns_local_slot_before_propagating(self):
        class Client:
            def __init__(self, _url): pass
            def claim(self, _count): raise OSError('claim reply lost')
            def close(self): pass
        with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
             patch.object(shared, 'LeaseClient', Client):
            executor = shared.SharedExecutor(1)
            try:
                with self.assertRaisesRegex(OSError, 'claim reply lost'):
                    executor.submit(lambda: None)
                released = executor.local.acquire(blocking=False)
                self.assertTrue(released, 'failed claim permanently consumed the local producer slot')
                if released:
                    executor.local.release()
            finally:
                executor.shutdown()

    def test_release_error_closes_owner_and_is_reported_without_losing_local_slot(self):
        budget = shared.ProducerBudget()
        base = budget_client(budget, 1)
        attempted = threading.Event()
        class Client(base):
            def release(self, count):
                attempted.set()
                raise OSError('release reply lost')
        with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
             patch.object(shared, 'LeaseClient', Client):
            executor = shared.SharedExecutor(1)
            self.assertEqual(executor.submit(lambda: 7).result(timeout=2), 7)
            self.assertTrue(attempted.wait(2))
            with self.assertRaisesRegex(OSError, 'release reply lost'):
                executor.shutdown()
            released = executor.local.acquire(blocking=False)
            self.assertTrue(released, 'release failure leaked a local slot')
            if released:
                executor.local.release()
        self.assertEqual(budget.active, 0)

    def test_real_request_threads_stay_bounded_until_release_reply_returns(self):
        budget = shared.ProducerBudget()
        peak = 0
        lock = threading.Lock()
        def work():
            nonlocal peak
            with lock:
                peak = max(peak, sum(t.is_alive() and t.name == 'model-range' for t in threading.enumerate()))
            time.sleep(.005)
            return 1
        def producer():
            executor = shared.SharedExecutor(8)
            try:
                pending = [executor.submit(work) for _ in range(16)]
                return sum(future.result(timeout=5) for future in pending)
            finally:
                executor.shutdown()
        with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
             patch.object(shared, 'LeaseClient', budget_client(budget, 4, .08)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as outer:
                self.assertEqual([f.result(timeout=10) for f in [outer.submit(producer) for _ in range(4)]], [16] * 4)
        self.assertLessEqual(peak, 4, 'worker threads outlived their producer lease')
        self.assertEqual(budget.active, 0)


class NativeAdapterBudgetTests(unittest.TestCase):
    def test_dictionary_classifier_actual_loop_shares_budget_across_books(self):
        sys.path.insert(0, str(ROOT / 'modules/dictionary/classification/scripts'))
        import classify_books as classifier
        budget = shared.ProducerBudget()
        live = peak = 0
        lock = threading.Lock()
        def process(ref, _base, out, _config):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(.01)
            with lock:
                live -= 1
            result = {'ref': ref, 'status': 'technical_failed', 'errors': ['offline fixture']}
            classifier.io.write(out / 'results' / (ref + '.json'), result)
            return result
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            base = root / 'base'
            refs = [str(n) for n in range(16)]
            classifier.io.write(base / 'manifest.json', {'targets': refs})
            for ref in refs:
                classifier.io.write(base / 'prepared' / (ref + '.json'), {})
            classifier.io.write(root / 'config.json', {'api_url': 'http://127.0.0.1:9',
                                                       'model': 'fake', 'context_limit': 32768})
            fake_lock = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None)
            with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
                 patch.dict(sys.modules, {'fcntl': fake_lock}), \
                 patch.object(shared, 'LeaseClient', budget_client(budget, 4)), \
                 patch.object(classifier.io, 'http', return_value={'data': [{'id': 'fake'}]}), \
                 patch.object(classifier.transport, 'process', side_effect=process):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as outer:
                    calls = [outer.submit(classifier.classify, base, root / str(n), root / 'config.json', 8)
                             for n in range(2)]
                    for future in calls:
                        future.result(timeout=10)
            self.assertLessEqual(peak, 4, 'dictionary front retained an independent model pool')
            self.assertEqual(budget.active, 0)

    def test_important_md_actual_audit_loop_shares_budget_across_books(self):
        budget = shared.ProducerBudget()
        live = peak = 0
        lock = threading.Lock()
        def audit(row, **_kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(.01)
            with lock:
                live -= 1
            return {'identifier': row['identifier'], 'audit_status': 'completed', 'ok': True}
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            source = root / 'books.csv'
            with source.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=['identifier', 'title'])
                writer.writeheader()
                writer.writerows({'identifier': str(n), 'title': 'fixture'} for n in range(16))
            with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
                 patch.object(shared, 'LeaseClient', budget_client(budget, 4)), \
                 patch.object(important_md, 'audit_one', side_effect=audit):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as outer:
                    calls = [outer.submit(important_md.run_md_audit, source, None, root / str(n),
                                          'fixture', workers=8) for n in range(2)]
                    self.assertEqual([future.result(timeout=10)['recorded_total'] for future in calls], [16, 16])
            self.assertLessEqual(peak, 4, 'front MD audit still creates an independent model pool per book')
            self.assertEqual(budget.active, 0)

    def test_native_important_verification_dispatch_uses_shared_request_budget(self):
        # Execute the actual dispatcher without importing optional LLM packages.
        # The fake serial request replaces only its external model dependency.
        path = ROOT / 'modules/important_llm/src/book_extractor/synthesis.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name in ('_verify_records', '_request_executor')]
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                                  *definitions], type_ignores=[])
        namespace = {'__file__': str(path), 'Path': Path, 'os': os, 'sys': sys,
                     'ThreadPoolExecutor': concurrent.futures.ThreadPoolExecutor,
                     'FIRST_COMPLETED': concurrent.futures.FIRST_COMPLETED,
                     'wait': concurrent.futures.wait, 'Event': threading.Event,
                     'nullcontext': nullcontext}
        live = peak = 0
        lock = threading.Lock()
        def serial(records, *_args, **_kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(.01)
            with lock:
                live -= 1
            return records, []
        namespace['_verify_records_serial'] = serial
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        budget = shared.ProducerBudget()
        with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:9'}), \
             patch.object(shared, 'LeaseClient', budget_client(budget, 4)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as outer:
                calls = [outer.submit(namespace['_verify_records'], list(range(16)), {}, SimpleNamespace(), Path('.'),
                                      workers=8) for _ in range(2)]
                self.assertEqual([f.result(timeout=10)[0] for f in calls], [list(range(16))] * 2)
        self.assertLessEqual(peak, 4, 'important LLM verification bypassed the shared producer budget')
        self.assertEqual(budget.active, 0)


if __name__ == '__main__':
    unittest.main()
