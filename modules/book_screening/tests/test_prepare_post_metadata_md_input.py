from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from prepare_post_metadata_md_input import prepare_md_input, write_smoke_sample  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class PrepareMdInputTests(unittest.TestCase):
    def test_writes_deterministic_smoke_rows_from_each_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "all.csv"
            output = root / "smoke.csv"
            write_csv(source, [
                {"identifier": "d2", "book_track": "辞海类"},
                {"identifier": "i2", "book_track": "其他重要书籍"},
                {"identifier": "d1", "book_track": "辞海类"},
                {"identifier": "i1", "book_track": "其他重要书籍"},
                {"identifier": "i3", "book_track": "其他重要书籍"},
            ])

            counts = write_smoke_sample(source, output, per_track=2)

            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(["d1", "d2", "i1", "i2"], [row["identifier"] for row in rows])
            self.assertEqual({"辞海类": 2, "其他重要书籍": 2}, counts)

    def test_combines_all_dictionaries_with_only_important_keep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dictionary = root / "dictionary.csv"
            important = root / "important.csv"
            llm = root / "llm.csv"
            output = root / "MD审核试验样本.csv"
            summary = root / "summary.json"
            write_csv(dictionary, [
                {"identifier": "d1", "title": "土木工程辞典", "book_track": ""},
                {"identifier": "shared", "title": "共享辞典", "book_track": ""},
            ])
            write_csv(important, [
                {"identifier": "i1", "title": "结构工程", "book_track": ""},
                {"identifier": "i2", "title": "施工研究", "book_track": ""},
                {"identifier": "shared", "title": "共享普通书", "book_track": ""},
            ])
            write_csv(llm, [
                {"source_row_number": "1", "identifier": "i1", "decision": "KEEP", "subject_fit": "core", "knowledge_extraction_fit": "high", "book_type": "textbook", "reason": "可提取"},
                {"source_row_number": "2", "identifier": "i2", "decision": "DROP", "subject_fit": "weak", "knowledge_extraction_fit": "low", "book_type": "other", "reason": "不适合"},
                {"source_row_number": "3", "identifier": "shared", "decision": "KEEP", "subject_fit": "core", "knowledge_extraction_fit": "high", "book_type": "handbook", "reason": "可提取"},
            ])

            result = prepare_md_input(dictionary, important, llm, output, summary)

            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(["d1", "shared", "i1"], [row["identifier"] for row in rows])
            self.assertEqual(["辞海类", "辞海类", "其他重要书籍"], [row["book_track"] for row in rows])
            self.assertEqual("KEEP", rows[-1]["important_llm_decision"])
            self.assertEqual(2, result["dictionary_rows"])
            self.assertEqual(2, result["important_keep_rows_before_dedup"])
            self.assertEqual(1, result["cross_track_duplicates_removed"])
            self.assertEqual(result, json.loads(summary.read_text(encoding="utf-8")))

    def test_rejects_incomplete_llm_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dictionary = root / "dictionary.csv"
            important = root / "important.csv"
            llm = root / "llm.csv"
            write_csv(dictionary, [{"identifier": "d1", "title": "辞典"}])
            write_csv(important, [
                {"identifier": "i1", "title": "书一"},
                {"identifier": "i2", "title": "书二"},
            ])
            write_csv(llm, [
                {"source_row_number": "1", "identifier": "i1", "decision": "KEEP"},
            ])

            with self.assertRaisesRegex(ValueError, "字段模型结果不完整"):
                prepare_md_input(dictionary, important, llm, root / "out.csv", root / "summary.json")

    def test_rejects_row_identifier_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dictionary = root / "dictionary.csv"
            important = root / "important.csv"
            llm = root / "llm.csv"
            write_csv(dictionary, [{"identifier": "d1", "title": "辞典"}])
            write_csv(important, [{"identifier": "i1", "title": "书一"}])
            write_csv(llm, [{"source_row_number": "1", "identifier": "wrong", "decision": "KEEP"}])

            with self.assertRaisesRegex(ValueError, "identifier 不一致"):
                prepare_md_input(dictionary, important, llm, root / "out.csv", root / "summary.json")


if __name__ == "__main__":
    unittest.main()
