"""Cold-copy regressions for failures that prevent the important-book run."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from staged_benchmark import mounting_dispositions
from test_mounting_boundary_staged import boundary_fixture
from test_mounting_bridge import bridge

ROOT = Path(__file__).resolve().parents[1]


class ContinuationRepairTests(unittest.TestCase):
    def test_ephemeral_running_marker_does_not_invalidate_completed_round(self):
        spec = importlib.util.spec_from_file_location(
            'continuation_portable', ROOT / 'modules/dictionary/portable_pipeline.py')
        portable = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(portable)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'accepted.json').write_text('{"accepted": 1}', encoding='utf-8')
            (out / '.running').write_text('lease', encoding='utf-8')
            saved = {'checkpoint_hashes': portable.staged_checkpoint_hashes(out)}
            (out / '.running').unlink()
            portable.verify_staged_checkpoint(out, saved)
            (out / 'accepted.json').write_text('{"accepted": 2}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'checkpoint changed'):
                portable.verify_staged_checkpoint(out, saved)

    def test_empty_mounting_cohort_has_all_explicit_zero_dispositions(self):
        transport, counts, content = mounting_dispositions([])
        self.assertEqual((transport, content), (0, 0))
        for name in ('review', 'technical_failure', 'unconfigured', 'missing_summary'):
            self.assertEqual(counts[name], 0)

    def test_generate_boundary_cli_runs_round_then_offline_finalize(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                payload = json.loads(wire['messages'][1]['content'])
                calls.append(1)
                cards = [{'node_code': node['code'], 'definition': '示例定义',
                          'boundary': '示例范围', 'includes': ['示例'], 'excludes': [],
                          'sibling_distinctions': [], 'cross_boundary_rule': ''}
                         for node in payload['targets']]
                raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'cards': cards}, ensure_ascii=False)}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args, _ = boundary_fixture(Path(tmp))
                argv = ['mounting_bridge.py', 'generate-boundaries','--asset-maintenance', '--out', str(args.out),
                        '--api-url', f'http://127.0.0.1:{server.server_port}/v1',
                        '--model', 'literal-fake', '--workers', '1',
                        '--boundary-review-mode', 'off', '--boundary-failure-policy', 'empty']
                with patch.object(sys, 'argv', argv + ['--only-round', '1']):
                    bridge.main()
                self.assertEqual(bridge.read(args.out / 'boundary/ORCHESTRATION_STATUS.json')['state'],
                                 'ready_to_finalize')
                self.assertGreater(len(calls), 0)
                before = len(calls)
                with patch.object(sys, 'argv', argv + ['--finalize-only']):
                    bridge.main()
                bridge.verify_boundaries(args)
                self.assertEqual(len(calls), before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
