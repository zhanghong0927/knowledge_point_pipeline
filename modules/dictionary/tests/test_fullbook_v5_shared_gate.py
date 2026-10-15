import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = Path(__file__).resolve().parents[1] / 'src/run_fullbook_v5.py'
spec = importlib.util.spec_from_file_location('shared_fullbook_gate', PATH)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class SharedGateTests(unittest.TestCase):
    def test_real_job_gate_opens_while_other_jobs_remain_active(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'limit': 1024, 'jobs': {
                    'book': {'total': {'active': 0, 'queued': 0}},
                    'boundary': {'total': {'active': 200, 'queued': 20}}}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                args = driver.parse_args(['--manifest', str(Path(directory)/'books.json'),
                    '--out', str(Path(directory)/'out'), '--model', 'mock',
                    '--api-url', f'http://127.0.0.1:{server.server_port}/_jobs/book',
                    '--idle-scope', 'scheduler-job', '--metrics-poll-interval', '.01'])
                args.out.mkdir()
                orchestrator = driver.Orchestrator(args)
                orchestrator.wait_idle('before_round_1', 1)
                gate = orchestrator.history['gates'][0]
                self.assertEqual(gate['outcome'], 'idle')
                self.assertEqual(gate['idle_scope'], 'scheduler-job')
                self.assertEqual(len(gate['samples']), 2)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_other_jobs_do_not_block_own_job_idle(self):
        snapshot = {'limit': 1024, 'jobs': {'book': {'total': {'active': 0, 'queued': 0}},
                                          'boundary': {'total': {'active': 200, 'queued': 30}}}}
        self.assertEqual(driver.parse_scheduler_job(json.dumps(snapshot), 'book'), {'running': 0, 'waiting': 0})

    def test_own_active_or_queued_is_not_idle(self):
        snapshot = {'limit': 1024, 'jobs': {'book': {'total': {'active': 3, 'queued': 2}}}}
        self.assertEqual(driver.parse_scheduler_job(json.dumps(snapshot), 'book'), {'running': 3, 'waiting': 2})

    def test_unregistered_first_round_is_idle_but_malformed_stats_are_blocked(self):
        self.assertEqual(driver.parse_scheduler_job('{"limit":1024,"jobs":{}}', 'book'), {'running': 0, 'waiting': 0})
        for bad in ('{}', '{"limit":1024,"jobs":{"book":{"total":{"active":-1,"queued":0}}}}'):
            with self.assertRaises(driver.Blocked):
                driver.parse_scheduler_job(bad, 'book')

    def test_scope_requires_known_loopback_gateway_job_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = ['--manifest', str(Path(directory)/'books.json'), '--out', str(Path(directory)/'out'),
                    '--model', 'mock', '--api-url', 'http://127.0.0.1:9000/_jobs/book',
                    '--idle-scope', 'scheduler-job']
            args = driver.parse_args(base)
            self.assertEqual(args.scheduler_job_id, 'book')
            self.assertEqual(args.scheduler_stats_url, 'http://127.0.0.1:9000/_scheduler/stats')
            base[base.index('--api-url')+1] = 'http://external.example'
            with self.assertRaises(SystemExit):
                driver.parse_args(base)


if __name__ == '__main__':
    unittest.main()
