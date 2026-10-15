import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import layout_model_validation as module


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.books = self.root / "books.json"
        self.config = self.root / "config.json"
        module.write_json(self.config, {"api_url": "http://127.0.0.1:1234", "model": "test"})
        self.prepared = self.root / "prepared"
        self.classified = self.root / "classified"

    def make_books(self, count):
        books = []
        for index in range(count):
            path = self.root / f"{index}.md"
            path.write_text("正文内容\n第二行正文\n", encoding="utf-8")
            books.append({"identifier": f"b{index}", "md_path": str(path)})
        module.write_json(self.books, books)
        return books

    @staticmethod
    def fake_http(url, config, payload=None):
        if url.endswith("/v1/models"):
            return {"data": [{"id": "test"}]}
        return {"choices": [{"message": {"content": json.dumps({
            "status": "needs_review", "primary_class": None, "other_subtype": None,
            "review_reason": "insufficient_evidence", "basis": "正文结构证据不足",
            "is_mixed": False, "secondary_classes": [], "layout_features": [],
            "evidence": [{"line_id": "md:1", "quote": "正文内容", "role": "representative", "class_code": None}],
            "uncertainties": "章节范围不明",
        }, ensure_ascii=False)}}]}

    def stream(self, **kwargs):
        with patch.object(module, "http_json", side_effect=self.fake_http), patch.object(
            module, "ProcessPoolExecutor", side_effect=lambda **kw: ThreadPoolExecutor(max_workers=kw["max_workers"])
        ):
            return module.prepare_run(self.books, self.prepared, self.classified,
                                      self.config, workers=2, prepare_workers=2, **kwargs)

    def make_n1_classification(self):
        books = self.make_books(2)
        for book in books:
            Path(book["md_path"]).write_text("# 第一章\n正文内容\n第二行正文\n", encoding="utf-8")

        def respond(url, config, payload=None):
            if url.endswith("/v1/models"):
                return {"data": [{"id": "test"}]}
            return {"choices": [{"message": {"content": json.dumps({
                "status": "classified", "primary_class": "N1", "other_subtype": None,
                "review_reason": "", "basis": "标题划分正文", "is_mixed": False,
                "secondary_classes": [], "layout_features": ["标题层级清晰"],
                "evidence": [
                    {"line_id": "md:1", "quote": "第一章", "role": "heading", "class_code": "N1"},
                    {"line_id": "md:2", "quote": "正文内容", "role": "prose", "class_code": "N1"},
                ], "uncertainties": "无",
            }, ensure_ascii=False)}}]}

        self.assertEqual(module.prepare(self.books, None, None, self.prepared)["prepared"], 2)
        with patch.object(module, "http_json", side_effect=respond):
            result = module.run(self.prepared, self.classified, self.config, workers=2)
        self.assertEqual(result["statuses"], {"classified": 2})

    def test_model_starts_before_last_preparation_finishes(self):
        self.make_books(3)
        real = module.prepare_one
        last_finished = threading.Event()
        model_started = threading.Event()

        def prepare(item, *args):
            if item["identifier"] == "b2":
                self.assertTrue(model_started.wait(5))
                time.sleep(0.03)
            result = real(item, *args)
            if item["identifier"] == "b2":
                last_finished.set()
            return result

        real_process = module.process

        def classify(*args):
            model_started.set()
            return real_process(*args)

        with patch.object(module, "prepare_one", side_effect=prepare), patch.object(
            module, "process", side_effect=classify
        ):
            summary = self.stream(max_pending=2)
        self.assertEqual(summary["finished"], 3)
        self.assertTrue(last_finished.is_set())
        events = module.read_json(self.classified / "SCHEDULER_EVENTS.json")
        self.assertLess(events["model_first_started_at"], events["prepare_last_finished_at"])
        self.assertIn("model_started", [item["event"] for item in events["events"]])
        self.assertIn("prepare_finished", [item["event"] for item in events["events"]])

    def test_bad_book_is_technical_failure_and_other_book_continues(self):
        books = self.make_books(2)
        books[0]["md_path"] = str(self.root / "missing.md")
        module.write_json(self.books, books)
        summary = self.stream(max_pending=2)
        self.assertEqual(summary["statuses"], {"technical_failed": 1, "needs_review": 1})
        self.assertEqual(module.read_json(self.prepared / "PREPARED.json")["failures"], 1)

    def test_empty_cohort_skips_model_and_writes_native_outputs(self):
        module.write_json(self.books, [])
        with patch.object(module, "http_json", side_effect=AssertionError("model called")), patch.object(
            module, "ProcessPoolExecutor", side_effect=lambda **kw: ThreadPoolExecutor(max_workers=kw["max_workers"])
        ):
            summary = module.prepare_run(self.books, self.prepared, self.classified,
                                         self.config, workers=2, prepare_workers=2)
        self.assertEqual(summary["expected"], 0)
        self.assertEqual(summary["finished"], 0)
        self.assertTrue((self.classified / "DONE.json").is_file())

    def test_large_cohort_has_bounded_inflight_work(self):
        self.make_books(1200)
        summary = self.stream(max_pending=3)
        self.assertEqual(summary["finished"], 1200)
        events = module.read_json(self.classified / "SCHEDULER_EVENTS.json")
        self.assertLessEqual(events["prepare_inflight_peak"], 2)
        self.assertLessEqual(events["model_inflight_peak"], 3)
        self.assertLessEqual(events["pipeline_inflight_peak"], 3)

    def test_source_drift_fails_closed(self):
        self.make_books(1)
        real = module.prepare_one

        def mutate(item, *args):
            result = real(item, *args)
            Path(item["md_path"]).write_text("modified", encoding="utf-8")
            return result

        with patch.object(module, "prepare_one", side_effect=mutate):
            summary = self.stream(max_pending=1)
        self.assertEqual(summary["statuses"], {"technical_failed": 1})
        self.assertIn("md_source_changed", module.read_json(
            self.classified / "results" / "b0.json")["errors"][0])

    def test_rejects_duplicate_and_malformed_books(self):
        books = self.make_books(1)
        module.write_json(self.books, books * 2)
        with self.assertRaises(ValueError):
            self.stream()
        module.write_json(self.books, [{"identifier": "ok"}])
        with self.assertRaises(ValueError):
            self.stream()

    def test_rejects_same_output_directory(self):
        self.make_books(1)
        with self.assertRaisesRegex(ValueError, "stream_output_directories_overlap"):
            module.prepare_run(self.books, self.prepared, self.prepared, self.config)

    def test_real_process_pool_and_legacy_interfaces(self):
        self.make_books(2)
        with patch.object(module, "http_json", side_effect=self.fake_http):
            result = module.prepare_run(self.books, self.prepared, self.classified,
                                        self.config, workers=2, prepare_workers=2, max_pending=2)
        self.assertEqual(result["statuses"], {"needs_review": 2})
        self.assertEqual(module.verify(self.classified)["finished"], 2)
        legacy_prepared = self.root / "legacy_prepared"
        legacy_classified = self.root / "legacy_classified"
        self.assertEqual(module.prepare(self.books, None, None, legacy_prepared)["prepared"], 2)
        with patch.object(module, "http_json", side_effect=self.fake_http):
            self.assertEqual(module.run(legacy_prepared, legacy_classified, self.config,
                                        workers=2)["finished"], 2)

    def test_config_and_code_drift_fail_verification(self):
        self.make_books(1)
        self.stream()
        original = module.read_json(self.config)
        module.write_json(self.config, {**original, "max_tokens": 1024})
        with self.assertRaisesRegex(ValueError, "config_changed"):
            module.verify(self.classified)
        module.write_json(self.config, original)
        manifest_path = self.classified / "manifest.json"
        manifest = module.read_json(manifest_path)
        manifest["code_sha256"] = "0" * 64
        module.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "code_changed"):
            module.verify(self.classified)

    def test_route_selected_subset_preserves_original_manifests(self):
        self.make_n1_classification()
        paths = [self.prepared / "manifest.json", self.classified / "manifest.json",
                 self.classified / "results" / "b0.json"]
        before = [path.read_bytes() for path in paths]
        result = module.route_extractors(self.prepared, self.classified,
                                         self.root / "selected", selected_refs=["b0"])
        self.assertEqual(result["selection_input_count"], 1)
        self.assertEqual(result["routed_selected_count"], 1)
        self.assertEqual([item["ref"] for item in result["routing"]], ["b0"])
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_route_selected_rejects_unknown_and_duplicate_ids(self):
        self.make_n1_classification()
        for refs, message in [(["missing"], "unknown_selected_ref"),
                              (["b0", "b0"], "duplicate_selected_ref")]:
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, message):
                module.route_extractors(self.prepared, self.classified,
                                        self.root / "selected", selected_refs=refs)

    def test_route_selected_rejects_non_n1_n3_status_or_class(self):
        self.make_n1_classification()
        path = self.classified / "results" / "b0.json"
        original = module.read_json(path)
        for changed in ({"status": "needs_review", "primary_class": None},
                        {"status": "classified", "primary_class": "OTHER"}):
            module.write_json(path, {**original, **changed})
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "invalid_selected_classification"):
                module.route_extractors(self.prepared, self.classified,
                                        self.root / "selected", selected_refs=["b0"])

    def test_route_selected_rejects_md_drift(self):
        self.make_n1_classification()
        (self.root / "0.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "md_source_changed"):
            module.route_extractors(self.prepared, self.classified,
                                    self.root / "selected", selected_refs=["b0"])

    def test_route_selected_rejects_modified_classification_evidence(self):
        self.make_n1_classification()
        result_path = self.classified / "results" / "b0.json"
        result = module.read_json(result_path)
        result["basis"] = "modified"
        module.write_json(result_path, result)
        with self.assertRaisesRegex(ValueError, "validation_drift"):
            module.route_extractors(self.prepared, self.classified,
                                    self.root / "selected", selected_refs=["b0"])

    def test_route_explicit_empty_selection_routes_nothing(self):
        self.make_n1_classification()
        result = module.route_extractors(self.prepared, self.classified,
                                         self.root / "selected", selected_refs=[])
        self.assertEqual(result["selection_input_count"], 0)
        self.assertEqual(result["routed_selected_count"], 0)
        self.assertEqual(result["routing"], [])


if __name__ == "__main__":
    unittest.main()
