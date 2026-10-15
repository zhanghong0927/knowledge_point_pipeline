"""Boundary cohorts use literal loopback responses and no real model service."""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from test_mounting_bridge import bridge, fixture


GENERATOR = bridge.ROOT / 'modules/mounting/pipeline/generate_semantic_boundaries.py'


def native_module():
    sys.path.insert(0, str(GENERATOR.parent))
    spec = importlib.util.spec_from_file_location('staged_boundary_native_test', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def boundary_fixture(root):
    args, _ = fixture(root)
    args.out = root/'boundary_mount'
    args.require_boundaries = True
    args.boundary_review_mode = 'off'
    args.boundary_failure_policy = 'empty'
    args.boundary_context_bytes = 50000
    bridge.prepare(args)
    return args, args.out/'groups/mechanical_engineering'


class BoundaryStagedTests(unittest.TestCase):
    def test_format_repair_is_separate_from_transport_retry(self):
        native = native_module()
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                calls.append(1)
                content = 'not json' if len(calls) == 1 else '{"ok": true}'
                response = json.dumps({'choices': [{'finish_reason': 'stop',
                                                     'message': {'content': content}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result, kind = native.staged_call(f'http://127.0.0.1:{server.server_port}',
                                              'literal-fake', 'prompt', {'targets': []}, lambda value: value)
            self.assertEqual(result['result'], {'ok': True})
            self.assertIsNone(kind)
            self.assertEqual(len(calls), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_boundary_checkpoint_or_source_mutation_stops_before_new_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            args, group = boundary_fixture(Path(directory))
            native = native_module()
            import boundary_unreviewed
            argv = ['native', '--tree', str(group/'boundary_input.json'), '--out', str(group/'boundaries'),
                    '--staged-state-dir', str(args.out/'boundary/groups/mechanical_engineering'),
                    '--base', 'http://127.0.0.1:9', '--model', 'literal-fake',
                    '--workers', '1', '--max-depth', '-1', '--review-mode', 'off', '--cross-review', 'off',
                    '--failure-policy', 'empty', '--max-context-bytes', '50000']

            def failed(base, model, payload, max_bytes):
                return {'status': 'technical_failure', 'reason': 'generation_technical_failure',
                        'technical_kind': 'transport', 'technical_error': 'HTTP 502',
                        'rounds': [{'generation': {'result': None, 'attempts': [{'error': 'HTTP 502'}]}}]}

            with patch.object(sys, 'argv', argv+['--only-round', '1']), \
                    patch.object(boundary_unreviewed, 'generate_group_staged', side_effect=failed):
                native.main()
            checkpoint = next((group/'boundaries/groups').glob('*.json'))
            original = checkpoint.read_text(encoding='utf-8')
            checkpoint.write_text(original.replace('HTTP 502', 'HTTP 503', 1), encoding='utf-8')
            with patch.object(sys, 'argv', argv+['--only-round', '2']), \
                    patch.object(boundary_unreviewed, 'generate_group_staged', side_effect=AssertionError('unexpected request')):
                with self.assertRaisesRegex(ValueError, 'checkpoint.*checksum'):
                    native.main()
            checkpoint.write_text(original, encoding='utf-8')
            source = group/'boundary_input.json'
            source.write_text(source.read_text(encoding='utf-8')+'\n', encoding='utf-8')
            with patch.object(sys, 'argv', argv+['--only-round', '2']), \
                    patch.object(boundary_unreviewed, 'generate_group_staged', side_effect=AssertionError('unexpected request')):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    native.main()

    def test_verified_cache_hit_finishes_first_round_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, _ = fixture(root)
            args.out = root/'first_mount'
            args.require_boundaries = True
            args.boundary_review_mode = 'off'
            args.boundary_failure_policy = 'empty'
            args.boundary_context_bytes = 50000
            args.boundary_cache_dir = root/'cache'
            bridge.prepare(args)
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    payload = json.loads(wire['messages'][1]['content'])
                    calls.append(1)
                    cards = [{'node_code': node['code'], 'definition': '示例定义',
                              'boundary': '示例边界', 'includes': ['示例'], 'excludes': [],
                              'sibling_distinctions': [], 'cross_boundary_rule': ''}
                             for node in payload['targets']]
                    response = json.dumps({'choices': [{'finish_reason': 'stop',
                                                         'message': {'content': json.dumps({'cards': cards}, ensure_ascii=False)}}]},
                                           ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args.api_url = f'http://127.0.0.1:{server.server_port}/v1'
            args.model = 'literal-fake'
            args.only_round = 1
            args.finalize_only = False
            try:
                bridge.generate_boundaries(args)
                self.assertEqual(bridge.read(args.out/'boundary/ORCHESTRATION_STATUS.json')['state'], 'ready_to_finalize')
                args.only_round = None
                args.finalize_only = True
                bridge.generate_boundaries(args)
                bridge.verify_boundaries(args)
                first_calls = len(calls)
                self.assertGreater(first_calls, 0)
                args.out = root/'second_mount'
                args.workers = 1
                args.only_round = 1
                args.finalize_only = False
                bridge.prepare(args)
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), first_calls)
                self.assertEqual(bridge.read(args.out/'boundary/ORCHESTRATION_STATUS.json')['state'], 'ready_to_finalize')
                args.only_round = None
                args.finalize_only = True
                report = bridge.generate_boundaries(args)
                self.assertEqual(len(calls), first_calls)
                self.assertTrue(report['groups']['mechanical_engineering']['cache_reused'])
                bridge.verify_boundaries(args)
                args.finalize_only = False
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), first_calls)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_exhausted_transport_uses_only_final_offline_empty_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            args, group = boundary_fixture(Path(directory))
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    calls.append(1)
                    self.send_response(502)
                    self.end_headers()

            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args.api_url = f'http://127.0.0.1:{server.server_port}/v1'
            args.model = 'literal-fake'
            args.finalize_only = False
            try:
                for phase in (1, 2, 3):
                    args.only_round = phase
                    bridge.generate_boundaries(args)
                    self.assertEqual(len(calls), phase)
                    self.assertFalse((group/'boundaries/summary.json').exists())
                    state = bridge.read(args.out/'boundary/ORCHESTRATION_STATUS.json')
                    self.assertEqual(state['pending_transport'], 1)
                self.assertEqual(state['state'], 'ready_to_finalize')
                args.only_round = None
                args.finalize_only = True
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), 3)
                summary = bridge.read(group/'boundaries/summary.json')
                self.assertEqual(summary['empty_boundary_nodes'], 4)
                self.assertEqual(summary['transport_unresolved'], 1)
                self.assertTrue(summary['semantic_quality_degraded'])
                history = list((group/'boundaries/group_history').glob('*.json'))
                self.assertEqual({bridge.read(path).get('last_attempt_round') for path in history}, {1, 2, 3})
                self.assertTrue(all(bridge.read(path)['output'].get('technical_kind') == 'transport'
                                    for path in history))
                bridge.verify_boundaries(args)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_transport_502_waits_for_next_cohort_and_finalizes_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            args, group = boundary_fixture(Path(directory))
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    payload = json.loads(wire['messages'][1]['content'])
                    calls.append([item['code'] for item in payload['targets']])
                    if len(calls) == 1:
                        self.send_response(502)
                        self.end_headers()
                        return
                    cards = [{'node_code': node['code'], 'definition': '示例定义',
                              'boundary': '示例边界', 'includes': ['示例'], 'excludes': [],
                              'sibling_distinctions': [], 'cross_boundary_rule': ''}
                             for node in payload['targets']]
                    response = json.dumps({'choices': [{'finish_reason': 'stop',
                                                         'message': {'content': json.dumps({'cards': cards}, ensure_ascii=False)}}]},
                                           ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args.api_url = f'http://127.0.0.1:{server.server_port}/v1'
            args.model = 'literal-fake'
            args.only_round = 1
            args.finalize_only = False
            try:
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), 1)
                self.assertEqual(bridge.read(args.out/'boundary/ORCHESTRATION_STATUS.json')['state'], 'awaiting_retry')
                self.assertFalse((group/'boundaries/summary.json').exists())
                args.only_round = 2
                args.workers = 1
                bridge.generate_boundaries(args)
                self.assertEqual(bridge.read(args.out/'boundary/ORCHESTRATION_STATUS.json')['state'], 'ready_to_finalize')
                before = len(calls)
                args.only_round = None
                args.finalize_only = True
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), before)
                summary = bridge.read(group/'boundaries/summary.json')
                self.assertTrue(summary['runtime_release'])
                self.assertEqual(summary['empty_boundary_nodes'], 0)
                args.finalize_only = False
                bridge.generate_boundaries(args)
                self.assertEqual(len(calls), before)
                bridge.verify_boundaries(args)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
