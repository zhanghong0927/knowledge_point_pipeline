"""Offline process tests for the resumable native dictionary driver."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[3]
DRIVER = ROOT / "modules/dictionary/src/run_fullbook_v5.py"
ADAPTER = ROOT / "adapters/dictionary_extraction.py"


class IdleMetrics(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class StagedDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.source = self.root / "book.md"
        self.source.write_text("# book\nbody\n", encoding="utf-8")
        self.manifest = self.root / "books.json"
        self.manifest.write_text(json.dumps([{"identifier": "book", "title": "Book", "md_path": str(self.source)}]), encoding="utf-8")
        self.runner = self.root / "fake_runner.py"
        self.runner.write_text('''import argparse, json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--out',type=Path,required=True)
p.add_argument('--transport-round',type=int,required=True)
a,_=p.parse_known_args()
count_file=a.out/'model_calls.json'
calls=json.loads(count_file.read_text()) if count_file.exists() else []
calls.append(a.transport_round)
count_file.write_text(json.dumps(calls))
pending=int(a.transport_round<3)
if (a.out/'force_no_pending').exists(): pending=0
if (a.out/'force_exhausted').exists(): pending=0; unresolved=1
else: unresolved=pending
status='partial' if unresolved or (a.out/'force_content_partial').exists() else 'completed'
if (a.out/'force_technical_partial').exists(): status='partial'
book={'identifier':'book','status':status,'eligible_entries':0,
 'http504_pending_chunks':pending,'http504_unresolved_chunks':unresolved,
 'chunks':[{'lo':0,'hi':1,'status':status}]}
import hashlib
folder=a.out/hashlib.sha256(b'book').hexdigest()[:16]
(folder/'chunks').mkdir(parents=True,exist_ok=True)
for name,value in {'INPUT.json':{'book':{'identifier':'book'}},'SUMMARY.json':book,
 'units.json':[],'entries.json':[],'accepted_entries.json':[],
 'quarantined_entries.json':[]}.items():
 (folder/name).write_text(json.dumps(value))
(folder/'chunks/00000000_00000001.json').write_text(json.dumps({
 'status':status,'unresolved':([] if not unresolved else [{'error':'HTTP 504'}]),
 'transport_retryable':bool(pending),'http504_unresolved':bool(unresolved),
 'technical_failure_unresolved':bool(unresolved or (a.out/'force_technical_partial').exists())}))
(a.out/'SUMMARY.json').write_text(json.dumps({'expected_books':1,'finished_books':1,
 'books':[book],'http504_pending_chunks':pending,'http504_unresolved_chunks':unresolved}))
raise SystemExit(2 if status=='partial' else 0)
''', encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), IdleMetrics)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, *flags, adapter=False):
        command = [sys.executable, "-B", str(ADAPTER if adapter else DRIVER),
                   "--manifest", str(self.manifest), "--out", str(self.out),
                   "--api-url", "http://unused.test", "--model", "offline",
                   "--runner", str(self.runner), "--round-workers", "512", "128", "32",
                   "--metrics-url", f"http://127.0.0.1:{self.server.server_port}/metrics",
                   "--metrics-poll-interval", "0.01", "--metrics-max-wait", "2", *flags]
        return subprocess.run(command, capture_output=True, text=True,
                              env=dict(os.environ, PYTHONUTF8="1"), timeout=30)

    def read(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def book_folder(self):
        return self.out / hashlib.sha256(b"book").hexdigest()[:16]

    def test_separate_processes_preserve_identity_and_wait_for_each_round(self):
        for number, calls in [(1, [1]), (2, [1, 2]), (3, [1, 2, 3])]:
            result = self.call("--only-round", str(number))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.read("model_calls.json"), calls)
            self.assertEqual([r["round"] for r in self.read("ROUNDS.json")["rounds"]], calls)
            if number < 3:
                self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "awaiting_retry")
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "ready_to_finalize")
        self.assertFalse((self.out / "HANDOFF.json").exists())

    def test_exhausted_504_blocks_finalization_and_handoff(self):
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.call("--only-round", "2").returncode, 0)
        (self.out / "force_exhausted").touch()
        self.assertEqual(self.call("--only-round", "3").returncode, 1)
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "technical_failure")
        self.assertNotEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        self.assertFalse((self.out / "HANDOFF.json").exists())

    def test_skipping_round_and_replay_with_changed_manifest_are_rejected(self):
        self.assertNotEqual(self.call("--only-round", "2").returncode, 0)
        self.assertFalse((self.out / "model_calls.json").exists())
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.manifest.write_text(self.manifest.read_text() + " ", encoding="utf-8")
        self.assertNotEqual(self.call("--only-round", "2").returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_existing_handoff_without_driver_state_is_not_reused(self):
        self.out.mkdir()
        (self.out / "HANDOFF.json").write_text('{"status":"completed"}', encoding="utf-8")
        self.assertNotEqual(self.call("--only-round", "1").returncode, 0)
        self.assertFalse((self.out / "model_calls.json").exists())

    def test_verified_completed_round_is_idempotent_but_mutated_cache_is_rejected(self):
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        run_id = self.read("ROUNDS.json")["run_id"]
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])
        self.assertEqual(self.read("ROUNDS.json")["run_id"], run_id)
        cache = self.book_folder() / "chunks/00000000_00000001.json"
        cache.write_text("{}", encoding="utf-8")
        self.assertNotEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_restart_after_verified_round_completion_does_not_relaunch_model(self):
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        status = self.read("ORCHESTRATION_STATUS.json")
        status["state"] = "running"
        (self.out / "ORCHESTRATION_STATUS.json").write_text(json.dumps(status), encoding="utf-8")
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "awaiting_retry")
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_no_pending_stops_rounds_then_finalize_adapter_writes_handoff(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "ready_to_finalize")
        self.assertNotEqual(self.call("--only-round", "2").returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])
        self.assertEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        self.assertEqual(self.read("HANDOFF.json")["status"], "completed")
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_no_flag_adapter_reconciles_finalized_staged_output_without_model_call(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        handoff = self.read("HANDOFF.json")
        result = self.call(adapter=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("model_calls.json"), [1])
        self.assertEqual(self.read("HANDOFF.json"), handoff)
        self.assertEqual([r["round"] for r in self.read("ROUNDS.json")["rounds"]], [1])

    def test_no_flag_adapter_rejects_staged_source_and_handoff_drift_without_calls(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        handoff = self.out / "HANDOFF.json"
        original = handoff.read_bytes()
        handoff.write_text('{"status":"forged"}', encoding="utf-8")
        self.assertNotEqual(self.call(adapter=True).returncode, 0)
        self.assertEqual(handoff.read_text(encoding="utf-8"), '{"status":"forged"}')
        handoff.write_bytes(original)
        self.source.write_text("# changed\n", encoding="utf-8")
        self.assertNotEqual(self.call(adapter=True).returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_no_flag_adapter_rejects_payload_and_unresolved_drift(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.assertEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        self.assertNotEqual(self.call("--model", "different", adapter=True).returncode, 0)
        unresolved = self.out / "UNRESOLVED.jsonl"
        unresolved.write_text('{"forged":true}\n', encoding="utf-8")
        self.assertNotEqual(self.call(adapter=True).returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_no_flag_adapter_preserves_content_partial_permission(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        (self.out / "force_content_partial").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 2)
        self.assertEqual(self.call("--allow-partial", "--finalize-only", adapter=True).returncode, 0)
        self.assertNotEqual(self.call(adapter=True).returncode, 0)
        self.assertEqual(self.call("--allow-partial", adapter=True).returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_source_drift_rejected_before_next_round_or_finalization(self):
        self.assertEqual(self.call("--only-round", "1").returncode, 0)
        self.source.write_text("# changed\n", encoding="utf-8")
        self.assertNotEqual(self.call("--only-round", "2").returncode, 0)
        self.assertNotEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        self.assertEqual(self.read("model_calls.json"), [1])

    def test_content_partial_requires_explicit_adapter_permission(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        (self.out / "force_content_partial").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 2)
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "ready_to_finalize")
        self.assertNotEqual(self.call("--finalize-only", adapter=True).returncode, 0)
        self.assertFalse((self.out / "HANDOFF.json").exists())
        self.assertEqual(self.call("--allow-partial", "--finalize-only", adapter=True).returncode, 0)
        self.assertEqual(self.read("HANDOFF.json")["status"], "finished_with_pending")

    def test_non504_technical_partial_is_not_finalized_as_content(self):
        self.out.mkdir()
        (self.out / "force_no_pending").touch()
        (self.out / "force_technical_partial").touch()
        self.assertEqual(self.call("--only-round", "1").returncode, 2)
        self.assertNotEqual(self.call("--allow-partial", "--finalize-only", adapter=True).returncode, 0)
        self.assertEqual(self.read("ORCHESTRATION_STATUS.json")["state"], "technical_failure")
        self.assertFalse((self.out / "HANDOFF.json").exists())


if __name__ == "__main__":
    unittest.main()
