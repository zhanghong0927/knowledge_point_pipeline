from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from screen_mechanical_reference_books import (  # noqa: E402
    classify_title,
    normalize_title_key,
    screen_rows,
)


class ClassifyTitleTests(unittest.TestCase):
    def test_keeps_explicit_chinese_reference_titles(self) -> None:
        cases = {
            "大辞海·机械电气卷": "辞海",
            "英汉机械工程词典": "词典/辞典",
            "中国大百科全书：机械工程": "百科全书",
            "机械制造标准术语大全": "术语/名词/词汇",
            "机械工程名词": "术语/名词/词汇",
        }

        for title, expected_type in cases.items():
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertEqual("strict_keep", result.decision)
                self.assertEqual(expected_type, result.reference_type)

    def test_keeps_explicit_english_reference_titles(self) -> None:
        cases = (
            "A Dictionary of Mechanical Engineering",
            "Mechanical Engineering Encyclopedia",
            "Glossary of Manufacturing Terms",
        )

        for title in cases:
            with self.subTest(title=title):
                self.assertEqual("strict_keep", classify_title(title).decision)

    def test_rejects_cross_word_and_dictionary_learning_false_positives(self) -> None:
        cases = (
            "机械制图典型习题及解答",
            "Dictionary Learning for Machine Fault Diagnosis",
            "Sparse Dictionary Learning in Mechanical Systems",
        )

        for title in cases:
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertEqual("false_positive", result.decision)

    def test_routes_study_aids_and_research_about_terms_to_review(self) -> None:
        cases = (
            "机械设计名词解释习题集",
            "机械工程专业英语词汇学习指导",
            "机械术语翻译研究",
        )

        for title in cases:
            with self.subTest(title=title):
                self.assertEqual("review", classify_title(title).decision)

    def test_ignores_titles_without_reference_book_evidence(self) -> None:
        self.assertEqual("no_hit", classify_title("机械原理与机械设计").decision)


class NormalizeTitleKeyTests(unittest.TestCase):
    def test_normalizes_case_width_whitespace_and_punctuation(self) -> None:
        self.assertEqual(
            normalize_title_key("Ａ Dictionary：Mechanical Engineering"),
            normalize_title_key("a dictionary mechanical-engineering"),
        )

    def test_preserves_edition_text(self) -> None:
        self.assertNotEqual(
            normalize_title_key("机械工程词典 第2版"),
            normalize_title_key("机械工程词典 第3版"),
        )


class ScreenRowsTests(unittest.TestCase):
    def test_splits_decisions_and_deduplicates_strict_candidates(self) -> None:
        rows = [
            {"identifier": "id-1", "title": "英汉机械工程词典"},
            {"identifier": "id-2", "title": "英汉机械工程词典！"},
            {"identifier": "id-3", "title": "机械设计名词解释习题集"},
            {"identifier": "id-4", "title": "机械制图典型习题及解答"},
            {"identifier": "id-5", "title": "机械原理与机械设计"},
            {"identifier": "id-6", "title": "机械工程词典 第2版"},
        ]

        result = screen_rows(rows)

        self.assertEqual(["id-1", "id-6"], [r["identifier"] for r in result.strict_rows])
        self.assertEqual(["id-3"], [r["identifier"] for r in result.review_rows])
        self.assertEqual(["id-4"], [r["identifier"] for r in result.false_positive_rows])
        self.assertEqual(["id-2"], [r["identifier"] for r in result.duplicate_rows])
        self.assertEqual(1, result.no_hit_count)
        self.assertEqual("词典/辞典", result.strict_rows[0]["reference_type"])
        self.assertEqual("strict_keep", result.strict_rows[0]["screen_decision"])

    def test_deduplicates_repeated_identifier_before_title_key(self) -> None:
        rows = [
            {"identifier": "same-id", "title": "机械工程词典"},
            {"identifier": "same-id", "title": "机械制造术语"},
        ]

        result = screen_rows(rows)

        self.assertEqual(1, len(result.strict_rows))
        self.assertEqual(1, len(result.duplicate_rows))
        self.assertEqual("identifier:same-id", result.duplicate_rows[0]["duplicate_of_key"])

    def test_applies_book_level_manual_review_override(self) -> None:
        rows = [{"identifier": "needs-review", "title": "Mechanical Engineering Dictionary"}]
        overrides = {"needs-review": "疑似残卷，完整性待复核。"}

        result = screen_rows(rows, manual_review_overrides=overrides)

        self.assertEqual([], result.strict_rows)
        self.assertEqual(1, len(result.review_rows))
        self.assertEqual("人工逐条复核：疑似残卷，完整性待复核。", result.review_rows[0]["screen_reason"])


if __name__ == "__main__":
    unittest.main()
