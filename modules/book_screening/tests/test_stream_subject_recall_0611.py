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

from stream_subject_recall_0611 import (  # noqa: E402
    SubjectRecallConfig,
    load_subject_config,
    match_subject,
    run_streaming_recall,
)


FIELDS = [
    "identifier", "documentpath", "parsed_path", "title", "doi", "page_count",
    "distributionformat", "md5", "filesize", "pdf_detail_type", "laplacian_sharpness",
    "author", "publisher", "publicationyear", "language", "isbns", "batch",
    "low_quality_flag", "subject1", "subject2", "subject3", "content_category",
    "content_genre", "usage_type", "education_stage", "audience_level", "abstract",
    "description", "keywords", "comments", "filesize_md",
]


def config_dict(name: str, slug: str, direct: list[str], strong: list[str]) -> dict:
    return {
        "subject_name": name,
        "subject_slug": slug,
        "boundary": {
            "aliases": direct,
            "direct_subject_labels": direct,
            "adjacent_subject_labels": ["建筑", "医学"],
            "strong_terms": strong,
            "weak_terms": ["地基", "认知", "行为"],
            "cooccurrence_groups": [["混凝土", "结构"], ["mental", "health"]],
            "exclude_phrases": ["计算机数据结构"],
        },
    }


def row(identifier: str, **overrides: str) -> dict[str, str]:
    value = {field: "" for field in FIELDS}
    value.update({
        "identifier": identifier,
        "title": "普通图书",
        "publicationyear": "2021",
        "pdf_detail_type": "非影印版PDF",
        "distributionformat": "PDF",
        "language": "zh",
        "parsed_path": f"oss://pipeline-result/{identifier}.md",
    })
    value.update(overrides)
    return value


class SubjectRecallConfigTests(unittest.TestCase):
    def test_load_config_validates_and_normalizes_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "civil.json"
            path.write_text(
                json.dumps(config_dict("土木工程", "civil_engineering", ["土木"], ["岩土工程"]), ensure_ascii=False),
                encoding="utf-8",
            )
            config = load_subject_config(path)

        self.assertIsInstance(config, SubjectRecallConfig)
        self.assertEqual("土木工程", config.subject_name)
        self.assertEqual(("岩土工程",), config.strong_terms)

    def test_missing_required_boundary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"subject_name":"土木工程","subject_slug":"civil"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "boundary"):
                load_subject_config(path)

    def test_civil_config_covers_known_broad_recall_misses(self) -> None:
        config = load_subject_config(PROJECT_DIR / "configs" / "subject_recall" / "civil_engineering.json")
        titles = [
            "Infrastructure in Africa",
            "Advances in Rock Dynamics and Applications",
            "建设工程领域热点合规问题和风险解析",
            "Municipal Wastewater Management",
            "Proceedings of the 9th National Conference on Wind Engineering",
        ]

        for index, title in enumerate(titles):
            with self.subTest(title=title):
                self.assertTrue(match_subject(row(f"civil-{index}", title=title), config).matched)

    def test_agriculture_and_biomedical_configs_cover_l1_boundaries(self) -> None:
        cases = {
            "agriculture.json": [
                "Dictionary of Crop Science and Plant Breeding",
                "智慧农业与农业机器人",
                "Veterinary Medicine and Animal Health",
                "Aquaculture and Fisheries Science",
            ],
            "biomedical_engineering.json": [
                "Encyclopedia of Biomedical Engineering",
                "Biomedical Imaging and Biophotonics",
                "Brain-Computer Interfaces for Neurorehabilitation",
                "Medical Robotics and Computer-Assisted Surgery",
            ],
            "automation.json": [
                "Modern Control Systems and Industrial Automation",
                "机器人控制理论与智能系统",
            ],
            "electrical_engineering.json": [
                "Power Systems and High Voltage Engineering",
                "电力电子与电气传动",
            ],
            "electronic_information.json": [
                "Microwave Engineering and Signal Processing",
                "通信与信息系统",
            ],
            "hydraulic_engineering.json": [
                "Encyclopedia of Hydrology and Water Resources",
                "水工结构与防洪工程",
            ],
            "integrated_circuits.json": [
                "CMOS Integrated Circuit Design",
                "Semiconductor Devices and IC Fabrication",
            ],
            "ocean_engineering.json": [
                "Offshore Structures and Subsea Engineering",
                "船舶与海洋工程",
            ],
        }
        for file_name, titles in cases.items():
            config = load_subject_config(
                PROJECT_DIR / "configs" / "subject_recall" / file_name
            )
            for index, title in enumerate(titles):
                with self.subTest(config=file_name, title=title):
                    self.assertTrue(
                        match_subject(row(f"{file_name}-{index}", title=title), config).matched
                    )

    def test_new_subject_configs_reject_known_title_ambiguities(self) -> None:
        agriculture = load_subject_config(
            PROJECT_DIR / "configs" / "subject_recall" / "agriculture.json"
        )
        biomedical = load_subject_config(
            PROJECT_DIR / "configs" / "subject_recall" / "biomedical_engineering.json"
        )
        self.assertFalse(
            match_subject(row("ag-bank", title="农业银行经营管理"), agriculture).matched
        )
        self.assertFalse(
            match_subject(row("hospital-finance", title="Hospital Financial Management"), biomedical).matched
        )

    def test_additional_subject_configs_reject_common_ambiguities(self) -> None:
        cases = {
            "automation.json": "Office Automation User Guide",
            "electronic_information.json": "Encyclopedia of Electronic Music",
            "hydraulic_engineering.json": "Hydraulic Fitness Equipment",
            "ocean_engineering.json": "Ocean Travel Guide",
        }
        for file_name, title in cases.items():
            config = load_subject_config(
                PROJECT_DIR / "configs" / "subject_recall" / file_name
            )
            with self.subTest(config=file_name, title=title):
                self.assertFalse(match_subject(row(file_name, title=title), config).matched)


class SubjectMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SubjectRecallConfig.from_dict(
            config_dict("土木工程", "civil_engineering", ["土木", "civil engineering"], ["岩土工程", "geotechnical engineering"])
        )

    def test_direct_subject_label_is_strong_recall(self) -> None:
        result = match_subject(row("1", subject1="土木"), self.config)
        self.assertTrue(result.matched)
        self.assertEqual("strong_recall", result.strength)
        self.assertIn("direct_subject", result.channels)

    def test_strong_title_term_is_strong_recall(self) -> None:
        result = match_subject(row("2", title="岩土工程设计方法"), self.config)
        self.assertTrue(result.matched)
        self.assertEqual("strong_recall", result.strength)
        self.assertIn("title_strong_term", result.channels)

    def test_metadata_strong_term_and_cooccurrence_are_weak_recall(self) -> None:
        metadata = match_subject(row("3", description="本书介绍岩土工程的发展"), self.config)
        paired = match_subject(row("4", abstract="混凝土构件的结构性能"), self.config)
        self.assertEqual("weak_recall", metadata.strength)
        self.assertEqual("weak_recall", paired.strength)

    def test_title_weak_term_is_recalled_but_metadata_weak_term_alone_is_not(self) -> None:
        title_weak = match_subject(row("5", title="地基"), self.config)
        metadata_weak = match_subject(row("5b", description="本书提到地基"), self.config)
        excluded = match_subject(row("6", title="计算机数据结构"), self.config)
        self.assertTrue(title_weak.matched)
        self.assertEqual("weak_recall", title_weak.strength)
        self.assertIn("title_domain_term", title_weak.channels)
        self.assertFalse(metadata_weak.matched)
        self.assertFalse(excluded.matched)

    def test_adjacent_subject_plus_weak_term_is_retained(self) -> None:
        result = match_subject(row("7", subject1="建筑", keywords="地基"), self.config)
        self.assertTrue(result.matched)
        self.assertEqual("weak_recall", result.strength)
        self.assertIn("adjacent_plus_domain", result.channels)


