import threading
import time
import unittest

from batch_pipeline import bounded_pipeline


class CrossStageSchedulerTests(unittest.TestCase):
    def test_first_result_arrives_before_slow_later_preparation_finishes(self):
        later_started = threading.Event()
        release_later = threading.Event()
        first_result = threading.Event()
        received = []
        errors = []

        def prepare(item):
            if item == 1:
                later_started.set()
                if not release_later.wait(5):
                    raise TimeoutError('later preparation was not released')
            return item

        def consume():
            try:
                for result in bounded_pipeline([0, 1], prepare, lambda x: x,
                                               prepare_workers=2, process_workers=1):
                    received.append(result)
                    first_result.set()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            self.assertTrue(later_started.wait(2))
            self.assertTrue(first_result.wait(2))
            self.assertEqual(received, [0])
            self.assertFalse(release_later.is_set())
        finally:
            release_later.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, [0, 1])

    def test_preparation_continues_while_prior_processing_runs(self):
        processing_started = threading.Event()
        later_prepared = threading.Event()
        release_processing = threading.Event()
        results = []
        errors = []

        def prepare(item):
            if item == 1:
                later_prepared.set()
            return item

        def process(item):
            if item == 0:
                processing_started.set()
                if not release_processing.wait(5):
                    raise TimeoutError('processing was not released')
            return item

        def consume():
            try:
                results.extend(bounded_pipeline([0, 1], prepare, process,
                                                prepare_workers=1, process_workers=1))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            self.assertTrue(processing_started.wait(2))
            self.assertTrue(later_prepared.wait(2))
        finally:
            release_processing.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [0, 1])

    def test_process_results_arrive_in_completion_order(self):
        first_started = threading.Event()
        release_first = threading.Event()
        second_received = threading.Event()
        results = []
        errors = []

        def process(item):
            if item == 0:
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError('first processing was not released')
            return item

        def consume():
            try:
                for result in bounded_pipeline([0, 1], lambda x: x, process,
                                               prepare_workers=1, process_workers=2):
                    results.append(result)
                    if result == 1:
                        second_received.set()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            self.assertTrue(first_started.wait(2))
            self.assertTrue(second_received.wait(2))
            self.assertEqual(results, [1])
        finally:
            release_first.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [1, 0])

    def test_large_input_respects_both_pools_and_pending_budget(self):
        lock = threading.Lock()
        counts = {'produced': 0, 'processing': 0, 'preparing': 0,
                  'process_started': 0, 'peak_preparing': 0, 'peak_processing': 0,
                  'peak_unprocessed': 0}

        def items():
            for item in range(1200):
                with lock:
                    counts['produced'] += 1
                    counts['peak_unprocessed'] = max(
                        counts['peak_unprocessed'],
                        counts['produced'] - counts['process_started'])
                yield item

        def prepare(item):
            with lock:
                counts['preparing'] += 1
                counts['peak_preparing'] = max(counts['peak_preparing'], counts['preparing'])
            time.sleep(.0002)
            with lock:
                counts['preparing'] -= 1
            return item

        def process(item):
            with lock:
                counts['processing'] += 1
                counts['process_started'] += 1
                counts['peak_processing'] = max(counts['peak_processing'], counts['processing'])
            time.sleep(.0002)
            with lock:
                counts['processing'] -= 1
            return item

        results = list(bounded_pipeline(items(), prepare, process,
                                        prepare_workers=3, process_workers=2, max_pending=5))
        self.assertEqual(sorted(results), list(range(1200)))
        self.assertLessEqual(counts['peak_preparing'], 3)
        self.assertLessEqual(counts['peak_processing'], 2)
        self.assertLessEqual(counts['peak_unprocessed'], 7)

    def test_single_pending_slot_with_multiple_workers_does_not_deadlock(self):
        self.assertEqual(sorted(bounded_pipeline(range(30), lambda x: x + 1,
                                                 lambda x: x * 2, prepare_workers=4,
                                                 process_workers=4, max_pending=1)),
                         list(range(2, 62, 2)))

    def test_empty_and_invalid_limits(self):
        self.assertEqual(list(bounded_pipeline([], lambda x: x, lambda x: x)), [])
        for name in ('prepare_workers', 'process_workers', 'max_pending'):
            for invalid in (0, -1, True, 1.5, '2'):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        list(bounded_pipeline([], lambda x: x, lambda x: x,
                                              **{name: invalid}))

    def test_prepare_and_process_exceptions_propagate(self):
        for failing_stage in ('prepare', 'process'):
            with self.subTest(stage=failing_stage):
                def prepare(item):
                    if failing_stage == 'prepare' and item == 1:
                        raise RuntimeError('prepare failed')
                    return item

                def process(item):
                    if failing_stage == 'process' and item == 1:
                        raise RuntimeError('process failed')
                    return item

                with self.assertRaisesRegex(RuntimeError, f'{failing_stage} failed'):
                    list(bounded_pipeline(range(20), prepare, process,
                                          prepare_workers=2, process_workers=2))

    def test_exception_cleanup_waits_for_running_preparation(self):
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        processing_failed = threading.Event()
        finished = threading.Event()
        errors = []

        def prepare(item):
            if item == 1:
                preparation_started.set()
                if not release_preparation.wait(5):
                    raise TimeoutError('preparation was not released')
            return item

        def process(item):
            processing_failed.set()
            raise RuntimeError('process failed')

        def consume():
            try:
                list(bounded_pipeline([0, 1], prepare, process,
                                      prepare_workers=2, process_workers=1))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            self.assertTrue(preparation_started.wait(2))
            self.assertTrue(processing_failed.wait(2))
            self.assertFalse(finished.wait(.05))
        finally:
            release_preparation.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)


if __name__ == '__main__':
    unittest.main()
