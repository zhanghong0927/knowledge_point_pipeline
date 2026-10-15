"""Offline regression tests for bounded extraction transport rounds."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'modules/dictionary/src'))
import fullbook_llm_v5 as v5
import run_fullbook_v5 as driver
from validate_offline import offline_network_guard


def response(data=None):
    if data is None:
        data = {'scanned_all': True, 'entries': []}
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}


class ExtractionTransportPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        md = self.root / 'book.md'
        md.write_text('Alpha means a thing.\n', encoding='utf-8')
        self.spec = {'identifier': 'book', 'title': 'Book', 'md_path': str(md)}
        self.requests = []
        self.actions = []
        self.action = 200
        guard = offline_network_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def send_json(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path == '/v1/models':
                    self.send_json(200, {'data': [{'id': 'test', 'max_model_len': 100000}]})
                elif self.path == '/metrics':
                    body = b'vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n'
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_json(404, {'error': 'unknown route'})

            def do_POST(self):
                payload = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/tokenize':
                    self.send_json(200, {'count': 100})
                    return
                if self.path != '/v1/chat/completions':
                    self.send_json(404, {'error': 'unknown route'})
                    return
                owner.requests.append(payload)
                action = owner.actions.pop(0) if owner.actions else owner.action
                if action == 'timeout':
                    time.sleep(.2)
                    self.send_json(200, response())
                elif action == 'disconnect':
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                elif action == 'truncated':
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    self.wfile.write(b'{"choices":')
                elif isinstance(action, dict):
                    self.send_json(200, response(action))
                elif action == 200:
                    self.send_json(200, response())
                else:
                    self.send_json(action, {'error': 'fixture transport response'})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.api_url = f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def run_round(self, number, *, out=None, api=None, timeout=2, model='test'):
        args = argparse.Namespace(api_url=self.api_url, model=model, tokenizer=None,
            context=100000, output_tokens=16000, overlap=4, workers=2, timeout=timeout,
            server_context=None, out=out or self.root / 'out', attempts=3,
            transport_round=number)
        runner = v5.Runner(args)
        runner.backoff = lambda *args: None
        if api is not None:
            runner.api = api
        try:
            return runner.run_book(self.spec)
        finally:
            runner.close()

    def test_retryable_http_errors_defer_same_payload_and_success_cache_is_reused(self):
        # A transient HTTP failure must not exhaust local discovery attempts or
        # become an immutable failed leaf before lower concurrency can run.
        for code in (408, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                out = self.root / str(code)
                before = len(self.requests)
                self.action = code
                first = self.run_round(1, out=out)
                self.assertEqual(first['status'], 'partial')
                self.assertEqual(first['execution']['requests'], 1)
                self.assertEqual(first.get('transport_pending_chunks'), 1)
                self.assertEqual(first['http504_pending_chunks'], 1)  # driver compatibility
                self.assertEqual(first['http504_unresolved_chunks'], int(code == 504))
                self.run_round(1, out=out)
                self.assertEqual(len(self.requests) - before, 1)
                self.action = 200
                second = self.run_round(2, out=out)
                self.assertEqual(second['status'], 'completed')
                self.assertEqual(second.get('transport_pending_chunks'), 0)
                self.assertEqual(second['technical_failure_unresolved_chunks'], 0)
                self.assertEqual(len(self.requests) - before, 2)
                self.assertEqual(self.requests[-2], self.requests[-1])
                error_path = next(out.glob('*/errors/*.json'))
                record = json.loads(error_path.read_text())
                self.assertEqual(record['payload_sha256'], hashlib.sha256(self.requests[-1]).hexdigest())
                self.run_round(3, out=out)
                self.assertEqual(len(self.requests) - before, 2)

    def test_mixed_transport_failures_share_three_attempt_ceiling(self):
        self.actions = [502, 503, 429]
        for number in (1, 2, 3):
            result = self.run_round(number)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(self.requests), number)
            self.assertEqual(result.get('transport_pending_chunks'), int(number < 3))
            self.assertTrue(result['chunks'][0]['technical_failure_unresolved'])
        record = json.loads(next((self.root / 'out').glob('*/errors/*.json')).read_text())
        self.assertEqual(record['transport_attempts'], 3)
        self.assertEqual(record['rounds'], [1, 2, 3])
        self.assertEqual(len(set(self.requests)), 1)
        self.run_round(3)
        self.assertEqual(len(self.requests), 3)

    def test_real_client_timeout_and_disconnect_are_deferred_to_later_round(self):
        for action in ('timeout', 'disconnect', 'truncated'):
            with self.subTest(action=action):
                out = self.root / action
                before = len(self.requests)
                self.action = action
                result = self.run_round(1, out=out, timeout=.04)
                self.assertEqual(result['execution']['requests'], 1)
                self.assertEqual(result.get('transport_pending_chunks'), 1)
                self.assertEqual(result['http504_unresolved_chunks'], 0)
                self.action = 200
                self.assertEqual(self.run_round(2, out=out)['status'], 'completed')
                self.assertEqual(len(self.requests) - before, 2)
                self.assertEqual(self.requests[-2], self.requests[-1])

    def test_url_wrapped_timeout_uses_same_bounded_transport_policy(self):
        sent = []
        def unavailable(route, payload):
            sent.append(payload)
            raise URLError(TimeoutError('fixture client timeout'))
        first = self.run_round(1, api=unavailable)
        self.assertEqual(first['execution']['requests'], 1)
        self.assertEqual(first.get('transport_pending_chunks'), 1)
        self.run_round(1, api=unavailable)
        self.assertEqual(len(sent), 1)
        second = self.run_round(2)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(json.loads(self.requests[0]), sent[0])

    def test_permanent_http_and_content_errors_do_not_enter_transport_rounds(self):
        for action in (400, 401, 403, 404, 422, {'unexpected': 'content'}):
            with self.subTest(action=action):
                out = self.root / (f'http-{action}' if isinstance(action, int) else 'bad-content')
                before = len(self.requests)
                self.action = action
                first = self.run_round(1, out=out)
                self.assertEqual(first['status'], 'partial')
                self.assertEqual(first.get('transport_pending_chunks'), 0)
                self.assertEqual(first.get('transport_unresolved_chunks'), 0)
                count = len(self.requests)
                self.assertEqual(count - before, 1 if isinstance(action, int) else 3)
                self.run_round(2, out=out)
                self.assertEqual(len(self.requests), count)

    def test_transport_error_in_repair_preserves_valid_neighbor_and_discovery_cache(self):
        good = {'head': [{'unit': 0, 'quote': 'Alpha'}],
                'knowledge_point': [{'unit': 0, 'quote': 'Alpha'}], 'name': [],
                'body': [{'start': {'unit': 0, 'quote': 'means'},
                          'end': {'unit': 0, 'quote': 'thing.'}}], 'body_complete': True}
        bad = {**good, 'head': [{'unit': 0, 'quote': 'NOT IN SOURCE'}]}
        self.actions = [{'scanned_all': True, 'entries': [good, bad]}, 503]
        first = self.run_round(1)
        self.assertEqual(first['entries'], 1)
        self.assertEqual(first.get('transport_pending_chunks'), 1)
        discovery, repair = self.requests
        task = json.loads(repair)['messages'][1]['content']
        rid = json.loads(task)['repair_tasks'][0]['repair_id']
        self.actions = [{'repairs': [{'repair_id': rid, 'entry': None, 'reason': 'No source head'}]}]
        second = self.run_round(2)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['entries'], 1)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(repair, self.requests[-1])
        self.assertNotEqual(discovery, self.requests[-1])

    def test_checkpoint_payload_change_is_refused_before_new_request(self):
        self.action = 503
        self.run_round(1)
        error_path = next((self.root / 'out').glob('*/errors/*.json'))
        record = json.loads(error_path.read_text())
        record['payload_sha256'] = '0' * 64
        error_path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Retry payload changed'):
            self.run_round(2)
        self.assertEqual(len(self.requests), 1)

    def test_real_orchestrator_runs_lower_concurrency_round_after_non504_failure(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps([self.spec]), encoding='utf-8')
        # Each model child installs the same literal-loopback-only guard.
        guarded_runner = self.root / 'guarded_runner.py'
        guarded_runner.write_text(
            'import runpy, sys\n'
            f'sys.path.insert(0, {str(ROOT)!r})\n'
            f'sys.path.insert(0, {str(ROOT / "modules/dictionary/src")!r})\n'
            'from validate_offline import offline_network_guard\n'
            'with offline_network_guard():\n'
            f'    runpy.run_path({str(ROOT / "modules/dictionary/src/fullbook_llm_v5.py")!r}, run_name="__main__")\n',
            encoding='utf-8')
        args = driver.parse_args([
            '--manifest', str(manifest), '--out', str(self.root / 'driver-out'),
            '--api-url', self.api_url, '--model', 'test', '--runner', str(guarded_runner),
            '--python', sys.executable, '--round-workers', '4', '2', '1', '--book-workers', '1',
            '--metrics-url', self.api_url + '/metrics', '--metrics-poll-interval', '.01',
            '--metrics-max-wait', '3', '--timeout', '2'])
        self.actions = [503, 429, 200]
        result = driver.Orchestrator(args).run()
        state = json.loads((args.out / 'ORCHESTRATION_STATUS.json').read_text())
        self.assertEqual(result, 0, state)
        history = json.loads((args.out / 'ROUNDS.json').read_text())
        self.assertEqual([r['round'] for r in history['rounds']], [1, 2, 3])
        self.assertEqual([r['workers'] for r in history['rounds']], [4, 2, 1])
        self.assertEqual([r['summary']['http504_pending_chunks'] for r in history['rounds']], [1, 1, 0])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(set(self.requests)), 1)

    def test_driver_validates_generic_counter_totals_and_keeps_http504_literal(self):
        book = {'identifier': 'book', 'status': 'partial', 'http504_pending_chunks': 1,
                'http504_unresolved_chunks': 0, 'transport_pending_chunks': 1,
                'transport_unresolved_chunks': 1}
        snapshot = {'expected_books': 1, 'finished_books': 1, 'books': [book],
                    'http504_pending_chunks': 1, 'http504_unresolved_chunks': 0,
                    'transport_pending_chunks': 1, 'transport_unresolved_chunks': 1}
        try:
            outcome = driver.validate_summary(snapshot, ['book'], 2, 1)
        except driver.RunnerFailure as exc:
            self.fail(f'Non504 transport counters were rejected: {exc}')
        self.assertEqual(outcome, 'partial')
        for field in ('transport_pending_chunks', 'transport_unresolved_chunks'):
            with self.subTest(field=field):
                changed = {**snapshot, field: 0}
                with self.assertRaises(driver.RunnerFailure):
                    driver.validate_summary(changed, ['book'], 2, 1)
        changed = {**snapshot, 'books': [{**book, 'status': 'completed'}]}
        with self.assertRaises(driver.RunnerFailure):
            driver.validate_summary(changed, ['book'], 0, 1)

    def test_staged_non504_exhaustion_is_technical_failure_before_finalization(self):
        # Use the real native runner and checkpoints; only metrics/model HTTP
        # responses come from the loopback fixture.
        manifest = self.root / 'staged-manifest.json'
        manifest.write_text(json.dumps([self.spec]), encoding='utf-8')
        guarded_runner = self.root / 'staged_guarded_runner.py'
        guarded_runner.write_text(
            'import runpy, sys\n'
            f'sys.path.insert(0, {str(ROOT)!r})\n'
            f'sys.path.insert(0, {str(ROOT / "modules/dictionary/src")!r})\n'
            'from validate_offline import offline_network_guard\n'
            'with offline_network_guard():\n'
            f'    runpy.run_path({str(ROOT / "modules/dictionary/src/fullbook_llm_v5.py")!r}, run_name="__main__")\n',
            encoding='utf-8')
        self.action = 503
        for number, expected_state in ((1, 'awaiting_retry'), (2, 'awaiting_retry'),
                                       (3, 'technical_failure')):
            args = driver.parse_args([
                '--manifest', str(manifest), '--out', str(self.root / 'staged-out'),
                '--api-url', self.api_url, '--model', 'test', '--runner', str(guarded_runner),
                '--python', sys.executable, '--round-workers', '4', '2', '1', '--book-workers', '1',
                '--metrics-url', self.api_url + '/metrics', '--metrics-poll-interval', '.01',
                '--metrics-max-wait', '3', '--timeout', '2', '--only-round', str(number)])
            result = driver.Orchestrator(args).run()
            state = json.loads((args.out / 'ORCHESTRATION_STATUS.json').read_text())
            self.assertEqual(state['state'], expected_state, state)
            self.assertEqual(result, int(number == 3))
            self.assertEqual(len(self.requests), number)
        self.assertEqual(len(set(self.requests)), 1)


if __name__ == '__main__':
    unittest.main()
