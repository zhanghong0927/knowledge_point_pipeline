import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class DictionaryHandoffTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec('dictionary_handoff')
        self.assertIsNotNone(spec, 'a guarded dictionary-completion handoff is required')
        import dictionary_handoff
        return dictionary_handoff

    @staticmethod
    def summary(root, status='completed_native_stages'):
        path = root / 'dictionary/SUMMARY.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'track': 'dictionary', 'status': status,
            'selected_books': 30, 'completed_cleaning_books': 30, 'dedup_completed': 2,
            'unresolved_stages': [], 'semantic_quality_approved': False}), encoding='utf-8')

    def test_absent_summary_cannot_trigger_any_signal_or_inference(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp, patch.object(module.os, 'killpg', create=True) as kill:
            self.assertIsNone(module.boundary_summary(Path(temp)))
            kill.assert_not_called()

    def test_invalid_or_wrong_track_summary_is_rejected(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.summary(root, 'running')
            with self.assertRaises(ValueError):
                module.boundary_summary(root)
            self.summary(root)
            path = root / 'dictionary/SUMMARY.json'
            row = json.loads(path.read_text())
            row['track'] = 'important'
            path.write_text(json.dumps(row))
            with self.assertRaises(ValueError):
                module.boundary_summary(root)

    def test_unresolved_dictionary_is_reported_without_semantic_approval(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.summary(root, 'finished_with_unresolved')
            report = module.boundary_summary(root)
            self.assertEqual(report['summary']['status'], 'finished_with_unresolved')
            self.assertFalse(report['summary']['semantic_quality_approved'])
            self.assertEqual(len(report['sha256']), 64)

    def test_pid_reuse_wrong_argv_or_nonisolated_session_cannot_signal(self):
        module = self.module()
        expected = {'pid': 9001, 'pgrp': 9001, 'session': 9001, 'start_ticks': 100,
                    'uid': 1, 'argv': ['python', 'run']}
        for key, bad in (('start_ticks', 101), ('argv', ['unrelated']),
                         ('session', 1), ('uid', 2), ('pgrp', 1)):
            value = dict(expected, state='S')
            value[key] = bad
            with patch.object(module.os, 'killpg', create=True) as kill:
                with self.assertRaises(ValueError):
                    module.validate_identity(value, expected)
                kill.assert_not_called()

    def test_all_stopped_members_are_required_before_gateway_is_resumed(self):
        module = self.module()
        expected = {'pid': 9001, 'pgrp': 9001, 'session': 9001, 'uid': 1}
        controller = dict(expected, state='T')
        child = dict(controller, pid=9002, state='S')
        with patch.object(module, 'group_members', return_value=[controller, child]):
            with self.assertRaises(TimeoutError):
                module.wait_stopped_group(expected, timeout=.001)

    def test_important_active_requests_drain_even_with_a_paused_queue(self):
        module = self.module()
        states = [{'paused': True, 'total': {'active': 1, 'queued': 4}},
                  {'paused': True, 'total': {'active': 0, 'queued': 4}}]
        with patch('adapters.gateway_control._request', side_effect=states) as request:
            receipt = module.drain_gateway('http://127.0.0.1:12345', timeout=.1)
        self.assertEqual(receipt['total']['active'], 0)
        self.assertEqual(receipt['total']['queued'], 4)
        self.assertEqual(request.call_count, 2)

    def test_killed_group_must_be_confirmed_empty_before_success(self):
        module = self.module()
        expected = {'pid': 9001, 'pgrp': 9001, 'session': 9001, 'uid': 1}
        with patch.object(module, 'group_members', return_value=[expected]), \
                patch.object(module.signal, 'SIGKILL', 9, create=True), \
                patch.object(module.os, 'killpg', create=True) as kill:
            with self.assertRaises(TimeoutError):
                module.wait_empty_group(expected, terminate_timeout=.001, kill_timeout=.001)
        kill.assert_called_once_with(9001, 9)

    def test_restore_failure_is_visible_and_does_not_resume_controller(self):
        module = self.module()
        with patch('adapters.gateway_control._request', side_effect=OSError('offline')), \
                patch.object(module.os, 'killpg', create=True) as kill:
            with self.assertRaises(module.HandoffRecoveryError) as raised:
                module.restore_gateway('http://127.0.0.1:12345', False)
        self.assertEqual(raised.exception.receipt['state'], 'recovery_failed')
        kill.assert_not_called()

    def test_restore_preserves_original_paused_state(self):
        module = self.module()
        with patch('adapters.gateway_control._request', return_value={'paused': True}) as request:
            module.restore_gateway('http://127.0.0.1:12345', True)
        self.assertTrue(request.call_args.args[3]['paused'])

    @unittest.skipUnless(sys.platform == 'linux', 'isolated POSIX process-group test')
    def test_real_gateway_drains_old_important_client_before_retirement(self):
        import shutil
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from adapters.gateway_control import _request
        module = self.module()
        release, entered = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                entered.set()
                if not release.wait(15):
                    raise RuntimeError('fake upstream not released')
                body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = root / 'code'
            (code / 'adapters').mkdir(parents=True)
            original = Path(module.__file__).parent / 'adapters/inference_gateway.py'
            shutil.copy2(original, code / 'adapters/inference_gateway.py')
            script = code / 'staged_benchmark.py'
            script.write_text('''import json,pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[sys.argv.index('--root')+1])
subprocess.Popen([sys.executable,'-B',str(root/'code/adapters/inference_gateway.py'),
 '--upstream',sys.argv[sys.argv.index('--fake')+1],'--limit','8','--out',str(root/'gateway')])
while not (root/'gateway/READY.json').exists(): time.sleep(.01)
url=json.loads((root/'gateway/READY.json').read_text())['api_url']
program="import sys,urllib.request; r=urllib.request.Request(sys.argv[1]+'/_jobs/important_meta/v1/chat/completions',data=b'{}',headers={'Content-Type':'application/json'}); urllib.request.urlopen(r,timeout=20).read()"
subprocess.Popen([sys.executable,'-B','-c',program,url])
while True: time.sleep(.05)
''')
            command = [sys.executable, '-B', '-u', str(script), 'run', '--root', str(root),
                       '--fake', f'http://127.0.0.1:{server.server_port}']
            child = subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            timer = None
            try:
                self.assertTrue(entered.wait(15))
                ready = json.loads((root / 'gateway/READY.json').read_text())
                self.assertEqual(_request(ready['api_url'], 'GET', '/_scheduler/stats')['total']['active'], 1)
                self.assertIsNone(module.boundary_summary(root))
                self.summary(root)
                expected = module.process_identity(child.pid)
                timer = threading.Timer(.15, release.set)
                timer.start()
                receipt = module.retire_launch(root, expected)
                child.wait(timeout=5)
                self.assertEqual(receipt['old_gateway_drain']['total']['active'], 0)
                self.assertEqual(receipt['termination']['remaining_live_processes'], 0)
                self.assertTrue(receipt['protected_files_unchanged'])
            finally:
                release.set()
                if timer:
                    timer.join(2)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                server.shutdown()
                server.server_close()
                serving.join(3)

    @unittest.skipUnless(sys.platform == 'linux', 'isolated POSIX process-group test')
    def test_real_group_is_untouched_before_summary_and_retired_after_boundary(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = root / 'code'
            code.mkdir()
            script = code / 'staged_benchmark.py'
            script.write_text('import time\nwhile True: time.sleep(.05)\n')
            command = [sys.executable, '-u', str(script), 'run', '--root', str(root)]
            child = subprocess.Popen(command, start_new_session=True)
            try:
                expected = module.process_identity(child.pid)
                self.assertIsNone(module.boundary_summary(root))
                self.assertIsNone(child.poll())
                self.summary(root)
                protected = module.protected_hashes(root)
                receipt = module.retire_launch(root, expected, require_gateway=False)
                child.wait(timeout=5)
                self.assertEqual(receipt['state'], 'retired_after_dictionary')
                self.assertEqual(protected, module.protected_hashes(root))
                self.assertIsNotNone(child.returncode)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGCONT)
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
