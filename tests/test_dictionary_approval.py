import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dictionary_approval", ROOT / "adapters/approve_dictionary_books.py")
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)
sys.path.insert(0, str(ROOT / "modules/dictionary/classification/scripts"))
from run_unseen20_classification import validate


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def fixture(root, modes):
    books, hashes = [], {}
    base, classified = root / "prepared", root / "classification"
    for index, mode in enumerate(modes):
        ref = f"book_{index}"
        md = root / f"{ref}.md"
        md.write_text("## FIRST\nFirst definition.\n\n## SECOND\nSecond definition.\n", encoding="utf-8")
        book = {"identifier": ref, "title": ref, "md_path": str(md),
                "subject_slug": "test", "scope_config": str(root / "scope.json")}
        books.append(book)
        windows, values = [], []
        for n, head, body in ((1, "FIRST", "First definition."), (4, "SECOND", "Second definition.")):
            h, b = {"line_id": f"md:{n}", "quote": head}, {"line_id": f"md:{n+1}", "quote": body}
            windows.append({"window_id": str(n), "zone": n, "kind": "targeted",
                            "lines": [{"id": h["line_id"], "text": "## " + head}, {"id": b["line_id"], "text": body}]})
            values.append({"window_id": str(n), "region": "entry_body", "body_scope": "primary",
                           "organization": "O1", "entries": [{"head": h, "body": b, "role": "main_entry"}],
                           "boundary_contract": {"status": "unsupported" if mode == "other" else "supported",
                                                 "reason": "fixture", "evidence": [h]},
                           "md_usable": mode != "md_review"})
        payload = {"windows": values, "applicability": {"decision": "compatible"}}
        if mode == "review":
            payload["applicability"] = {"decision": "uncertain"}
        if mode == "other":
            payload["applicability"] = {"decision": "other", "reason": "unsupported structure", "evidence": [
                {"window_id": v["window_id"], **v["entries"][0]["head"]} for v in values]}
        prep = {"ref": ref, "title": ref, "windows": windows,
                "md_sha256": hashlib.sha256(md.read_bytes()).hexdigest()}
        write(base / "prepared" / (ref + ".json"), prep)
        hashes[ref] = hashlib.sha256((base / "prepared" / (ref + ".json")).read_bytes()).hexdigest()
        write(classified / "prepared" / (ref + ".json"), prep)
        result = validate(payload, windows)
        result.update(ref=ref, title=ref, errors=[])
        if mode == "technical_failed":
            result = {"ref": ref, "status": "technical_failed", "errors": ["test timeout"]}
        write(classified / "results" / (ref + ".json"), result)
        write(classified / "raw" / (ref + "_0.json"), {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]})
    write(root / "books.json", books)
    write(base / "manifest.json", {"books": books, "targets": [b["identifier"] for b in books]})
    write(classified / "manifest.json", {"base": str(base), "targets": [b["identifier"] for b in books], "input_sha256": hashes})
    write(classified / "DONE.json", {"id_sets_equal": True})
    return root / "books.json", classified, books


class DictionaryApprovalTests(unittest.TestCase):
    def test_native_results_split_and_preserve_original_books(self):
        with tempfile.TemporaryDirectory() as d:
            path, classified, books = fixture(Path(d), ["approved", "other", "review", "technical_failed", "md_review"])
            approved, queues, audit, report = ADAPTER.convert(path, classified)
            self.assertEqual(approved, books[:1])
            self.assertEqual({k: len(v) for k, v in queues.items()}, {"other": 1, "review": 2, "technical_failed": 1})
            self.assertEqual(len(audit), 5)
            self.assertTrue(report["count_conserved"])
            self.assertFalse(audit[0]["classification"]["ready_for_extraction"])

    def test_changed_md_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as d:
            path, classified, books = fixture(Path(d), ["approved"])
            Path(books[0]["md_path"]).write_text("changed")
            approved, queues, _, _ = ADAPTER.convert(path, classified)
            self.assertFalse(approved)
            self.assertIn("MD changed", queues["technical_failed"][0]["error"])

    def test_saved_result_edit_does_not_override_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            path, classified, _ = fixture(Path(d), ["review"])
            target = classified / "results/book_0.json"
            result = json.loads(target.read_text())
            result["status"] = result["routing_status"] = "sample_supported"
            result["applicability_status"] = "model_considered_compatible"
            write(target, result)
            approved, queues, _, _ = ADAPTER.convert(path, classified)
            self.assertFalse(approved)
            self.assertEqual(len(queues["technical_failed"]), 1)

    def test_missing_result_is_a_technical_failure_not_drop(self):
        with tempfile.TemporaryDirectory() as d:
            path, classified, _ = fixture(Path(d), ["approved", "approved"])
            (classified / "results/book_1.json").unlink()
            approved, queues, _, report = ADAPTER.convert(path, classified)
            self.assertEqual(len(approved), 1)
            self.assertEqual(len(queues["technical_failed"]), 1)
            self.assertEqual(report["input_books"], 2)

    def test_book_metadata_mismatch_and_duplicate_id(self):
        with tempfile.TemporaryDirectory() as d:
            path, classified, books = fixture(Path(d), ["approved"])
            modified = copy.deepcopy(books)
            modified[0]["md_path"] = str(Path(d) / "different.md")
            write(path, modified)
            self.assertFalse(ADAPTER.convert(path, classified)[0])
            write(path, books * 2)
            with self.assertRaises(ValueError):
                ADAPTER.convert(path, classified)

    def test_pipeline_uses_generated_approval_only_on_dictionary_track(self):
        spec = importlib.util.spec_from_file_location("pipeline", ROOT / "pipeline.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg, values = module.load_config(ROOT / "configs/pipeline.example.json")
        stages = module.plan(cfg, values)
        approval = next(t for t in stages[1]["tasks"] if t["name"] == "approve_dictionary_books")
        self.assertIn(approval["produces"][0], stages[2]["tasks"][0]["requires"])
        cfg["track"] = "important"
        self.assertNotIn("approve_dictionary_books", str(module.plan(cfg, values)))


if __name__ == "__main__":
    unittest.main()
