"""Offline orchestration tests: no model requests and no real runner launches."""
import argparse
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

try:
    import run_fullbook_v5 as driver
except ModuleNotFoundError as exc:
    if exc.name != 'run_fullbook_v5':
        raise
    driver = None


def metrics(running=0, waiting=0):
    return (f'vllm:num_requests_running{{model_name="test"}} {running}\n'
            f'vllm:num_requests_waiting{{model_name="test"}} {waiting}\n')


def summary(pending=0, unresolved=0, status=None, identifier='book-1'):
    return {
        'expected_books': 1, 'finished_books': 1,
        'http504_pending_chunks': pending,
        'http504_unresolved_chunks': unresolved,
        'books': [{'identifier': identifier,
                   'status': status or ('partial' if unresolved else 'completed'),
                   'http504_pending_chunks': pending,
                   'http504_unresolved_chunks': unresolved}],
    }


class Clock:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(driver, 'The three-round orchestration driver is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manifest = self.base / 'manifest.json'
        self.manifest.write_text(json.dumps([{'identifier': 'book-1'}]), encoding='utf-8')
        self.out = self.base / 'out'
        self.out.mkdir()
        self.args = driver.parse_args([
            '--manifest', str(self.manifest), '--out', str(self.out),
            '--api-url', 'http://unused.test/v1/chat/completions', '--model', 'test',
            '--metrics-poll-interval', '1', '--metrics-max-wait', '5',
        ])
        self.clock = Clock()
        self.events = []

    def execute(self, results, samples=None):
        results = iter(results)
        samples = iter(samples) if samples is not None else None

        def fetch(request, timeout):
            self.assertLessEqual(timeout, 5)
            self.events.append(('metrics', request.full_url))
            value = next(samples) if samples is not None else metrics()
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value.encode('utf-8'))

        def launch(command, **kwargs):
            round_number = int(command[command.index('--transport-round') + 1])
            self.events.append(('round', round_number, command))
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            code, snapshot = result
            self.clock.now += 2
            if snapshot is not None:
                (self.out / 'SUMMARY.json').write_text(
                    json.dumps(snapshot), encoding='utf-8')
            kwargs['stdout'].write('offline runner log\n')
            return subprocess.CompletedProcess(command, code)

        with patch.object(driver.urllib.request, 'urlopen', side_effect=fetch), \
                patch.object(driver.subprocess, 'run', side_effect=launch), \
                patch.object(driver.time, 'monotonic', self.clock.monotonic), \
                patch.object(driver.time, 'sleep', self.clock.sleep):
            code = driver.Orchestrator(self.args).run()
        self.state = json.loads((self.out / 'ORCHESTRATION_STATUS.json').read_text())
        self.history = json.loads((self.out / 'ROUNDS.json').read_text())
        return code

    def launches(self):
        return [event for event in self.events if event[0] == 'round']

    def test_three_rounds_share_identity_and_wait_before_every_launch(self):
        code = self.execute([(2, summary(3, 3)), (2, summary(1, 1)), (0, summary())])
        self.assertEqual(code, 0)
        self.assertEqual([e[0] for e in self.events],
                         ['metrics', 'metrics', 'round', 'metrics', 'metrics', 'round',
                          'metrics', 'metrics', 'round', 'metrics', 'metrics'])
        for event, workers in zip(self.launches(), ['1024', '256', '64']):
            command = event[2]
            self.assertEqual(command[command.index('--workers') + 1], workers)
            self.assertEqual(command[command.index('--manifest') + 1], str(self.manifest))
            self.assertEqual(command[command.index('--out') + 1], str(self.out))
            for flag, value in [('--book-workers', '32'), ('--context', '100000'),
                                ('--output-tokens', '16000'), ('--timeout', '900')]:
                self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(self.state['state'], 'completed')
        self.assertEqual([r['outcome'] for r in self.history['rounds']],
                         ['partial', 'partial', 'completed'])
        self.assertEqual([r['exit_code'] for r in self.history['rounds']], [2, 2, 0])
        self.assertEqual(self.history['rounds'][0]['summary']['http504_pending_chunks'], 3)
        for stage in self.history['rounds'] + self.history['gates']:
            self.assertTrue(stage['started_at'])
            self.assertTrue(stage['ended_at'])
            self.assertGreater(stage['wall_seconds'], 0)

    def test_cli_overrides_are_inherited_by_each_round(self):
        self.args.book_workers = 9
        self.args.context = 85000
        self.args.output_tokens = 12000
        self.args.timeout = 321
        self.args.server_context = 100000
        self.args.tokenizer = '/offline/tokenizer'
        self.args.overlap = 8
        self.args.metrics_url = ['http://metrics.test/metrics']
        self.assertEqual(self.execute([(2, summary(1, 1)), (0, summary())]), 0)
        for event in self.launches():
            command = event[2]
            for flag, value in [('--book-workers', '9'), ('--context', '85000'),
                                ('--output-tokens', '12000'), ('--timeout', '321'),
                                ('--server-context', '100000'),
                                ('--tokenizer', '/offline/tokenizer'), ('--overlap', '8')]:
                self.assertEqual(command[command.index(flag) + 1], value)
        self.assertTrue(all(e[1] == 'http://metrics.test/metrics'
                            for e in self.events if e[0] == 'metrics'))

    def test_completed_first_round_skips_remaining_rounds(self):
        self.assertEqual(self.execute([(0, summary())]), 0)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.state['stop_reason'], 'no_http504_pending')

    def test_non504_partial_is_not_success_crash_or_retry(self):
        snapshot = summary(status='partial')
        snapshot['books'][0]['error'] = 'HTTP 503 / validation incomplete'
        self.assertEqual(self.execute([(2, snapshot)]), 2)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.state['state'], 'partial')
        self.assertEqual(self.history['rounds'][0]['outcome'], 'partial')

    def test_exhausted_504_stays_partial_after_round_three(self):
        self.assertEqual(self.execute([(2, summary(1, 1)), (2, summary(1, 1)),
                                       (2, summary(0, 1))]), 2)
        self.assertEqual(len(self.launches()), 3)
        self.assertEqual(self.state['state'], 'partial')
        self.assertEqual(self.state['stop_reason'], 'http504_exhausted')

    def test_initial_metrics_unavailable_blocks_without_launch(self):
        self.assertEqual(self.execute([], [OSError('metrics unavailable')]), 3)
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.state['state'], 'blocked')
        self.assertIn('metrics unavailable', self.state['error'])

    def test_truncated_http_metrics_response_is_explicitly_blocked(self):
        self.assertEqual(self.execute([], [http.client.IncompleteRead(b'partial', 8)]), 3)
        self.assertEqual(self.state['state'], 'blocked')
        self.assertEqual(self.launches(), [])

    def test_slow_metrics_response_cannot_pass_after_deadline(self):
        with patch.object(driver.urllib.request, 'urlopen', side_effect=lambda *a, **kw: (
                setattr(self.clock, 'now', self.clock.now + 6) or io.BytesIO(metrics().encode()))), \
                patch.object(driver.time, 'monotonic', self.clock.monotonic), \
                patch.object(driver.time, 'sleep', self.clock.sleep), \
                patch.object(driver.subprocess, 'run', side_effect=AssertionError('must not launch')):
            self.assertEqual(driver.Orchestrator(self.args).run(), 3)
        state = json.loads((self.out / 'ORCHESTRATION_STATUS.json').read_text())
        self.assertEqual(state['state'], 'blocked')

    def test_duplicate_driver_cannot_overwrite_owner_status(self):
        marker = '{"state":"running","pid":"owner"}'
        (self.out / 'ORCHESTRATION_STATUS.json').write_text(marker, encoding='utf-8')
        with driver.output_lock(self.out), patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(driver.Orchestrator(self.args).run(), 3)
        self.assertEqual((self.out / 'ORCHESTRATION_STATUS.json').read_text(), marker)

    def test_metrics_unavailable_after_partial_prevents_next_round(self):
        self.assertEqual(self.execute([(2, summary(1, 1))],
                                     [metrics(), metrics(), OSError('offline')]), 3)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.state['state'], 'blocked')
        self.assertEqual(self.history['rounds'][0]['exit_code'], 2)
        self.assertEqual(self.history['gates'][-1]['outcome'], 'blocked')

    def test_busy_metrics_timeout_is_bounded_and_does_not_launch(self):
        self.assertEqual(self.execute([], [metrics(waiting=1)] * 20), 3)
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.clock.now, 5)
        self.assertIn('timed out', self.state['error'])

    def test_busy_after_partial_does_not_mix_rounds(self):
        self.assertEqual(self.execute([(2, summary(1, 1))],
                                     [metrics(), metrics()] + [metrics(running=1)] * 20), 3)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.history['gates'][-1]['wall_seconds'], 5)

    def test_zero_busy_zero_requires_a_new_consecutive_pair(self):
        self.assertEqual(self.execute([(0, summary())],
                                     [metrics(), metrics(1), metrics(), metrics(),
                                      metrics(), metrics()]), 0)
        self.assertEqual([e[0] for e in self.events[:5]],
                         ['metrics', 'metrics', 'metrics', 'metrics', 'round'])

    def test_all_metrics_endpoints_must_be_idle(self):
        self.args.metrics_url = ['http://one.test/metrics', 'http://two.test/metrics']
        samples = [metrics(), metrics(1), metrics(), metrics(), metrics(), metrics(),
                   metrics(), metrics(), metrics(), metrics()]
        self.assertEqual(self.execute([(0, summary())], samples), 0)
        self.assertEqual([e[0] for e in self.events[:7]], ['metrics'] * 6 + ['round'])

    def test_final_gate_failure_is_not_reported_as_success(self):
        self.assertEqual(self.execute([(0, summary())],
                                     [metrics(), metrics(), OSError('offline')]), 3)
        self.assertEqual(self.state['state'], 'blocked')
        self.assertEqual(self.history['rounds'][0]['outcome'], 'completed')

    def test_missing_stale_or_incomplete_summary_aborts(self):
        cases = [None, {**summary(1, 1), 'finished_books': 0},
                 {**summary(1, 1), 'expected_books': 2}]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                self.events.clear()
                (self.out / 'SUMMARY.json').unlink(missing_ok=True)
                self.assertEqual(self.execute([(2, snapshot)]), 1)
                self.assertEqual(len(self.launches()), 1)
                self.assertEqual(self.state['state'], 'aborted')

    def test_unchanged_old_summary_cannot_mask_runner_crash(self):
        (self.out / 'SUMMARY.json').write_text(json.dumps(summary(1, 1)), encoding='utf-8')
        self.assertEqual(self.execute([(2, None)]), 1)
        self.assertIn('fresh', self.state['error'])
        self.assertEqual(len(self.launches()), 1)

    def test_nonfinite_json_summary_aborts_instead_of_breaking_status_writer(self):
        snapshot = summary()
        snapshot['elapsed_seconds'] = float('nan')
        self.assertEqual(self.execute([(0, snapshot)]), 1)
        self.assertEqual(self.state['state'], 'aborted')
        self.assertEqual(self.history['rounds'][0]['exit_code'], 0)

    def test_technical_failure_exit2_is_a_failure_not_normal_partial(self):
        self.assertEqual(self.execute([(2, summary(status='technical_failure'))]), 1)
        self.assertEqual(self.state['state'], 'aborted')

    def test_signal_or_unexpected_exit_code_aborts_even_with_complete_summary(self):
        for code in [-9, 1, 3]:
            with self.subTest(code=code):
                self.events.clear()
                self.assertEqual(self.execute([(code, summary())]), 1)
                self.assertEqual(self.history['rounds'][0]['exit_code'], code)
                self.assertEqual(len(self.launches()), 1)

    def test_exit0_with_partial_and_exit2_with_completed_are_inconsistent(self):
        for code, snapshot in [(0, summary(1, 1)), (2, summary())]:
            with self.subTest(code=code):
                self.events.clear()
                self.assertEqual(self.execute([(code, snapshot)]), 1)

    def test_missing_wrong_type_or_inconsistent_504_totals_abort(self):
        cases = []
        for value in [None, True, -1, 1.5, '1', 2]:
            cases.append({**summary(1, 1), 'http504_pending_chunks': value})
        missing = summary(1, 1)
        del missing['http504_unresolved_chunks']
        cases.append(missing)
        wrong_book = summary(1, 1)
        wrong_book['books'][0]['http504_pending_chunks'] = 0
        cases.append(wrong_book)
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                self.events.clear()
                self.assertEqual(self.execute([(2, snapshot)]), 1)
                self.assertEqual(len(self.launches()), 1)

    def test_wrong_or_duplicate_book_ids_abort(self):
        snapshots = [summary(1, 1, identifier='wrong')]
        duplicate = summary(1, 1)
        duplicate['books'] *= 2
        snapshots.append(duplicate)
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.events.clear()
                self.assertEqual(self.execute([(2, snapshot)]), 1)

    def test_round3_must_not_claim_future_504_retries(self):
        self.assertEqual(self.execute([(2, summary(1, 1))] * 3), 1)
        self.assertIn('round 3', self.state['error'])

    def test_subprocess_launch_failure_records_failed_stage_and_aborts(self):
        self.assertEqual(self.execute([OSError('cannot launch')]), 1)
        stage = self.history['rounds'][0]
        self.assertIsNone(stage['exit_code'])
        self.assertIsNotNone(stage['ended_at'])
        self.assertEqual(stage['outcome'], 'aborted')
        self.assertIn('cannot launch', self.state['error'])

    def test_keyboard_interrupt_closes_active_stage_without_more_launches(self):
        self.assertEqual(self.execute([KeyboardInterrupt()]), 1)
        self.assertEqual(self.history['rounds'][0]['outcome'], 'aborted')
        self.assertIsNotNone(self.history['rounds'][0]['ended_at'])
        self.assertEqual(len(self.launches()), 1)

    def test_manifest_drift_between_rounds_aborts(self):
        original = self.clock.sleep

        def change_manifest(seconds):
            original(seconds)
            if self.launches():
                self.manifest.write_text(json.dumps([{'identifier': 'changed'}]), encoding='utf-8')

        self.clock.sleep = change_manifest
        self.assertEqual(self.execute([(2, summary(1, 1))]), 1)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn('manifest', self.state['error'])

    def test_standalone_cli_chains_real_offline_subprocesses(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = metrics().encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [sys.executable, '-B', str(Path(driver.__file__).resolve()),
                       '--runner', str(Path(__file__).resolve()),
                       '--manifest', str(self.manifest), '--out', str(self.out),
                       '--api-url', 'http://unused.test', '--model', 'offline',
                       '--metrics-url', f'http://127.0.0.1:{server.server_port}/metrics',
                       '--metrics-poll-interval', '0.01', '--metrics-max-wait', '10']
            environment = dict(os.environ, FULLBOOK_DRIVER_OFFLINE_STUB='1',
                               PYTHONDONTWRITEBYTECODE='1')
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=environment) as process:
                try:
                    stdout, stderr = process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
                self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)['state'], 'completed')
            history = json.loads((self.out / 'ROUNDS.json').read_text())
            state = json.loads((self.out / 'ORCHESTRATION_STATUS.json').read_text())
            self.assertNotEqual(state['pid'], os.getpid())
            self.assertEqual([r['round'] for r in history['rounds']], [1, 2, 3])
            self.assertEqual([r['exit_code'] for r in history['rounds']], [2, 2, 0])
            self.assertEqual([r['workers'] for r in history['rounds']], [1024, 256, 64])
            self.assertTrue(all(r['summary']['model_calls'] == 0 for r in history['rounds']))
            self.assertTrue(all(g['outcome'] == 'idle' for g in history['gates']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(driver, 'The metrics gate is missing')

    def test_series_are_summed_including_multiple_models(self):
        text = ('# TYPE vllm:num_requests_running gauge\n'
                'vllm:num_requests_running{model_name="a"} 0\n'
                'vllm:num_requests_running{model_name="b"} 2.0 123\n'
                'vllm:num_requests_waiting{model_name="a"} 1e0\n'
                'vllm:num_requests_waiting{model_name="b"} 3\n')
        self.assertEqual(driver.parse_metrics(text), {'running': 2., 'waiting': 4.})

    def test_missing_invalid_negative_nonfinite_metrics_are_not_idle(self):
        for text in ['', 'vllm:num_requests_running 0\n', metrics('NaN'),
                     metrics('+Inf'), metrics(-1), metrics('invalid')]:
            with self.subTest(text=text), self.assertRaises(driver.Blocked):
                driver.parse_metrics(text)

    def test_invalid_wait_options_fail_cli_validation(self):
        base = ['--manifest', 'manifest.json', '--out', 'out',
                '--api-url', 'http://unused.test', '--model', 'test']
        for flag, value in [('--metrics-max-wait', '0'),
                            ('--metrics-poll-interval', '0'),
                            ('--metrics-timeout', '0'),
                            ('--metrics-max-wait', 'nan')]:
            with self.subTest(flag=flag, value=value), patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    driver.parse_args(base + [flag, value])
                self.assertEqual(exc.exception.code, 2)


def offline_stub():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--transport-round', type=int, required=True)
    args, _ = parser.parse_known_args()
    pending = int(args.transport_round < 3)
    snapshot = {**summary(pending, pending), 'model_calls': 0}
    (args.out / 'SUMMARY.json').write_text(json.dumps(snapshot), encoding='utf-8')
    return 2 if pending else 0


if __name__ == '__main__':
    if os.environ.get('FULLBOOK_DRIVER_OFFLINE_STUB') == '1' and '--transport-round' in sys.argv:
        raise SystemExit(offline_stub())
    unittest.main()
