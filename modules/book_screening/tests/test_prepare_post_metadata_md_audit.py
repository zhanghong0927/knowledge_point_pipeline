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

from prepare_post_metadata_md_audit import prepare_md_audit_input  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class PreparePostMetadataMdAuditTests(unittest.TestCase):
    def test_launcher_waits_for_metadata_completion_summary(self) -> None:
        launcher = (SCRIPTS_DIR / "run_post_metadata_md_audit.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('metadata_summary_json="${metadata_results_csv%/*}/书目大模型精筛汇总.json"', launcher)
        self.assertIn(
            'while [[ ! -s "$metadata_summary_json" || ! -s "$metadata_results_csv" ]]',
            launcher,
        )

    def test_combines_all_dictionaries_with_only_kept_important_books(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dictionary_csv = root / "dictionary.csv"
            important_csv = root / "important.csv"
            decisions_csv = root / "decisions.csv"
            output_csv = root / "MD审核试验样本.csv"

            write_csv(dictionary_csv, [
                {"identifier": "dict-1", "title": "辞典", "parsed_path": "oss://b/dict-1.md"},
                {"identifier": "shared", "title": "百科", "parsed_path": "oss://b/shared.md"},
            ])
            write_csv(important_csv, [
                {"identifier": "keep-1", "title": "结构工程", "parsed_path": "oss://b/keep-1.md"},
                {"identifier": "drop-1", "title": "无关书", "parsed_path": "oss://b/drop-1.md"},
                {"identifier": "shared", "title": "重复书", "parsed_path": "oss://b/shared.md"},
            ])
            write_csv(decisions_csv, [
                {"source_row_number": "1", "identifier": "keep-1", "decision": "KEEP", "reason": "相关"},
                {"source_row_number": "2", "identifier": "drop-1", "decision": "DROP", "reason": "无关"},
                {"source_row_number": "3", "identifier": "shared", "decision": "KEEP", "reason": "相关"},
            ])

            summary = prepare_md_audit_input(
                dictionary_csv, important_csv, decisions_csv, output_csv
            )

            with output_csv.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(["dict-1", "shared", "keep-1"], [row["identifier"] for row in rows])
            self.assertEqual(["辞海类", "辞海类", "其他重要书籍"], [row["book_track"] for row in rows])
            self.assertEqual("KEEP", rows[-1]["metadata_llm_decision"])
            self.assertEqual(2, summary["dictionary_rows"])
            self.assertEqual(2, summary["important_keep_rows_before_dedup"])
            self.assertEqual(1, summary["cross_track_duplicates_removed"])
            self.assertEqual(3, summary["output_rows"])

    def test_rejects_incomplete_metadata_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dictionary_csv = root / "dictionary.csv"
            important_csv = root / "important.csv"
            decisions_csv = root / "decisions.csv"

            write_csv(dictionary_csv, [{"identifier": "dict-1", "title": "辞典"}])
            write_csv(important_csv, [
                {"identifier": "book-1", "title": "书一"},
                {"identifier": "book-2", "title": "书二"},
            ])
            write_csv(decisions_csv, [
                {"source_row_number": "1", "identifier": "book-1", "decision": "KEEP"},
            ])

            with self.assertRaisesRegex(ValueError, "字段精筛结果不完整"):
                prepare_md_audit_input(
                    dictionary_csv,
                    important_csv,
                    decisions_csv,
                    root / "output.csv",
                )

    def test_rejects_identifier_mismatch_at_source_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dictionary_csv = root / "dictionary.csv"
            important_csv = root / "important.csv"
            decisions_csv = root / "decisions.csv"

            write_csv(dictionary_csv, [{"identifier": "dict-1", "title": "辞典"}])
            write_csv(important_csv, [{"identifier": "book-1", "title": "书一"}])
            write_csv(decisions_csv, [
                {"source_row_number": "1", "identifier": "wrong-id", "decision": "KEEP"},
            ])

            with self.assertRaisesRegex(ValueError, "identifier 不一致"):
                prepare_md_audit_input(
                    dictionary_csv,
                    important_csv,
                    decisions_csv,
                    root / "output.csv",
                )


if __name__ == "__main__":
    unittest.main()
