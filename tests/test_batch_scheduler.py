import threading
import time
import unittest
import json
from pathlib import Path
from batch_pipeline import bounded_map, bind_model_routes


class BatchSchedulerTests(unittest.TestCase):
    def test_shipped_example_empty_hook_is_not_a_custom_client(self):
        source = Path(__file__).resolve().parents[1] / 'configs/pipeline.example.json'
        config = json.loads(source.read_text(encoding='utf-8'))
        updated = bind_model_routes(config, 'http://127.0.0.1:1234/_jobs/test')
        self.assertIsNone(updated['hooks']['mounting'])
        self.assertTrue(updated['mounting']['boundaries']['api_url'].startswith('http://127.0.0.1:1234/'))

    def test_large_input_is_not_eagerly_submitted(self):
        lock = threading.Lock()
        counts = {'produced': 0, 'finished': 0, 'peak_pending': 0}

        def inputs():
            for value in range(1200):
                with lock:
                    counts['produced'] += 1
                    counts['peak_pending'] = max(counts['peak_pending'], counts['produced'] - counts['finished'])
                yield value

        def work(value):
            time.sleep(.0001)
            with lock:
                counts['finished'] += 1
            return value

        result = list(bounded_map(inputs(), work, 8))
        self.assertEqual(sorted(result), list(range(1200)))
        self.assertLessEqual(counts['peak_pending'], 8)

    def test_limit_and_empty(self):
        self.assertEqual(list(bounded_map([], lambda x: x, 2)), [])
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                list(bounded_map([], lambda x: x, limit))

    def test_all_model_routes_are_bound_and_extra_services_rejected(self):
        config = {'track': 'dictionary', 'model': {'api_url': 'http://external', 'name': 'test'},
                  'screening_environment': {'API_URL': 'http://old'},
                  'mounting': {'api_url': 'http://old'}, 'hooks': {}}
        updated = bind_model_routes(config, 'http://127.0.0.1:1234/_jobs/job_1')
        self.assertEqual(updated['model']['api_url'], 'http://127.0.0.1:1234/_jobs/job_1')
        self.assertEqual(updated['screening_environment']['API_URL'], '{chat_url}')
        self.assertEqual(updated['mounting']['api_url'], 'http://127.0.0.1:1234/_jobs/job_1/v1/chat/completions')
        self.assertEqual(updated['mounting']['boundaries']['api_url'], updated['mounting']['api_url'])
        self.assertEqual(config['model']['api_url'], 'http://external')
        for extra in ({'hooks': {'mounting': [['custom']]}},
                      {'track': 'important', 'important_extraction': 'llm'}):
            with self.assertRaises(ValueError):
                bind_model_routes({**config, **extra}, 'http://127.0.0.1:1234/_jobs/test')


if __name__ == '__main__':
    unittest.main()
