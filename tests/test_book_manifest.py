import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("manifest_adapter", ROOT / "adapters/screened_books_to_manifest.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManifestTests(unittest.TestCase):
    def prepare(self, root, rows, extra=()):
        path = root / "audit.csv"
        fields = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        return MODULE.parser().parse_args(["--input", str(path), "--out", str(root / "result"),
            "--track", "important", "--subject-slug", "mechanical_engineering", *extra])

    def row(self, identifier="b1", **values):
        return {"identifier": identifier, "title": "示例", "final_decision": "PASS",
                "book_track": "其他重要书籍", **values}

    def test_pass_filter_and_relative_md_with_optional_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b1.md").write_text("# 名称\n正文")
            rows = [self.row(md_path="b1.md"), self.row("b2", final_decision="REVIEW"),
                    self.row("b3", book_track="辞海类")]
            books, audit, excluded, unresolved, report = MODULE.convert(self.prepare(root, rows))
            self.assertEqual([b["identifier"] for b in books], ["b1"])
            self.assertEqual(books[0]["md_path"], str(root / "b1.md"))
            self.assertNotIn("pdf_path", books[0])
            self.assertEqual(len(excluded), 2)
            self.assertFalse(unresolved)
            self.assertTrue(report["count_conserved"])
            self.assertFalse(report["pdf_quality_checked"])
            self.assertEqual(audit[0]["original"], {k: rows[0].get(k, "") for k in audit[0]["original"]})

    def test_oss_prefix_mapping_and_pdf_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b1.md").write_text("# test")
            # Arbitrary bytes: the adapter attaches paths, and does not audit PDF quality.
            (root / "b1.pdf").write_bytes(b"assumed acceptable PDF")
            args = self.prepare(root, [self.row(parsed_path="oss://bucket/md/b1.md", documentpath="b1.pdf")],
                                ["--path-map", f"oss://bucket/md/={root}"])
            books, audit, _, unresolved, report = MODULE.convert(args)
            self.assertFalse(unresolved)
            self.assertEqual(books[0]["pdf_path"], str(root / "b1.pdf"))
            self.assertEqual(report["downloads"], 0)
            self.assertEqual(audit[0]["md_match"]["reference"], "oss://bucket/md/b1.md")

    def test_exact_identifier_match_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a/b1.md").write_text("x")
            args = self.prepare(root, [self.row()], ["--md-root", str(root)])
            self.assertEqual(len(MODULE.convert(args)[0]), 1)
            (root / "b").mkdir()
            (root / "b/b1.md").write_text("y")
            books, _, _, unresolved, report = MODULE.convert(args)
            self.assertFalse(books)
            self.assertEqual(unresolved[0]["detail"]["error"], "ambiguous_local_files")
            self.assertFalse(report["ready_for_classification"])

    def test_duplicate_ids_are_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args = self.prepare(root, [self.row(), self.row()])
            books, _, _, unresolved, report = MODULE.convert(args)
            self.assertFalse(books)
            self.assertEqual([r["reason"] for r in unresolved], ["duplicate_identifier"] * 2)
            self.assertTrue(report["count_conserved"])

    def test_dictionary_needs_scope_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b1.md").write_text("x")
            args = self.prepare(root, [self.row(md_path="b1.md", book_track="辞海类")])
            args.track = "dictionary"
            self.assertEqual(MODULE.convert(args)[3][0]["reason"], "dictionary_scope_config_missing")
            scope = root / "scope.json"
            scope.write_text("{}")
            args.scope_config = scope
            books = MODULE.convert(args)[0]
            self.assertEqual(books[0]["identifier"], "b1")
            self.assertEqual(books[0]["scope_config"], str(scope))

    def test_partial_failures_and_technical_failure_not_content_drop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b1.md").write_text("x")
            args = self.prepare(root, [self.row(md_path="b1.md"), self.row("b2", audit_status="api_failed")])
            _, _, excluded, unresolved, report = MODULE.convert(args)
            self.assertFalse(excluded)
            self.assertEqual(unresolved[0]["reason"], "technical_failure_despite_pass")
            self.assertFalse(report["ready_for_classification"])
            args.allow_partial = True
            self.assertTrue(MODULE.convert(args)[4]["ready_for_classification"])

    def test_missing_track_requires_explicit_assumption(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b1.md").write_text("x")
            row = self.row(md_path="b1.md")
            del row["book_track"]
            args = self.prepare(root, [row])
            with self.assertRaises(ValueError):
                MODULE.convert(args)
            args.assume_track = True
            self.assertEqual(len(MODULE.convert(args)[0]), 1)

    def test_pipeline_connects_generated_manifest_and_has_no_pdf_gate(self):
        spec = importlib.util.spec_from_file_location("pipeline", ROOT / "pipeline.py")
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        cfg, values = pipeline.load_config(ROOT / "configs/pipeline.example.json")
        plan = pipeline.plan(cfg, values)
        tasks = plan[1]["tasks"]
        converter = next(t for t in tasks if t["name"] == "convert_audited_books")
        classifier = next(t for t in tasks if t["name"] == "prepare_structure")
        self.assertIn(converter["produces"][0], classifier["requires"])
        self.assertNotIn("pdf_quality", json.dumps(plan))


if __name__ == "__main__":
    unittest.main()
