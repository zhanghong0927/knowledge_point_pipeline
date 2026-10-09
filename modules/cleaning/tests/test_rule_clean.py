from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rule_clean.py"
SPEC = importlib.util.spec_from_file_location("books_rule_clean", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RuleCleanTests(unittest.TestCase):
    def args(self, **changes):
        values = {
            "max_name_chars": 160,
            "max_context_chars": 6000,
            "keep_single_letter": False,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_single_latin_letter_is_rejected_by_default(self):
        cleaned, _flags, reason = MODULE.clean_title("A", 160, False)
        self.assertEqual(cleaned, "A")
        self.assertEqual(reason, "single_latin_letter")

    def test_single_latin_letter_can_be_kept_for_dictionary(self):
        _cleaned, _flags, reason = MODULE.clean_title("A", 160, True)
        self.assertIsNone(reason)

    def test_bullet_is_removed_but_term_is_kept(self):
        cleaned, flags, reason = MODULE.clean_title("▲鼓形修整", 160, False)
        self.assertEqual(cleaned, "鼓形修整")
        self.assertIn("title_prefix_removed", flags)
        self.assertIsNone(reason)

    def test_normal_mixed_terms_are_retained(self):
        for value in ("PID控制", "CAD/CAM技术", "3D打印"):
            with self.subTest(value=value):
                _cleaned, _flags, reason = MODULE.clean_title(value, 160, False)
                self.assertIsNone(reason)

    def test_english_caption_sentence_is_retained_for_model_review(self):
        _cleaned, flags, reason = MODULE.clean_title(
            "C-34: Severe chemosis of the right eye of a goat.", 160, False
        )
        self.assertIsNone(reason)
        self.assertIn("sentence_like_title_requires_model_review", flags)

    def test_inline_dictionary_definition_is_retained(self):
        cleaned, flags, reason = MODULE.clean_title("unsound wood Decayed wood.", 160, False)
        self.assertEqual(cleaned, "unsound wood Decayed wood.")
        self.assertIsNone(reason)
        self.assertIn("sentence_like_title_requires_model_review", flags)

    def test_dictionary_cross_reference_is_retained(self):
        cleaned, flags, reason = MODULE.clean_title(
            "thermoelectric pyrometer See THERMOCOUPLE PYROMETER.", 160, False
        )
        self.assertEqual(cleaned, "thermoelectric pyrometer See THERMOCOUPLE PYROMETER.")
        self.assertIsNone(reason)
        self.assertIn("cross_reference_entry_requires_model_review", flags)

    def test_chinese_angle_bracket_command_is_not_treated_as_html(self):
        cleaned, _flags, reason = MODULE.clean_title("<偏移>", 160, False)
        self.assertEqual(cleaned, "<偏移>")
        self.assertIsNone(reason)

    def test_formula_symbol_is_retained_for_model_review(self):
        _cleaned, flags, reason = MODULE.clean_title("$ v_{cr} $", 160, False)
        self.assertIsNone(reason)
        self.assertIn("formula_or_symbol_title_requires_model_review", flags)

    def test_spaced_numeric_prefix_is_removed(self):
        cleaned, flags, reason = MODULE.clean_title(
            "8 跨孔法波速测试 cross hole method wave velocity test", 160, False
        )
        self.assertEqual(cleaned, "跨孔法波速测试 cross hole method wave velocity test")
        self.assertIn("title_prefix_removed", flags)
        self.assertIsNone(reason)

    def test_latex_toc_leader_and_page_are_removed(self):
        cleaned, flags, reason = MODULE.clean_title(
            r"创新文化\(\ldots \ldots\)117", 160, False
        )
        self.assertEqual(cleaned, "创新文化")
        self.assertIn("latex_toc_leader_and_page_removed", flags)
        self.assertIsNone(reason)

    def test_latex_ellipsis_inside_formula_is_preserved(self):
        cleaned, flags, reason = MODULE.clean_title(
            r"序列 $a_1, a_2, \ldots, a_n$", 160, False
        )
        self.assertEqual(cleaned, r"序列 $a_1, a_2, \ldots, a_n$")
        self.assertNotIn("latex_toc_leader_and_page_removed", flags)
        self.assertIsNone(reason)

    def test_number_before_term_starting_with_section_character(self):
        cleaned, _flags, reason = MODULE.clean_title(
            "11 节理糙度系数 joint roughness coefficient", 160, False
        )
        self.assertEqual(cleaned, "节理糙度系数 joint roughness coefficient")
        self.assertIsNone(reason)

    def test_severely_mixed_but_recoverable_content_is_retained(self):
        value = "碳氮硫三元共渗又叫硫氰共渗,表层有FeS或FeS加α-Fe,并形成Fe_{3}(C,N)。"
        cleaned, flags, reason = MODULE.clean_title(value, 160, False)
        self.assertEqual(cleaned, value)
        self.assertIsNone(reason)
        self.assertTrue(
            {
                "mixed_script_requires_model_review",
                "severely_mixed_script_title_requires_model_review",
            }
            & set(flags)
        )

    def test_navigation_labels_are_rejected(self):
        for value in ("SEE WEB LINKS", "Figure"):
            with self.subTest(value=value):
                _cleaned, _flags, reason = MODULE.clean_title(value, 160, False)
                self.assertEqual(reason, "structural_or_metadata_title")

    def test_recoverable_extraction_labels_are_retained(self):
        for value in ("英译", "含义", "词条 1.4-3"):
            with self.subTest(value=value):
                _cleaned, flags, reason = MODULE.clean_title(value, 160, False)
                self.assertIsNone(reason)
                self.assertIn("extraction_label_requires_model_review", flags)

    def test_valid_second_field_preserves_record(self):
        row = {
            "id": 1,
            "name": "A",
            "knowledge_point": "PID control",
            "definition": "PID control is a control method.",
        }
        status, output = MODULE.classify(row, 1, self.args())
        self.assertEqual(status, "pass")
        self.assertEqual(output["name"], "")
        self.assertEqual(output["knowledge_point"], "PID control")

    def test_no_explicit_definition_is_not_rejected(self):
        row = {"id": 2, "name": "控制系统", "explanation": "随着机器功能越来越复杂。"}
        status, _output = MODULE.classify(row, 1, self.args())
        self.assertEqual(status, "pass")

    def test_csv_input_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "knowledge_point", "name"])
                writer.writeheader()
                writer.writerow({"id": "x1", "knowledge_point": "management", "name": "管理"})
            rows = list(MODULE.iter_records(path, "auto"))
        self.assertEqual(rows[0][1]["id"], "x1")
        self.assertEqual(rows[0][1]["name"], "管理")

    def test_procedural_knowledge_is_retained_for_model_review(self):
        for value in ("计算圆柱齿轮的齿面接触强度", "如何选择滚动轴承", "焊接缺陷分析"):
            with self.subTest(value=value):
                cleaned, flags, reason = MODULE.clean_title(value, 160, False)
                self.assertEqual(cleaned, value)
                self.assertIsNone(reason)
                if value.startswith(("计算", "如何", "分析")):
                    self.assertIn("question_or_procedure_title_requires_model_review", flags)


if __name__ == "__main__":
    unittest.main()
