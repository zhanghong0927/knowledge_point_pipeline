import importlib.util
import json
import unittest


class ConcurrencyCalibrationTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec('concurrency_calibration')
        self.assertIsNotNone(spec, 'fixed-population calibration is required')
        import concurrency_calibration
        return concurrency_calibration

    def test_response_requires_complete_json_and_reports_truncation(self):
        module = self.module()
        reply = {'choices': [{'finish_reason': 'stop',
                              'message': {'content': json.dumps({'scanned_all': True, 'entries': []})}}]}
        self.assertTrue(module.validate_response(reply, 'extraction')['protocol_valid'])
        reply['choices'][0]['finish_reason'] = 'length'
        self.assertFalse(module.validate_response(reply, 'extraction')['protocol_valid'])
        reply['choices'][0]['finish_reason'] = 'stop'
        reply['choices'][0]['message']['content'] = '{}'
        self.assertFalse(module.validate_response(reply, 'extraction')['protocol_valid'])

    def test_fixed_population_does_not_change_with_test_concurrency(self):
        module = self.module()
        packets = [{'id': 'short', 'payload': {'messages': [{'content': 'a'}]}},
                   {'id': 'long', 'payload': {'messages': [{'content': 'x' * 40000}]}}]
        before = json.dumps(packets, sort_keys=True)
        population = module.fixed_population(packets, 128)
        self.assertEqual(len(population), 128)
        self.assertEqual(sum(packet['id'] == 'long' for packet in population), 64)
        self.assertEqual(json.dumps(packets, sort_keys=True), before)
        self.assertEqual(population, module.fixed_population(packets, 128))
        with self.assertRaises(ValueError):
            module.fixed_population([], 128)

    def test_high_failure_rate_does_not_win_on_raw_request_count(self):
        module = self.module()
        rows = [{'concurrency': 32, 'requests': 64, 'protocol_valid': 64,
                 'transport_errors': 0, 'wall_seconds': 100, 'p95_seconds': 90},
                {'concurrency': 64, 'requests': 64, 'protocol_valid': 40,
                 'transport_errors': 24, 'wall_seconds': 50, 'p95_seconds': 45}]
        self.assertEqual(module.choose_level(rows)['concurrency'], 32)

    def test_near_equal_throughput_chooses_lower_latency_and_lower_concurrency(self):
        module = self.module()
        rows = [{'concurrency': 64, 'requests': 128, 'protocol_valid': 128,
                 'transport_errors': 0, 'wall_seconds': 100, 'p95_seconds': 85},
                {'concurrency': 128, 'requests': 128, 'protocol_valid': 128,
                 'transport_errors': 0, 'wall_seconds': 96, 'p95_seconds': 140}]
        self.assertEqual(module.choose_level(rows)['concurrency'], 64)

    def test_no_healthy_level_does_not_silently_apply_candidate_caps(self):
        module = self.module()
        rows = [{'concurrency': 128, 'requests': 128, 'protocol_valid': 70,
                 'transport_errors': 58, 'wall_seconds': 60, 'p95_seconds': 50}]
        self.assertIsNone(module.choose_level(rows))

    def test_healthy_dictionary_subset_cannot_hide_important_front_failure(self):
        module = self.module()
        rows = [{'concurrency': 64, 'requests': 64, 'protocol_valid': 58,
                 'transport_errors': 1, 'wall_seconds': 100, 'p95_seconds': 90,
                 'groups': {'dictionary': {'requests': 32, 'protocol_valid': 32, 'transport_errors': 0},
                            'important': {'requests': 32, 'protocol_valid': 26, 'transport_errors': 1}}}]
        self.assertIsNone(module.choose_level(rows))

    def test_second_warm_pass_cannot_hide_a_failing_first_pass(self):
        module = self.module()
        rows = [{'concurrency': 64, 'requests': 64, 'protocol_valid': 64,
                 'transport_errors': 0, 'wall_seconds': 100, 'p95_seconds': 90, 'population_sha256': 'a'},
                {'concurrency': 64, 'requests': 64, 'protocol_valid': 62,
                 'transport_errors': 2, 'wall_seconds': 80, 'p95_seconds': 70, 'population_sha256': 'a'}]
        combined = module.combine_passes(rows)
        self.assertEqual(combined[0]['requests'], 128)
        self.assertIsNone(module.choose_level(combined))

    def test_real_loopback_replay_keeps_population_and_bounds_requests(self):
        import tempfile
        import threading
        import time
        from pathlib import Path
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        module = self.module()
        lock = threading.Lock()
        active = peak = 0
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal active, peak
                data = self.rfile.read(int(self.headers['Content-Length']))
                with lock:
                    received.append(data)
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(.03)
                    body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                        'content': json.dumps({'scanned_all': True, 'entries': []})}}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with lock:
                        active -= 1

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                packets = [{'id': 'p', 'payload': {'model': 'fake', 'messages': []}}]
                population = module.fixed_population(packets, 12)
                base = f'http://127.0.0.1:{server.server_port}'
                rows = [module.run_level(base, 'extraction', population, limit,
                                        Path(temp) / str(limit), 3) for limit in (2, 4)]
            self.assertEqual([row['requests'] for row in rows], [12, 12])
            self.assertEqual([row['protocol_valid'] for row in rows], [12, 12])
            self.assertEqual(rows[0]['population_sha256'], rows[1]['population_sha256'])
            self.assertLessEqual(peak, 4)
            self.assertEqual(len(received), 24)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
