import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fullbook_concurrency", ROOT / "modules/dictionary/src/run_fullbook_v5.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class ExtractionConcurrencyTests(unittest.TestCase):
    def arguments(self, root, extra=()):
        return DRIVER.parse_args([
            "--manifest", str(root / "books.json"), "--out", str(root / "out"),
            "--api-url", "http://localhost:8000", "--model", "test-model", *extra])

    def test_native_default_preserves_previous_rounds(self):
        args = self.arguments(Path("."))
        self.assertEqual(tuple(args.round_workers), (1024, 256, 64))

    def test_native_parser_accepts_eight_card_rounds(self):
        args = self.arguments(Path("."), ["--round-workers", "256", "64", "16"])
        self.assertEqual(tuple(args.round_workers), (256, 64, 16))

    def test_native_parser_rejects_invalid_rounds(self):
        for values in (["256", "64"], ["256", "64", "16", "8"],
                       ["256", "0", "16"], ["-1", "64", "16"],
                       ["256.0", "64", "16"]):
            with self.subTest(values=values), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.arguments(Path("."), ["--round-workers", *values])

    def test_all_three_rounds_use_configured_workers_and_record_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "books.json").write_text(
                json.dumps([{"identifier": "book"}]), encoding="utf-8")
            args = self.arguments(root, ["--round-workers", "256", "64", "16"])
            args.out.mkdir()
            driver = DRIVER.Orchestrator(args)
            rounds = []

            def run_round(number, workers, identifiers):
                rounds.append((number, workers, identifiers))
                command = driver.command(number, workers)
                self.assertEqual(command[command.index("--workers") + 1], str(workers))
                return {"http504_pending_chunks": int(number < 3),
                        "http504_unresolved_chunks": 0}, "completed"

            with patch.object(driver, "wait_idle"), patch.object(driver, "run_round", run_round):
                self.assertEqual(driver.run_locked(), 0)
            self.assertEqual(rounds, [(1, 256, ["book"]), (2, 64, ["book"]), (3, 16, ["book"])])
            persisted = json.loads((args.out / "ORCHESTRATION_STATUS.json").read_text())
            self.assertEqual(persisted["round_workers"], [256, 64, 16])

    def test_adapter_and_native_subprocesses_dispatch_all_eight_card_rounds(self):
        class MetricsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b"vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "books.json"
            manifest.write_text(json.dumps([{"identifier": "book"}]), encoding="utf-8")
            runner = root / "offline_runner.py"
            runner.write_text('''import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--out', type=Path, required=True)
p.add_argument('--workers', type=int, required=True)
p.add_argument('--transport-round', type=int, required=True)
a, _ = p.parse_known_args()
observed = a.out / 'observed_workers.json'
workers = json.loads(observed.read_text()) if observed.exists() else []
workers.append(a.workers)
observed.write_text(json.dumps(workers))
pending = int(a.transport_round < 3)
book = {'identifier': 'book', 'status': 'partial' if pending else 'completed',
        'eligible_entries': 1, 'http504_pending_chunks': pending,
        'http504_unresolved_chunks': pending, 'chunks': []}
folder = a.out / 'book'
folder.mkdir(exist_ok=True)
for name, data in {'SUMMARY.json': book, 'units.json': [],
                   'quarantined_entries.json': [],
                   'accepted_entries.json': [{'id': 'kp', 'source': {'identifier': 'book'},
                                             'eligible_for_name_screening': True}]}.items():
    (folder / name).write_text(json.dumps(data), encoding='utf-8')
(a.out / 'SUMMARY.json').write_text(json.dumps({
    'expected_books': 1, 'finished_books': 1, 'books': [book],
    'http504_pending_chunks': pending, 'http504_unresolved_chunks': pending}), encoding='utf-8')
raise SystemExit(2 if pending else 0)
''', encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run([
                    DRIVER.sys.executable, "-B", str(ROOT / "adapters/dictionary_extraction.py"),
                    "--manifest", str(manifest), "--out", str(root / "out"),
                    "--api-url", "http://unused.test:8000", "--model", "offline-test",
                    "--runner", str(runner), "--round-workers", "256", "64", "16",
                    "--metrics-url", f"http://127.0.0.1:{server.server_port}/metrics",
                    "--metrics-poll-interval", "0.01", "--metrics-max-wait", "2"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    env=dict(os.environ, PYTHONUTF8="1"), timeout=30)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(json.loads((root / "out/observed_workers.json").read_text()), [256, 64, 16])
            status = json.loads((root / "out/ORCHESTRATION_STATUS.json").read_text())
            self.assertEqual(status["round_workers"], [256, 64, 16])
            handoff = json.loads((root / "out/HANDOFF.json").read_text())
            self.assertEqual(handoff["status"], "completed")
            self.assertEqual(handoff["accepted_records"], 1)


if __name__ == "__main__":
    unittest.main()
