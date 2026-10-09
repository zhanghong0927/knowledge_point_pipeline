from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from screen_subject_reference_books import classify_title, run_batch, run_screening  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class GenericScreeningTests(unittest.TestCase):
    def test_routes_popular_science_and_magazine_titles_to_review(self) -> None:
        for title in (
            "军事百科普及读物之陆战之王",
            "宝宝的第一本军事小百科",
            "TANK ENCYCLOPEDIA MAGAZINE",
        ):
            with self.subTest(title=title):
                self.assertEqual("review", classify_title(title).decision)

    def test_single_file_supports_custom_identifier_and_title_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "医学.csv"
            output = root / "output"
            write_csv(
                source,
                [
                    {"book_code": "m1", "book_name": "临床医学词典", "category": "医学"},
                    {"book_code": "m2", "book_name": "内科学", "category": "医学"},
                ],
            )

            summary = run_screening(
                source,
                output,
                subject_name="医学",
                identifier_column="book_code",
                title_column="book_name",
            )

            self.assertEqual(1, summary["strict_keep_rows"])
            strict_path = output / "医学_工具书辞海_严格保留.csv"
            self.assertTrue(strict_path.exists())
            with strict_path.open("r", encoding="utf-8-sig", newline="") as handle:
                strict_rows = list(csv.DictReader(handle))
            self.assertEqual("m1", strict_rows[0]["book_code"])

    def test_batch_processes_each_subject_csv_into_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "subjects"
            output_dir = root / "outputs"
            write_csv(source_dir / "数学.csv", [{"identifier": "a", "title": "数学大辞典"}])
            write_csv(source_dir / "历史学.csv", [{"identifier": "b", "title": "中国历史百科全书"}])

            summary = run_batch(source_dir, output_dir)

            self.assertEqual(2, summary["subject_count"])
            self.assertEqual(2, summary["strict_keep_rows"])
            self.assertTrue((output_dir / "数学" / "数学_工具书辞海_严格保留.csv").exists())
            self.assertTrue((output_dir / "历史学" / "历史学_工具书辞海_严格保留.csv").exists())
            self.assertTrue((output_dir / "批量筛选汇总.json").exists())


if __name__ == "__main__":
    unittest.main()
