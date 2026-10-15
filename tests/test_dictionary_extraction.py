import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline


class DictionaryExtractionTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("dictionary_extraction", ROOT / "adapters/dictionary_extraction.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, root, status="partial"):
        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        manifest = root / "books.json"
        save(manifest, [{"identifier": "book"}])
        book = {"identifier": "book", "status": status, "entries": 1, "eligible_entries": 1,
                "http504_pending_chunks": 0, "http504_unresolved_chunks": 0,
                "chunks": [{"lo": 0, "hi": 1, "status": "partial" if status == "partial" else "completed"}]}
        out = root / "out"
        save(out / "SUMMARY.json", {"expected_books": 1, "finished_books": 1, "books": [book],
                                     "http504_pending_chunks": 0, "http504_unresolved_chunks": 0})
        save(out / "ORCHESTRATION_STATUS.json", {"state": status})
        save(out / "book/SUMMARY.json", book)
        save(out / "book/accepted_entries.json", [{"id": "kp", "source": {"identifier": "book"}, "eligible_for_name_screening": True}])
        save(out / "book/quarantined_entries.json", [])
        save(out / "book/units.json", [])
        save(out / "book/chunks/00000000_00000001.json", {"unresolved": [{"error": "unlocated head"}]})
        return manifest, out

    def test_partial_is_explicit_and_unresolved_is_separate(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            report, pending = self.load().finalized_report(manifest, out, 2, True)
            self.assertEqual(report["status"], "finished_with_pending")
            self.assertEqual(report["partial_books"], 1)
            self.assertEqual(report["completed_books"], 0)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["error"], "unlocated head")

    def test_strict_mode_does_not_accept_partial(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            with self.assertRaisesRegex(ValueError, "allow_partial"):
                self.load().finalized_report(manifest, out, 2, False)

    def test_technical_failure_never_enters_cleaning(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d), "technical_failure")
            with self.assertRaises(ValueError):
                self.load().finalized_report(manifest, out, 2, True)

    def test_unresolved_timeout_does_not_become_content_partial(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            summary = json.loads((out / "SUMMARY.json").read_text())
            summary["http504_unresolved_chunks"] = 1
            (out / "SUMMARY.json").write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "HTTP504"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_partial_with_other_api_errors_requires_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            summary = json.loads((out / "SUMMARY.json").read_text())
            summary["books"][0]["execution"] = {"technical_errors": 1}
            (out / "SUMMARY.json").write_text(json.dumps(summary))
            (out / "book/SUMMARY.json").write_text(json.dumps(summary["books"][0]))
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_explicitly_recovered_partial_allows_historical_error_count(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            summary = json.loads((out / "SUMMARY.json").read_text())
            summary["books"][0]["execution"] = {"technical_errors": 1}
            (out / "SUMMARY.json").write_text(json.dumps(summary))
            (out / "book/SUMMARY.json").write_text(json.dumps(summary["books"][0]))
            chunk = out / "book/chunks/00000000_00000001.json"
            chunk.write_text(json.dumps({"status": "partial", "technical_failure_unresolved": False,
                                         "unresolved": [{"error": "unlocated head"}]}))
            report, pending = self.load().finalized_report(manifest, out, 2, True)
            self.assertEqual(report["status"], "finished_with_pending")
            self.assertEqual(len(pending), 1)

    def test_legacy_partial_response_without_failure_checkpoint_is_not_recovery_proof(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            summary = json.loads((out / "SUMMARY.json").read_text())
            summary["books"][0]["execution"] = {"technical_errors": 1}
            (out / "SUMMARY.json").write_text(json.dumps(summary))
            (out / "book/SUMMARY.json").write_text(json.dumps(summary["books"][0]))
            response = out / "book/responses/00000000_00000001_2.json"
            response.parent.mkdir(parents=True)
            response.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_cached_partial_with_persisted_technical_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            chunk = out / "book/chunks/00000000_00000001.json"
            chunk.write_text(json.dumps({"status": "partial", "technical_failure_unresolved": True,
                                         "unresolved": [{"error": "TimeoutError: model unavailable"}]}))
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_legacy_cached_partial_with_unanswered_request_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            error = out / "book/errors/00000000_00000001_2.json"
            error.parent.mkdir(parents=True)
            error.write_text(json.dumps({"error": "TimeoutError: model unavailable", "http_status": None}))
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_unanswered_request_error_overrides_false_marker(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            chunk = out / "book/chunks/00000000_00000001.json"
            chunk.write_text(json.dumps({"status": "partial", "technical_failure_unresolved": False,
                                         "unresolved": [{"error": "unlocated head"}]}))
            error = out / "book/errors/00000000_00000001_2.json"
            error.parent.mkdir(parents=True)
            error.write_text(json.dumps({"error": "TimeoutError: model unavailable"}))
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_legacy_cached_partial_with_only_exception_reason_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            chunk = out / "book/chunks/00000000_00000001.json"
            chunk.write_text(json.dumps({"status": "partial", "transport_retryable": False,
                                         "http504_unresolved": False,
                                         "unresolved": [{"error": "TimeoutError: model unavailable"}]}))
            with self.assertRaisesRegex(ValueError, "Technical errors"):
                self.load().finalized_report(manifest, out, 2, True)

    def test_legacy_error_followed_by_response_allows_semantic_partial(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            summary = json.loads((out / "SUMMARY.json").read_text())
            summary["books"][0]["execution"] = {"technical_errors": 1}
            (out / "SUMMARY.json").write_text(json.dumps(summary))
            (out / "book/SUMMARY.json").write_text(json.dumps(summary["books"][0]))
            error = out / "book/errors/00000000_00000001_1.json"
            error.parent.mkdir(parents=True)
            error.write_text(json.dumps({"error": "TimeoutError: temporary"}))
            response = out / "book/responses/00000000_00000001_2.json"
            response.parent.mkdir(parents=True)
            response.write_text("{}")
            report, pending = self.load().finalized_report(manifest, out, 2, True)
            self.assertEqual(report["status"], "finished_with_pending")
            self.assertEqual(len(pending), 1)

    def test_completed_leaf_with_old_request_error_does_not_block_partial_book(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d))
            for summary in (out / "SUMMARY.json", out / "book/SUMMARY.json"):
                data = json.loads(summary.read_text())
                book = data["books"][0] if "books" in data else data
                book["chunks"].insert(0, {"lo": 1, "hi": 2, "status": "completed"})
                summary.write_text(json.dumps(data))
            error = out / "book/errors/00000001_00000002_1.json"
            error.parent.mkdir(parents=True)
            error.write_text(json.dumps({"error": "TimeoutError: temporary"}))
            report, pending = self.load().finalized_report(manifest, out, 2, True)
            self.assertEqual(report["status"], "finished_with_pending")
            self.assertEqual(len(pending), 1)

    def test_completed_does_not_require_partial_switch(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, out = self.fixture(Path(d), "completed")
            report, _ = self.load().finalized_report(manifest, out, 0, False)
            self.assertEqual(report["status"], "completed")

    def test_pipeline_switch_is_dictionary_only_and_defaults_strict(self):
        config, values = pipeline.load_config(ROOT / "configs/pipeline.example.json")
        def extraction():
            return next(t for s in pipeline.plan(config, values) for t in s["tasks"] if t["name"] == "extract_fullbook")
        self.assertNotIn("--allow-partial", extraction()["command"])
        config["dictionary_extraction"] = {"allow_partial": True}
        self.assertIn("--allow-partial", extraction()["command"])
        config["dictionary_extraction"]["allow_partial"] = "true"
        with self.assertRaisesRegex(ValueError, "boolean"):
            extraction()

    def test_pipeline_passes_configured_retry_concurrency(self):
        config, values = pipeline.load_config(ROOT / "configs/pipeline.example.json")
        config["dictionary_extraction"]["round_workers"] = [256, 64, 16]
        task = next(t for s in pipeline.plan(config, values) for t in s["tasks"]
                    if t["name"] == "extract_fullbook")
        command = task["command"]
        position = command.index("--round-workers")
        self.assertEqual(command[position + 1:position + 4], ["256", "64", "16"])

    def test_pipeline_rejects_invalid_retry_concurrency(self):
        config, values = pipeline.load_config(ROOT / "configs/pipeline.example.json")
        for invalid in (None, [], [256, 64], [256, 64, 16, 8], [0, 64, 16],
                        [256, True, 16], [256.0, 64, 16], "256,64,16"):
            with self.subTest(round_workers=invalid):
                config["dictionary_extraction"]["round_workers"] = invalid
                with self.assertRaisesRegex(ValueError, "three positive integers"):
                    pipeline.plan(config, values)

    def test_eight_card_profile_keeps_all_api_limits_at_most_256(self):
        config, values = pipeline.load_config(ROOT / "configs/pipeline.8card.example.json")
        self.assertEqual(config["dictionary_extraction"]["round_workers"], [256, 64, 16])
        for key in ("screening_workers", "classification_workers", "cleaning_workers"):
            self.assertEqual(config[key], 256)
        self.assertEqual(config["mounting"]["workers"], 256)
        self.assertEqual(config['mounting']['boundaries']['source'],'project')
        self.assertEqual(config["paths"]["approved_books"], "")
        for stage in pipeline.plan(config, values):
            for task in stage["tasks"]:
                command = task["command"]
                if "--workers" in command:
                    self.assertLessEqual(int(command[command.index("--workers") + 1]), 256)


if __name__ == "__main__":
    unittest.main()