class StreamingRecallTests(unittest.TestCase):
    def test_record_shards_are_disjoint_and_reconstruct_full_result(self) -> None:
        civil = SubjectRecallConfig.from_dict(
            config_dict("土木工程", "civil_engineering", ["土木"], ["岩土工程"])
        )
        rows = [
            row("civil-0", subject1="土木"),
            row("none-1", title="古典文学作品"),
            row("civil-2", title="岩土工程词典"),
            row("civil-3", subject1="土木"),
            row("none-4", title="诗歌选集"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)

            full = run_streaming_recall(source, [civil], root / "full")
            shard0 = run_streaming_recall(
                source, [civil], root / "shard0", shard_index=0, shard_count=2
            )
            shard1 = run_streaming_recall(
                source, [civil], root / "shard1", shard_index=1, shard_count=2
            )

            def recalled_ids(output: Path) -> set[str]:
                path = output / "civil_engineering" / "土木工程_宽召回全集.csv"
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    return {item["identifier"] for item in csv.DictReader(handle)}

            full_ids = recalled_ids(root / "full")
            shard0_ids = recalled_ids(root / "shard0")
            shard1_ids = recalled_ids(root / "shard1")

        self.assertFalse(shard0_ids & shard1_ids)
        self.assertEqual(full_ids, shard0_ids | shard1_ids)
        self.assertEqual(3, shard0["processed_rows"])
        self.assertEqual(2, shard1["processed_rows"])
        self.assertEqual(5, shard0["source_rows_seen"])
        self.assertEqual(5, shard1["source_rows_seen"])
        self.assertEqual(5, full["processed_rows"])

    def test_large_metadata_field_does_not_abort_scan(self) -> None:
        civil = SubjectRecallConfig.from_dict(
            config_dict("土木工程", "civil_engineering", ["土木"], ["岩土工程"])
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            source_row = row("large", subject1="土木", description="x" * 200_000)
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(source_row)

            summary = run_streaming_recall(source, [civil], root / "out")

        self.assertEqual(1, summary["total_rows"])
        self.assertEqual(1, summary["subjects"]["civil_engineering"]["recalled"])

    def test_nul_byte_in_source_does_not_abort_scan(self) -> None:
        civil = SubjectRecallConfig.from_dict(
            config_dict("土木工程", "civil_engineering", ["土木"], ["岩土工程"])
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            source_row = row("nul", title="岩土__NUL_FIXTURE__工程")
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(source_row)

            source.write_bytes(source.read_bytes().replace(b'__NUL_FIXTURE__',b'\x00'))

            summary = run_streaming_recall(source, [civil], root / "out")

        self.assertEqual(1, summary["total_rows"])
        self.assertEqual(1, summary["subjects"]["civil_engineering"]["recalled"])

    def test_one_scan_outputs_each_subject_and_hard_gate_partition(self) -> None:
        civil = SubjectRecallConfig.from_dict(
            config_dict("土木工程", "civil_engineering", ["土木"], ["岩土工程"])
        )
        psychology = SubjectRecallConfig.from_dict(
            config_dict("心理学", "psychology", ["心理学", "psychology"], ["认知心理学"])
        )
        rows = [
            row("civil-ok", subject1="土木"),
            row("civil-old", title="岩土工程词典", publicationyear="1999"),
            row("psych-ok", subject1="心理学"),
            row("none", title="古典文学作品"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)

            summary = run_streaming_recall(source, [civil, psychology], root / "out")

            with (root / "out" / "civil_engineering" / "土木工程_宽召回全集.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                civil_all = list(csv.DictReader(handle))
            with (root / "out" / "civil_engineering" / "土木工程_满足后续处理条件.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                civil_eligible = list(csv.DictReader(handle))
            with (root / "out" / "civil_engineering" / "土木工程_硬规则排除清单.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                civil_excluded = list(csv.DictReader(handle))

        self.assertEqual(4, summary["total_rows"])
        self.assertEqual({"civil-ok", "civil-old"}, {item["identifier"] for item in civil_all})
        self.assertEqual(["civil-ok"], [item["identifier"] for item in civil_eligible])
        self.assertEqual(["civil-old"], [item["identifier"] for item in civil_excluded])
        self.assertIn("publicationyear_before_2000", civil_excluded[0]["metadata_drop_reasons"])
        self.assertEqual(1, summary["subjects"]["psychology"]["recalled"])


if __name__ == "__main__":
    unittest.main()
