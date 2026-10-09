import json
import tempfile
import unittest
from pathlib import Path

import layout_model_validation as module


class DeliveryModuleTests(unittest.TestCase):
    def test_only_n1_n3_extractors_are_shipped(self):
        scripts = Path(module.__file__).parent
        self.assertEqual(
            {path.name for path in scripts.glob("*_extractor.py")},
            {"n1_heading_extractor.py", "n3_rules_extractor.py"},
        )
        self.assertEqual(set(module.CLASSES), {"N1", "N3", "OTHER"})
        self.assertEqual(set(module.OTHER_SUBTYPES), {"N2", "N4", "N5"})

    def test_routing_filters_n1_extracts_n3_and_skips_other(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepared = root / "prepared"
            classified = root / "classification"
            (prepared / "prepared").mkdir(parents=True)
            (classified / "results").mkdir(parents=True)
            n1 = root / "n1.md"
            n1.write_text(
                "# 第一章 管理学\n\n## 核心概念\n核心概念是管理理论中的基本术语。\n\n"
                "## 主要参考文献\n作者，2024。\n", encoding="utf-8"
            )
            n3 = root / "n3.md"
            n3.write_text(
                "# 第一章\n1. 岗位责任制：明确职责。\n"
                "2. 风险评估：评估风险。\n", encoding="utf-8"
            )
            refs = ["n1", "n3", "n2", "n4", "n5"]
            module.write_json(prepared / "manifest.json", {"targets": refs})
            for ref in refs:
                md = n1 if ref == "n1" else n3
                module.write_json(prepared / "prepared" / f"{ref}.json", {
                    "title": ref, "md_path": str(md),
                })
                primary = ref.upper() if ref in {"n1", "n3"} else "OTHER"
                module.write_json(classified / "results" / f"{ref}.json", {
                    "ref": ref, "status": "classified", "primary_class": primary,
                    "other_subtype": ref.upper() if primary == "OTHER" else None,
                })
            output = root / "extraction"
            summary = module.route_extractors(prepared, classified, output)
            self.assertEqual(summary["books_total"], 5)
            self.assertEqual(summary["extracted_books"], 2, summary["routing"])
            self.assertEqual(summary["other_books"], 3)
            self.assertGreater(summary["knowledge_points_extracted"], 0)
            n1_result = json.loads((output / "results" / "n1.json").read_text(encoding="utf-8"))
            self.assertIn("title_filter", n1_result)
            self.assertNotIn("主要参考文献", [
                point["knowledge_point"] for unit in n1_result["units"]
                for point in unit["knowledge_points"]
            ])
            self.assertTrue((output / "results" / "n3.json").is_file())
            for ref in ("n2", "n4", "n5"):
                route = json.loads((output / "results" / f"{ref}_routing.json").read_text(encoding="utf-8"))
                self.assertEqual(route["extraction_status"], "skipped_other")
                self.assertFalse((output / "results" / f"{ref}.json").exists())


if __name__ == "__main__":
    unittest.main()
