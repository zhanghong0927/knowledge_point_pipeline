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

from merge_sharded_recall_outputs import merge_shards  # noqa: E402


def write_csv(path: Path, identifiers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["identifier", "title"])
        writer.writeheader()
        for identifier in identifiers:
            writer.writerow({"identifier": identifier, "title": f"book {identifier}"})


class MergeShardedRecallOutputsTests(unittest.TestCase):
    def test_merge_reconstructs_subject_outputs_and_aggregates_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "subject.json"
            config.write_text(
                json.dumps({"subject_name": "测试学科", "subject_slug": "test_subject"}),
                encoding="utf-8",
            )
            for index, identifiers in enumerate((["a"], ["b"])):
                shard = root / "shards" / f"shard_{index:02d}"
                subject = shard / "test_subject"
                write_csv(subject / "测试学科_宽召回全集.csv", identifiers)
                write_csv(subject / "测试学科_满足后续处理条件.csv", identifiers)
                write_csv(subject / "测试学科_硬规则排除清单.csv", [])
                subject_summary = {
                    "subject_name": "测试学科",
                    "recalled": 1,
                    "strong_recall": 1,
                    "eligible": 1,
                    "hard_rule_excluded": 0,
                    "channel_counts": {"direct_subject": 1},
                    "hard_rule_reason_counts": {},
                }
                (subject / "测试学科_宽召回统计.json").write_text(
                    json.dumps(subject_summary), encoding="utf-8"
                )
                (shard / "全库宽召回汇总.json").write_text(
                    json.dumps({
                        "input_csv": "/source.csv",
                        "source_rows_seen": 2,
                        "processed_rows": 1,
                        "shard_index": index,
                        "shard_count": 2,
                    }),
                    encoding="utf-8",
                )

            report = merge_shards(
                root / "shards", root / "merged", [config], expected_shards=2
            )
            with (root / "merged" / "test_subject" / "测试学科_宽召回全集.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual({"a", "b"}, {row["identifier"] for row in rows})
        self.assertEqual(2, report["source_rows"])
        self.assertEqual(2, report["processed_rows"])
        self.assertEqual(2, report["subjects"]["test_subject"]["recalled"])
        self.assertTrue(report["coverage_ok"])


if __name__ == "__main__":
    unittest.main()
