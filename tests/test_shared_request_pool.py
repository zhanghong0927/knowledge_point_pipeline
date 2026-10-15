"""Global producer capacity; all work is a local deterministic fixture."""
import concurrent.futures
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage_scheduling import ResourceProfile


class ProfileTests(unittest.TestCase):
    def test_one_book_can_use_whole_phase_without_a_fixed_book_quota(self):
        p = ResourceProfile(gpu_cards=32, limit=1024, front_limit=1024,
                            extraction_limit=1024, model_limit=1024)
        for kind in ('production', 'cleaning', 'mounting', 'boundary'):
            self.assertEqual([p.job_workers(kind, n) for n in (1, 2, 3)],
                             [1024, 256, 64])


class GlobalBudgetTests(unittest.TestCase):
    def setUp(self):
        from shared_request_pool import ProducerBudget
        self.pool = ProducerBudget()

    def test_books_share_one_budget_and_survivor_claims_released_slots(self):
        self.assertEqual(self.pool.claim('a', 3, limit=6), 3)
        self.assertEqual(self.pool.claim('b', 3, limit=6), 3)
        self.assertEqual(self.pool.claim('c', 6, limit=6), 0)
        self.pool.release('a', 3)
        self.assertEqual(self.pool.claim('c', 6, limit=6), 3)
        self.pool.close('b'); self.pool.close('c')
        self.assertEqual(self.pool.claim('a', 6, limit=6), 6)
        self.assertEqual(self.pool.active, 6)

    def test_pause_does_not_grant_more_producer_slots(self):
        self.assertEqual(self.pool.claim('a', 6, limit=6, paused=True), 0)

    def test_over_release_is_rejected_without_freeing_other_books_slots(self):
        self.pool.claim('a', 2, limit=4); self.pool.claim('b', 2, limit=4)
        with self.assertRaises(ValueError): self.pool.release('a', 3)
        self.assertEqual(self.pool.active, 4)

    def test_completed_generator_persists_results_before_scheduling_all_inputs(self):
        from shared_request_pool import completed
        persisted = []
        started = []
        lock = threading.Lock()
        def work(value):
            with lock:
                started.append(value)
            return value * 2
        with patch.dict(os.environ, {}, clear=True):
            stream = completed(work, range(20), 2)
            item, future = next(stream)
            persisted.append((item, future.result()))
            self.assertLess(len(started), 20)
            persisted.extend((item, f.result()) for item, f in stream)
        self.assertEqual(sorted(v for _, v in persisted), list(range(0, 40, 2)))

    def test_global_client_budget_is_bounded_across_two_producers(self):
        from shared_request_pool import completed
        pool = self.pool
        live = peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        class Client:
            def __init__(self, *args): self.owner = str(threading.get_ident())
            def claim(self, n): return pool.claim(self.owner, n, limit=4)
            def release(self, n): pool.release(self.owner, n)
            def close(self): pool.close(self.owner)
        def work(n):
            nonlocal live, peak
            with lock: live += 1; peak = max(live, peak)
            time.sleep(.01)
            with lock: live -= 1
            return n
        def producer():
            barrier.wait()
            return [f.result() for _, f in completed(work, range(12), 4)]
        with patch.dict(os.environ, {'PIPELINE_SHARED_POOL_URL': 'http://127.0.0.1:1'}), \
             patch('shared_request_pool.LeaseClient', Client):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(producer) for _ in range(2)]
                self.assertEqual([len(f.result()) for f in futures], [12, 12])
        self.assertEqual(peak, 4)
        self.assertEqual(pool.active, 0)


if __name__ == '__main__': unittest.main()
