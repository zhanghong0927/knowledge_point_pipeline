import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class IntegrationTests(unittest.TestCase):
    def test_english_name_moves_without_translation(self):
        normalize = module(ROOT / "adapters/normalize_records.py")
        raw = {"id": 5, "name": "Stable angina", "definition": "Stable angina is a condition.",
               "source": {"identifier": "book1", "head_spans": [[1, 8]]}}
        row, trace = normalize.standardize(raw, "standard", "医学", "medicine", 1)
        self.assertEqual(row["knowledge_point"], raw["name"])
        self.assertEqual(row["en_definition"], raw["definition"])
        self.assertEqual((row["name"], row["definition"]), ("", ""))
        self.assertEqual(trace["original_record"], raw)
        self.assertEqual(row["id"], "5")

    def test_important_heading_is_not_assumed_english(self):
        normalize = module(ROOT / "adapters/normalize_records.py")
        raw = {"identifier": "book1", "knowledge_point": "齿轮传动", "source_quote": "齿轮传动"}
        row, _ = normalize.standardize(raw, "important", "机械工程", "mechanical_engineering", 1)
        self.assertEqual(row["name"], "齿轮传动")
        self.assertEqual(row["knowledge_point"], "")
        self.assertEqual(row["definition"], "")
        self.assertEqual(row["explanation"], "齿轮传动")

    def test_conflicting_languages_are_preserved_for_model(self):
        normalize = module(ROOT / "adapters/normalize_records.py")
        raw = {"id": "1", "name": "gear", "knowledge_point": "wheel"}
        row, _ = normalize.standardize(raw, "standard", "机械工程", "mechanical_engineering", 1)
        self.assertEqual(row["name"], "gear")
        self.assertEqual(row["knowledge_point"], "wheel")

    def test_plan_offline_and_builtin_mount_interface(self):
        pipeline = module(ROOT / "pipeline.py")
        config, values = pipeline.load_config(ROOT / "configs/pipeline.example.json")
        plan = pipeline.plan(config, values)
        self.assertEqual([s["stage"] for s in plan], ["01", "02", "03", "04", "05", "06"])
        self.assertTrue(all(not task['blocked'] for task in plan[4]['tasks']))
        self.assertEqual(plan[4]['tasks'][-1]['name'], 'export_mounting')
        self.assertIn("/v1/chat/completions", values["chat_url"])
        config["track"] = "important"
        important = pipeline.plan(config, values)
        self.assertIn("important_route.py", str(important[2]))
        self.assertNotIn("run_fullbook_v5.py", str(important[2]))

    def test_normalize_rules_and_restore_trace_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "input.jsonl"
            rows = [{"id": "x", "name": "gear", "definition": "A toothed wheel.",
                     "source": {"identifier": "book1", "md_sha256": "example"}},
                    {"id": "y", "name": "A", "definition": "a heading"}]
            raw.write_text("".join(json.dumps(r) + "\n" for r in rows))
            script = ROOT / "adapters/normalize_records.py"
            subprocess.run([sys.executable, script, "--input", raw, "--subject", "机械工程",
                            "--slug", "mechanical_engineering", "--out", root / "normalized"],
                           check=True, capture_output=True)
            subprocess.run([sys.executable, ROOT / "modules/cleaning/scripts/rule_clean.py", "--input",
                            root / "normalized/records.jsonl", "--output-dir", root / "rules"],
                           check=True, capture_output=True)
            kept = [json.loads(l) for l in (root / "rules/rule_pass.jsonl").read_text().splitlines()]
            self.assertEqual([r["id"] for r in kept], ["x"])
            # Simulate a model-keep standard record without invoking an API.
            (root / "kept.jsonl").write_text(json.dumps(kept[0]) + "\n")
            subprocess.run([sys.executable, script, "--restore", "--input", root / "kept.jsonl",
                            "--trace", root / "normalized/trace.jsonl", "--subject", "机械工程",
                            "--slug", "mechanical_engineering", "--out", root / "export"],
                           check=True, capture_output=True)
            restored = json.loads((root / "export/records.jsonl").read_text())
            self.assertEqual(restored["source"], rows[0]["source"])
            self.assertEqual(restored["tag"], "机械工程")

    def test_duplicate_ids_fail_before_committing_normalized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "input.json"
            raw.write_text(json.dumps([{"id": 1, "name": "gear"}, {"id": "1", "name": "wheel"}]))
            result = subprocess.run([sys.executable, ROOT / "adapters/normalize_records.py", "--input", raw,
                                     "--out", root / "out", "--subject", "机械工程", "--slug", "mechanical_engineering"],
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "out/records.jsonl").exists())

    def test_stage_six_merges_two_tracks_with_same_path_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "dictionary.jsonl"
            second = base / "important.jsonl"
            first.write_text(json.dumps({"id": "d1", "name": "齿轮", "definition": "短定义",
                                         "main_tags": "机械/传动", "tag": "机械工程"}) + "\n")
            rows = [{"id": "i1", "name": "齿轮", "definition": "这是更长的定义内容",
                     "main_tags": "机械/传动", "tag": "机械工程"},
                    {"id": "i2", "name": "齿轮", "definition": "其他路径同名",
                     "main_tags": "机械/制造", "tag": "机械工程"}]
            second.write_text("".join(json.dumps(r) + "\n" for r in rows))
            config = json.loads((ROOT / "configs/pipeline.example.json").read_text())
            config["paths"]["run"] = str(base / "run")
            config["dedup_inputs"] = [str(first), str(second)]
            path = base / "config.json"
            path.write_text(json.dumps(config))
            result = subprocess.run([sys.executable, ROOT / "pipeline.py", "run", "--config", path,
                                     "--stages", "06"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            retained = [json.loads(line) for line in (base / "run/06_dedup/retained.jsonl").read_text().splitlines()]
            self.assertEqual({r["id"] for r in retained}, {"i1", "i2"})
            self.assertTrue((base / "run/06_dedup/VERIFICATION.json").is_file())


if __name__ == "__main__":
    unittest.main()
