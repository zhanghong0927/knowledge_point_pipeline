from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from general_book_screening_pipeline import (  # noqa: E402
    _compact_prompt_text,
    analyze_edited_collection_signals,
    analyze_full_text_signals,
    apply_knowledge_extraction_gates,
    apply_final_safety_gates,
    apply_full_text_signal_gates,
    apply_md_hard_gates,
    apply_original_year_gate,
    apply_subject_gates,
    apply_track_structure_gates,
    audit_one,
    build_system_prompt,
    classify_book_track,
    classify_important_book,
    detect_periodical_source,
    flatten_audit,
    load_l1_boundaries,
    load_subject_config,
    materialize_from_progress,
    materialize_results,
    metadata_screen_row,
    normalize_structure_label,
    parse_llm_result,
    parse_oss_uri,
    prepare_audit_text,
    resolve_parsed_path,
    run_md_audit,
    run_metadata_screen,
    distributed_samples,
    select_stratified_pilot,
)


def valid_row(**overrides: str) -> dict[str, str]:
    row = {
        "identifier": "id-1",
        "title": "机械设计基础",
        "publicationyear": "2020",
        "pdf_detail_type": "非影印版PDF",
        "distributionformat": "pdf",
        "language": "zh",
        "parsed_path": "oss://pipeline-result1/a/id-1.md",
        "document_type": "TEXTBOOK",
    }
    row.update(overrides)
    return row


class MetadataRulesTests(unittest.TestCase):
    def test_missing_or_pre_2000_year_is_drop(self) -> None:
        for value, reason in (("", "missing_publicationyear"), ("1999", "publicationyear_before_2000")):
            with self.subTest(value=value):
                result = metadata_screen_row(valid_row(publicationyear=value))
                self.assertEqual("DROP", result.decision)
                self.assertIn(reason, result.drop_reasons)

    def test_file_type_allows_only_non_scanned_pdf_or_blank_detail_epub(self) -> None:
        self.assertEqual("KEEP", metadata_screen_row(valid_row()).decision)
        epub = valid_row(pdf_detail_type="", distributionformat="ePuB")
        self.assertEqual("KEEP", metadata_screen_row(epub).decision)

        for row in (
            valid_row(pdf_detail_type="影印版PDF"),
            valid_row(pdf_detail_type="", distributionformat="pdf"),
            valid_row(pdf_detail_type="Broken"),
        ):
            with self.subTest(row=row):
                result = metadata_screen_row(row)
                self.assertEqual("DROP", result.decision)
                self.assertIn("unsupported_file_type", result.drop_reasons)

    def test_language_and_parsed_path_are_hard_gates(self) -> None:
        self.assertIn(
            "unsupported_metadata_language",
            metadata_screen_row(valid_row(language="de")).drop_reasons,
        )
        self.assertIn(
            "missing_parsed_path",
            metadata_screen_row(valid_row(parsed_path="")).drop_reasons,
        )

    def test_dictionary_track_has_priority(self) -> None:
        row = valid_row(
            title="机械工程百科全书与设计手册",
            document_type="HANDBOOK",
        )
        result = classify_book_track(row)
        self.assertEqual("辞海类", result.track)
        self.assertIn("百科", result.evidence)

    def test_metadata_screen_can_select_all_dictionary_candidates(self) -> None:
        rows = [
            valid_row(identifier="dict-1", title="土木工程术语词典"),
            valid_row(identifier="book-1", title="土木工程设计原理"),
            valid_row(identifier="dict-old", title="岩土工程词典", publicationyear="1990"),
        ]
        fields = list(rows[0])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            summary = run_metadata_screen(
                source,
                root / "output",
                sample_size=1,
                audit_track="辞海类",
                audit_all=True,
            )
            with (root / "output" / "MD审核试验样本.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                pilot = list(csv.DictReader(handle))

        self.assertEqual(["dict-1"], [item["identifier"] for item in pilot])
        self.assertEqual(1, summary["audit_population_rows"])
        self.assertEqual("辞海类", summary["audit_track"])

    def test_dictionary_full_audit_uses_streaming_metadata_path(self) -> None:
        rows = [
            valid_row(identifier="dict-1", title="土木工程术语词典"),
            valid_row(identifier="book-1", title="土木工程设计原理"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with mock.patch(
                "general_book_screening_pipeline.read_csv",
                side_effect=AssertionError("不应整表载入内存"),
            ):
                summary = run_metadata_screen(
                    source,
                    root / "output",
                    audit_track="辞海类",
                    audit_all=True,
                )

        self.assertEqual(2, summary["input_rows"])
        self.assertEqual(1, summary["audit_population_rows"])

    def test_dictionary_content_genre_alone_is_not_title_evidence(self) -> None:
        result = classify_book_track(
            valid_row(title="机械专业参考资料", content_genre="DICTIONARY")
        )
        self.assertEqual("其他重要书籍", result.track)

    def test_dictionary_learning_false_positive_stays_other_track(self) -> None:
        result = classify_book_track(
            valid_row(title="Sparse Dictionary Learning for Fault Diagnosis")
        )
        self.assertEqual("其他重要书籍", result.track)

    def test_non_reference_candidate_goes_to_other_important_books(self) -> None:
        result = classify_book_track(valid_row(title="机械设计基础", document_type="TEXTBOOK"))
        self.assertEqual("其他重要书籍", result.track)

    def test_important_book_screen_has_separate_high_recall_tiers(self) -> None:
        strict = classify_important_book(
            valid_row(content_genre="HIGHER_EDU_TEXTBOOK"), "其他重要书籍"
        )
        review = classify_important_book(
            valid_row(content_genre="SELF_HELP"), "其他重要书籍"
        )
        dictionary = classify_important_book(valid_row(), "辞海类")
        self.assertEqual("strict_keep", strict[0])
        self.assertEqual("review", review[0])
        self.assertEqual("not_applicable", dictionary[0])

    def test_clear_periodical_metadata_is_dropped_from_important_books(self) -> None:
        result = metadata_screen_row(valid_row(
            title="液压与气动",
            publisher="《液压与气动》编辑部",
            abstract="2023年第6期机械工程技术论文",
        ))
        self.assertEqual("DROP", result.decision)
        self.assertIn("periodical_source", result.drop_reasons)

    def test_dictionary_editor_word_alone_is_not_treated_as_periodical(self) -> None:
        result = metadata_screen_row(valid_row(
            title="机械工程词典",
            publisher="机械工程词典编辑部",
        ))
        self.assertEqual("KEEP", result.decision)


class PilotSamplingTests(unittest.TestCase):
    def test_fixed_seed_sample_is_stable_exact_and_unique(self) -> None:
        rows = []
        for index in range(120):
            row = valid_row(
                identifier=f"id-{index:03d}",
                language="zh" if index % 2 else "en",
                title="机械词典" if index % 3 == 0 else "机械设计",
                document_type="DICTIONARY" if index % 3 == 0 else "TEXTBOOK",
            )
            screen = metadata_screen_row(row)
            row.update(screen.as_audit_fields())
            rows.append(row)

        first = select_stratified_pilot(rows, sample_size=37, seed=20260903)
        second = select_stratified_pilot(list(reversed(rows)), sample_size=37, seed=20260903)
        first_ids = [row["identifier"] for row in first]
        second_ids = [row["identifier"] for row in second]
        self.assertEqual(37, len(first_ids))
        self.assertEqual(37, len(set(first_ids)))
        self.assertEqual(first_ids, second_ids)

    def test_small_track_is_fully_included_in_pilot(self) -> None:
        rows = []
        for index in range(120):
            track = "辞海类" if index < 7 else "其他重要书籍"
            rows.append({
                "identifier": f"id-{index:03d}",
                "language": "zh",
                "book_track": track,
                "metadata_stratum": f"zh|{track}|non_scanned_pdf",
            })
        sample = select_stratified_pilot(rows, sample_size=30, seed=20260903)
        selected_dictionary_ids = {
            row["identifier"] for row in sample if row["book_track"] == "辞海类"
        }
        self.assertEqual({f"id-{index:03d}" for index in range(7)}, selected_dictionary_ids)

    def test_reference_title_duplicates_are_excluded_from_pilot_not_hard_gate_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.csv"
            rows = [
                valid_row(identifier="a", title="机械工程词典"),
                valid_row(identifier="b", title="机械工程词典！"),
                valid_row(identifier="c", title="机械设计基础"),
            ]
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            summary = run_metadata_screen(source, root / "output", sample_size=10)
            self.assertEqual(3, summary["metadata_keep"])
            self.assertEqual(1, summary["reference_duplicate_rows"])
            self.assertEqual(1, summary["reference_title_candidates"])
            self.assertEqual(2, summary["md_candidate_rows"])
            self.assertEqual(2, summary["pilot_rows"])
            self.assertTrue(
                (root / "output" / "辞海类" / "初筛" / "进入MD审核0611字段.csv").exists()
            )
            self.assertTrue(
                (root / "output" / "其他重要书籍" / "初筛" / "严格保留0611字段.csv").exists()
            )


class MdAuditTests(unittest.TestCase):
    def test_llm_result_accepts_ordered_string_sample_labels(self) -> None:
        payload = {
            "decision": "REVIEW",
            "dominant_languages": ["zh"],
            "substantial_non_zh_en": False,
            "matched_l1_paths": [],
            "subject_relevance": "medium",
            "knowledge_extraction_suitability": "medium",
            "document_structure": "mixed",
            "dictionary_structure": "mixed",
            "entry_definition_alignment": "medium",
            "continuous_prose_dominant": False,
            "original_publication_year": None,
            "sample_structure_labels": ["dictionary_entries", "unexpected"],
            "sample_knowledge_labels": ["dictionary_entry", "unclear"],
            "ocr_quality": "medium",
            "rule_cleanable": True,
            "summary": "需复核",
            "evidence": [{"sample_no": 2, "anchor": "片段", "issue": "结构不明确"}],
            "confidence": 0.6,
        }

        parsed = parse_llm_result(
            json.dumps(payload, ensure_ascii=False),
            set(),
            expected_sample_count=2,
            track="辞海类",
        )

        self.assertEqual(
            [
                {"sample_no": 1, "structure": "compact_entries"},
                {"sample_no": 2, "structure": "unclear"},
            ],
            parsed["sample_structure_labels"],
        )
        self.assertEqual(
            [
                {"sample_no": 1, "knowledge_type": "dictionary_entry"},
                {"sample_no": 2, "knowledge_type": "unclear"},
            ],
            parsed["sample_knowledge_labels"],
        )

    def test_md_audit_retries_unresolved_md_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "input.csv"
            row = valid_row(identifier="retry-md-1")
            with input_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)

            progress_dir = root / "output" / "模型原始结果"
            progress_dir.mkdir(parents=True)
            progress_path = progress_dir / "progress.jsonl"
            progress_path.write_text(
                json.dumps({
                    "identifier": "retry-md-1",
                    "audit_status": "unresolved_md_path",
                    "error": "temporary OSS fetch failure",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            completed = {
                "identifier": "retry-md-1",
                "audit_status": "completed",
                "ok": True,
                "result": {"decision": "PASS"},
            }
            with mock.patch(
                "general_book_screening_pipeline.audit_one",
                return_value=completed,
            ) as audit:
                summary = run_md_audit(
                    input_csv,
                    None,
                    root / "output",
                    "机械工程",
                    workers=1,
                )

            self.assertEqual(1, summary["processed_now"])
            self.assertEqual(1, audit.call_count)
            self.assertEqual({"completed": 1}, summary["status_counts"])

    def test_compact_prompt_text_compresses_repeated_symbols(self) -> None:
        compacted = _compact_prompt_text("正文" + "$" * 500 + "结尾")
        self.assertLess(len(compacted), 100)
        self.assertIn("连续500字符已压缩", compacted)

    def test_catalog_table_structure_alias_is_structured_exposition(self) -> None:
        self.assertEqual(
            "structured_exposition",
            normalize_structure_label("catalog_table", "辞海类"),
        )

    def test_periodical_md_is_excluded_before_llm_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md = root / "journal.md"
            md.write_text(
                "ISSN 1000-3762\n2020年第40卷第06期 总第264期\n月刊\n",
                encoding="utf-8",
            )
            row = valid_row(
                identifier="journal-1",
                title="液压气动与密封",
                parsed_path=str(md),
                book_track="其他重要书籍",
            )
            result = audit_one(
                row,
                subject="机械工程",
                l1_nodes=[{
                    "name": "机械设计",
                    "path": "机械设计",
                    "node_definition": "机械结构设计",
                    "node_boundary": "机械产品与零部件设计",
                }],
                mappings={},
                cache_dir=root / "cache",
                ossutil_config=None,
                source_oss_endpoint=None,
                api_url="http://127.0.0.1:1/v1/chat/completions",
                model="unused",
                sample_count=16,
                chunk_chars=1200,
                timeout=1,
                retries=0,
                max_tokens=100,
            )
            self.assertEqual("periodical_excluded", result["audit_status"])
            self.assertEqual("DROP", result["result"]["decision"])

    def test_issn_and_issue_markers_identify_periodical_md(self) -> None:
        signals = detect_periodical_source(
            valid_row(title="BEARING", publisher="洛阳轴承研究所有限公司"),
            "ISSN 1000-3762\n2019年第8期 总第477期\n月刊\n目次",
        )
        self.assertIn("md_issn_issue", signals)

    def test_chinese_issue_and_total_issue_identify_periodical_without_issn(self) -> None:
        signals = detect_periodical_source(
            valid_row(title="机械制造文摘——焊接分册"),
            "机械制造文摘——焊接分册\n2019年第6期(总第284期)\n专题研究\n",
        )
        self.assertIn("md_chinese_issue", signals)

    def test_chinese_issue_volume_and_total_issue_identify_periodical_without_issn(self) -> None:
        signals = detect_periodical_source(
            valid_row(title="机械强度"),
            "机械强度\n2021年第3期 第43卷 总第215期\n实验研究\n",
        )
        self.assertIn("md_chinese_issue", signals)

    def test_isbn_and_chapter_markers_do_not_identify_periodical_md(self) -> None:
        signals = detect_periodical_source(
            valid_row(title="机械设计基础", publisher="机械工业出版社"),
            "ISBN 978-7-111-00000-0\n第3章 齿轮传动\n本章介绍齿轮设计。",
        )
        self.assertEqual((), signals)

    def test_materialization_overrides_existing_pass_for_periodical_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "journal.md"
            md.write_text(
                "ISSN 1000-3762\n2020年第40卷第06期 总第264期\n月刊\n",
                encoding="utf-8",
            )
            row = valid_row(
                identifier="journal-1",
                title="液压气动与密封",
                book_track="其他重要书籍",
            )
            progress = {
                "journal-1": {
                    "identifier": "journal-1",
                    "ok": True,
                    "audit_status": "completed",
                    "resolved_md_path": str(md),
                    "result": {
                        "decision": "PASS",
                        "matched_l1_paths": ["机械设计"],
                        "dominant_languages": ["zh"],
                        "subject_relevance": "high",
                        "knowledge_extraction_suitability": "high",
                        "document_structure": "textbook",
                        "ocr_quality": "high",
                        "rule_cleanable": True,
                        "summary": "正文可用",
                        "evidence": [],
                    },
                }
            }
            flattened = flatten_audit(row, progress)
            self.assertEqual("DROP", flattened["final_decision"])
            self.assertEqual("periodical_excluded", flattened["audit_status"])
            self.assertEqual(True, flattened["periodical_detected"])
            self.assertIn("md_issn_issue", flattened["periodical_evidence"])

    def test_full_audit_materialization_includes_metadata_drop_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            keep = valid_row(identifier="keep-1", book_track="其他重要书籍")
            keep.update(metadata_decision="KEEP", metadata_drop_reasons="")
            dropped = valid_row(identifier="drop-1", book_track="其他重要书籍")
            dropped.update(
                metadata_decision="DROP",
                metadata_drop_reasons="periodical_source",
            )
            fields = list(dict.fromkeys([*keep, *dropped]))
            for path, rows in (
                (output / "书目初筛结果.csv", [keep, dropped]),
                (output / "MD审核试验样本.csv", [keep]),
            ):
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            progress_dir = output / "模型原始结果"
            progress_dir.mkdir()
            progress_item = {
                "identifier": "keep-1",
                "ok": True,
                "audit_status": "completed",
                "resolved_md_path": "",
                "result": {
                    "decision": "PASS",
                    "matched_l1_paths": [],
                    "dominant_languages": ["zh"],
                    "substantial_non_zh_en": False,
                    "subject_relevance": "high",
                    "knowledge_extraction_suitability": "high",
                    "document_structure": "textbook",
                    "ocr_quality": "high",
                    "rule_cleanable": True,
                    "summary": "正文可用",
                    "evidence": [],
                },
            }
            (progress_dir / "progress.jsonl").write_text(
                json.dumps(progress_item, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            summary = materialize_from_progress(output / "MD审核试验样本.csv", output)
            self.assertEqual(2, summary["total"])
            with (output / "最终审核结果.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = {row["identifier"]: row for row in csv.DictReader(handle)}
            self.assertEqual("PASS", rows["keep-1"]["final_decision"])
            self.assertEqual("DROP", rows["drop-1"]["final_decision"])
            self.assertEqual("metadata_drop", rows["drop-1"]["audit_status"])

    def test_subject_gate_prevents_weak_or_unmapped_pass(self) -> None:
        base = {
            "decision": "PASS",
            "subject_relevance": "medium",
            "knowledge_extraction_suitability": "high",
            "matched_l1_paths": [],
            "summary": "模型总评通过",
            "evidence": [],
        }
        self.assertEqual("REVIEW", apply_subject_gates(dict(base))["decision"])
        low = dict(base, subject_relevance="low", matched_l1_paths=["机械设计"])
        self.assertEqual("DROP", apply_subject_gates(low)["decision"])

    def test_subject_gate_without_l1_keeps_highly_relevant_extractable_book(self) -> None:
        result = {
            "decision": "PASS",
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "matched_l1_paths": [],
            "summary": "学科相关且正文可抽取",
            "evidence": [],
        }
        gated = apply_subject_gates(result, require_l1=False)
        self.assertEqual("PASS", gated["decision"])

    def test_prompt_without_l1_uses_subject_scope_only(self) -> None:
        prompt = build_system_prompt("辞海类", "土木工程", [])
        self.assertIn("未提供L1", prompt)
        self.assertIn("仅判断是否属于土木工程领域", prompt)

    def test_prompt_without_l1_uses_explicit_subject_boundary(self) -> None:
        prompt = build_system_prompt(
            "其他重要书籍",
            "土木工程",
            [],
            {
                "core_scope": "结构、岩土、桥梁与隧道工程",
                "accepted_adjacent": "建筑材料与工程管理",
                "excluded_scope": "室内装饰与纯计算机编程",
            },
        )
        self.assertIn("结构、岩土、桥梁与隧道工程", prompt)
        self.assertIn("建筑材料与工程管理", prompt)
        self.assertIn("室内装饰与纯计算机编程", prompt)
        self.assertIn("不因 matched_l1_paths 为空而降级", prompt)

    def test_unknown_structure_label_uses_track_specific_conservative_fallback(self) -> None:
        self.assertEqual(
            "structured_exposition",
            normalize_structure_label("magazine_article", "其他重要书籍"),
        )
        self.assertEqual(
            "unclear",
            normalize_structure_label("magazine_article", "辞海类"),
        )

    def test_samples_include_nearest_markdown_heading_context(self) -> None:
        text = "# 词条A\n" + ("这是释义。" * 300) + "\n# 词条B\n" + ("另一释义。" * 300)
        samples = distributed_samples(text, count=4, chunk_chars=300)
        self.assertTrue(any(item.get("heading_context") == "词条A" for item in samples))
        self.assertTrue(any(item.get("heading_context") == "词条B" for item in samples))

    def test_prepare_audit_text_removes_embedded_base64_but_keeps_surrounding_text(self) -> None:
        text = (
            "# 钳工实训\n锯削是钳工基本操作。\n"
            '<img src="data:image/jpeg;base64,' + ("A" * 500) + '" width="80" />\n'
            "锉削用于修整工件表面。"
        )
        cleaned = prepare_audit_text(text)
        self.assertIn("锯削是钳工基本操作", cleaned)
        self.assertIn("锉削用于修整工件表面", cleaned)
        self.assertIn("EMBEDDED_IMAGE_REMOVED", cleaned)
        self.assertNotIn("A" * 100, cleaned)

    def test_prepare_audit_text_removes_html_escaped_embedded_base64(self) -> None:
        text = (
            "锯削前检查锯条。\n"
            "&lt;img src=&quot;data:image/jpeg;base64," + ("B" * 500) + "&quot; width=&quot;80&quot; /&gt;\n"
            "锯削后清理工件。"
        )
        cleaned = prepare_audit_text(text)
        self.assertIn("锯削前检查锯条", cleaned)
        self.assertIn("锯削后清理工件", cleaned)
        self.assertIn("EMBEDDED_IMAGE_REMOVED", cleaned)
        self.assertNotIn("B" * 100, cleaned)

    def test_loads_l1_array_and_rejects_missing_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.json"
            good.write_text(
                json.dumps([{
                    "name": "机械设计",
                    "path": "机械设计",
                    "node_definition": "机械结构设计",
                    "node_boundary": "机械产品与零部件设计",
                }], ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(1, len(load_l1_boundaries(good)))
            bad = root / "bad.json"
            bad.write_text(json.dumps([{"name": "机械设计"}], ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_l1_boundaries(bad)

    def test_loads_subject_boundary_with_optional_embedded_l1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "civil.json"
            path.write_text(
                json.dumps({
                    "subject_name": "土木工程",
                    "boundary": {
                        "core_scope": "结构、岩土与交通基础设施",
                        "accepted_adjacent": ["建筑材料", "工程管理"],
                        "excluded_scope": ["纯软件开发"],
                    },
                    "l1_nodes": [{
                        "name": "结构工程",
                        "path": "土木工程/结构工程",
                        "node_definition": "研究工程结构的分析与设计",
                        "node_boundary": "包含结构力学、抗震与结构设计",
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            config = load_subject_config(path)

        self.assertEqual("土木工程", config["subject_name"])
        self.assertIn("建筑材料", config["boundary"]["accepted_adjacent"])
        self.assertEqual("土木工程/结构工程", config["l1_nodes"][0]["path"])

    def test_subject_config_requires_explicit_core_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text(
                json.dumps({"subject_name": "土木工程", "boundary": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "核心范围"):
                load_subject_config(path)

    def test_resolves_oss_uri_via_prefix_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "ocr_parse" / "batch" / "id.md"
            target.parent.mkdir(parents=True)
            target.write_text("text", encoding="utf-8")
            resolved = resolve_parsed_path(
                "oss://pipeline-result1/ocr_parse/batch/id.md",
                {"oss://pipeline-result1/": root},
            )
            self.assertEqual(target.resolve(), resolved)

    def test_unresolved_path_is_none(self) -> None:
        self.assertIsNone(resolve_parsed_path("oss://pipeline-result1/missing.md", {}))

    def test_parses_oss_uri(self) -> None:
        self.assertEqual(
            ("pipeline-result1", "ocr_parse/batch/id.md"),
            parse_oss_uri("oss://pipeline-result1/ocr_parse/batch/id.md"),
        )
        with self.assertRaises(ValueError):
            parse_oss_uri("oss://pipeline-result1")

    def test_traditional_or_non_zh_en_body_forces_drop(self) -> None:
        base = {"decision": "PASS", "summary": "可用", "problems": []}
        traditional = apply_md_hard_gates(
            dict(base), traditional_chinese_body=True, dominant_languages=["zh"]
        )
        self.assertEqual("DROP", traditional["decision"])
        foreign = apply_md_hard_gates(
            dict(base), traditional_chinese_body=False, dominant_languages=["de", "en"]
        )
        self.assertEqual("DROP", foreign["decision"])
        omitted_language = apply_md_hard_gates(
            dict(base),
            traditional_chinese_body=False,
            dominant_languages=["en"],
            substantial_non_zh_en=True,
        )
        self.assertEqual("DROP", omitted_language["decision"])

    def test_prompt_has_track_specific_acceptance_rules_and_l1(self) -> None:
        l1 = [{
            "name": "制造工程",
            "path": "制造工程",
            "node_definition": "制造过程",
            "node_boundary": "加工与生产系统",
        }]
        dictionary_prompt = build_system_prompt("辞海类", "机械工程", l1)
        important_prompt = build_system_prompt("其他重要书籍", "机械工程", l1)
        self.assertIn("双语词条对照", dictionary_prompt)
        self.assertIn("百科长释义", dictionary_prompt)
        self.assertIn("不要求辞海词条结构", important_prompt)
        self.assertIn("无需重建论文上下文", important_prompt)
        self.assertIn("独立研究论文或会议论文", important_prompt)
        self.assertIn("不能仅因文档标注为博士论文", important_prompt)
        self.assertIn("原始出版年份早于 2000 年", important_prompt)
        self.assertIn("original_publication_year", important_prompt)
        self.assertIn("制造工程", important_prompt)
        self.assertIn("sample_structure_labels", dictionary_prompt)
        self.assertIn("sample_knowledge_labels", important_prompt)
        self.assertIn("product_specific_procedure", important_prompt)
        self.assertIn("edited_research_collection", important_prompt)
        self.assertIn("structural_signals", important_prompt)

    def test_edited_collection_signals_require_combined_evidence(self) -> None:
        edited_text = """
        EDITED BY CORNELIUS LEONDES
        Contributors to this volume
        Chapter 1 Manufacturing Systems\nAlice Smith
        Chapter 2 Production Design\nBob Jones
        Chapter 3 Control Methods\nCarol Lee
        Chapter 4 Factory Planning\nDavid Wu
        Chapter 5 Validation\nEric Chen
        # References\n# References\n# References\n# References
        """
        signals = analyze_edited_collection_signals(edited_text)
        self.assertTrue(signals["edited_collection_candidate"])
        self.assertGreaterEqual(signals["reference_section_count"], 3)

        ordinary_handbook = """
        Edited by Jane Doe
        # Chapter 1 Fundamentals
        This handbook explains the principles of pumps and valves.
        # Chapter 2 Design
        This chapter systematically presents design equations.
        """
        self.assertFalse(
            analyze_edited_collection_signals(ordinary_handbook)[
                "edited_collection_candidate"
            ]
        )

    def test_full_text_signals_find_explicit_original_publication_year(self) -> None:
        text = """
        Copyright 2018 Chinese edition.
        Originally published in Japan in 1996 by Asakura Publishing Company.
        # Chapter 1
        Vehicle ergonomics principles.
        """
        signals = analyze_full_text_signals(text, valid_row(title="汽车人机工程学技术"))
        self.assertEqual(1996, signals["original_publication_year"])
        self.assertIn("original_publication_year_before_2000", signals["hard_drop_reasons"])
        self.assertTrue(any(item["line"] == 3 for item in signals["targeted_evidence"]))

    def test_full_text_year_does_not_use_reference_years(self) -> None:
        text = """
        # References
        Smith (1984). Mechanical systems.
        Jones (1996). Control theory.
        """
        signals = analyze_full_text_signals(text, valid_row())
        self.assertIsNone(signals["original_publication_year"])

    def test_full_text_year_ignores_publication_history_inside_body(self) -> None:
        text = "\n".join(
            ["Systematic textbook content."] * 900
            + ["The standard was first published in 1964 and revised in 2003."]
        )
        signals = analyze_full_text_signals(text, valid_row())
        self.assertIsNone(signals["original_publication_year"])

    def test_full_text_year_ignores_standard_history_in_frontmatter(self) -> None:
        text = """
        Power Piping Guide
        The ASME B31.3 code was first published in 1959.
        This edition of the guide was published in 2013.
        """
        signals = analyze_full_text_signals(text, valid_row())
        self.assertIsNone(signals["original_publication_year"])

    def test_full_text_year_ignores_magazine_history_in_frontmatter(self) -> None:
        text = """
        Predictive Maintenance Course
        Vibrations Magazine was first published in 1985.
        This training book was published in 2002.
        """
        signals = analyze_full_text_signals(text, valid_row())
        self.assertIsNone(signals["original_publication_year"])

    def test_full_text_signals_detect_exam_dominance_with_exam_context(self) -> None:
        text = (
            "Solved Problems - 1600+\nPractice Questions - 6000+\n"
            + "\n".join(f"Solution: answer {index}" for index in range(80))
        )
        signals = analyze_full_text_signals(
            text,
            valid_row(title="GATE Mechanical Engineering", content_genre="EXAM_PREP"),
        )
        self.assertTrue(signals["exam_dominated"])
        self.assertIn("exam_question_answer_dominated", signals["hard_drop_reasons"])

    def test_many_solutions_without_exam_context_are_not_hard_dropped(self) -> None:
        text = "\n".join(f"Solution: industrial solution {index}" for index in range(80))
        signals = analyze_full_text_signals(text, valid_row(title="Industrial Solutions Handbook"))
        self.assertFalse(signals["exam_dominated"])

    def test_full_text_signals_detect_short_single_paper(self) -> None:
        text = """
        # ABSTRACT
        This paper specifies washer selection for bolted joints.
        Proceedings of the ASME Design Engineering Conference.
        # 1 Introduction
        The experiment evaluates washer hardness.
        # REFERENCES
        Smith, A. Washer design.
        """
        signals = analyze_full_text_signals(text, valid_row(title="Washer Usage Guidance"))
        self.assertTrue(signals["single_article_candidate"])
        self.assertIn("single_article_or_conference_paper", signals["hard_drop_reasons"])

    def test_full_text_signals_detect_independent_research_chapter_collection(self) -> None:
        chapters = []
        for index in range(1, 6):
            chapters.append(
                f"Chapter {index}. Reliability Study {index}\n"
                f"ALICE AUTHOR and BOB AUTHOR\n"
                f"## {index}.8. Conclusion\n"
                f"## {index}.9. References\n"
            )
        text = "Edited by Jane Editor\nList of Authors\n" + "\n".join(chapters)
        signals = analyze_full_text_signals(text, valid_row(title="Reliability Studies"))
        self.assertTrue(signals["edited_collection_candidate"])
        self.assertEqual("high", signals["edited_collection_confidence"])

    def test_collection_signal_requires_model_confirmation_for_drop(self) -> None:
        signals = {
            "hard_drop_reasons": [],
            "edited_collection_candidate": True,
            "edited_collection_confidence": "high",
            "product_specific_candidate": False,
            "targeted_evidence": [],
        }
        row = {"title": "Welding Handbook", "book_track": "其他重要书籍"}
        unconfirmed = apply_full_text_signal_gates(
            {
                "decision": "PASS",
                "document_structure": "handbook",
                "summary": "系统性手册",
                "evidence": [],
            },
            row,
            signals,
        )
        self.assertEqual("REVIEW", unconfirmed["decision"])

        confirmed = apply_full_text_signal_gates(
            {
                "decision": "PASS",
                "document_structure": "edited_research_collection",
                "summary": "研究章节合集",
                "evidence": [],
            },
            row,
            signals,
        )
        self.assertEqual("DROP", confirmed["decision"])

    def test_confirmed_explicit_collection_reason_is_not_duplicated(self) -> None:
        gated = apply_full_text_signal_gates(
            {
                "decision": "PASS",
                "document_structure": "edited_research_collection",
                "summary": "研究章节合集",
                "evidence": [],
            },
            {"title": "Mechanical Systems Handbook", "book_track": "其他重要书籍"},
            {
                "hard_drop_reasons": ["independent_research_chapter_collection"],
                "edited_collection_candidate": True,
                "edited_collection_confidence": "high",
                "targeted_evidence": [],
            },
        )
        self.assertEqual(
            "independent_research_chapter_collection",
            gated["full_text_gate_reason"],
        )

    def test_systematic_textbook_with_chapter_references_is_not_collection_risk(self) -> None:
        chapters = []
        for index in range(1, 8):
            chapters.append(
                f"# Chapter {index} Manufacturing Principle\n"
                f"This chapter systematically explains theory {index}.\n"
                f"## {index}.8 Conclusion\n"
                f"## {index}.9 References\n"
            )
        text = "Edited by Jane Doe\n" + "\n".join(chapters)
        signals = analyze_full_text_signals(text, valid_row(title="Manufacturing Textbook"))
        self.assertFalse(signals["edited_collection_candidate"])
        self.assertEqual("low", signals["edited_collection_confidence"])

    def test_full_text_signals_detect_explicit_selected_topic_collection(self) -> None:
        text = (
            "Edited by Jane Editor. Four section editors recruited authors. "
            "This collection of selected topics contains individually important contributions.\n"
            + "\n".join("## References" for _ in range(8))
        )
        signals = analyze_full_text_signals(text, valid_row(title="Mechanical Systems Design Handbook"))
        self.assertEqual("high", signals["edited_collection_confidence"])
        self.assertIn(
            "independent_research_chapter_collection",
            signals["hard_drop_reasons"],
        )

    def test_product_brand_dominance_is_review_risk_not_hard_drop(self) -> None:
        text = "\n".join(
            f"LEGO beam {index}: assemble the LEGO pins and gears step by step."
            for index in range(80)
        )
        signals = analyze_full_text_signals(
            text,
            valid_row(
                title="THE UNOFFICIAL LEGO TECHNIC BUILDER'S GUIDE",
                content_genre="POPULAR_SCIENCE",
            ),
        )
        self.assertTrue(signals["product_specific_candidate"])
        self.assertNotIn("product_specific_candidate", signals["hard_drop_reasons"])

    def test_full_text_signal_fusion_blocks_false_passes(self) -> None:
        base = {"decision": "PASS", "summary": "模型认为可通过", "evidence": []}
        row = {"title": "Mechanical Engineering", "book_track": "其他重要书籍"}
        cases = (
            ({"hard_drop_reasons": ["original_publication_year_before_2000"], "targeted_evidence": []}, "DROP"),
            ({"hard_drop_reasons": ["exam_question_answer_dominated"], "targeted_evidence": []}, "DROP"),
            ({"hard_drop_reasons": ["single_article_or_conference_paper"], "targeted_evidence": []}, "DROP"),
            ({"hard_drop_reasons": ["independent_research_chapter_collection"], "targeted_evidence": []}, "DROP"),
            ({"hard_drop_reasons": [], "product_specific_candidate": True, "targeted_evidence": []}, "REVIEW"),
            ({"hard_drop_reasons": [], "product_specific_candidate": False, "targeted_evidence": []}, "PASS"),
        )
        for signals, expected in cases:
            with self.subTest(signals=signals):
                gated = apply_full_text_signal_gates(dict(base), row, signals)
                self.assertEqual(expected, gated["decision"])

    def test_full_text_hard_gate_replaces_contradictory_model_summary(self) -> None:
        gated = apply_full_text_signal_gates(
            {
                "decision": "PASS",
                "summary": "正文以知识讲解为主，故PASS。",
                "evidence": [],
            },
            {"title": "GATE Mechanical Engineering", "book_track": "其他重要书籍"},
            {
                "hard_drop_reasons": ["exam_question_answer_dominated"],
                "targeted_evidence": [],
            },
        )
        self.assertEqual("DROP", gated["decision"])
        self.assertNotIn("故PASS", gated["summary"])
        self.assertIn("题目与答案", gated["summary"])

    def test_prompt_requires_reconciling_full_text_signals(self) -> None:
        prompt = build_system_prompt("其他重要书籍", "机械工程", [])
        self.assertIn("full_text_signals", prompt)
        self.assertIn("targeted_evidence", prompt)
        self.assertIn("全文高风险信号", prompt)

    def test_product_specific_procedure_dominant_is_review(self) -> None:
        result = {
            "decision": "PASS",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "product_specific_procedure" if i <= 12 else "general_knowledge"}
                for i in range(1, 17)
            ],
            "summary": "专业维修手册",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("REVIEW", gated["decision"])
        self.assertTrue(gated["decision_adjusted_by_knowledge_gate"])

    def test_exercise_question_dominant_is_drop(self) -> None:
        result = {
            "decision": "PASS",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "exercise_question" if i <= 13 else "frontmatter_index"}
                for i in range(1, 17)
            ],
            "summary": "习题集",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("DROP", gated["decision"])

    def test_software_dominant_review_is_not_forced_to_drop(self) -> None:
        result = {
            "decision": "REVIEW",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "software_operation" if i <= 15 else "frontmatter_index"}
                for i in range(1, 17)
            ],
            "summary": "软件教材，可能包含理论说明",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("REVIEW", gated["decision"])

    def test_final_gate_recovers_subject_relevant_software_textbook_to_review(self) -> None:
        result = {
            "decision": "DROP",
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "low",
            "document_structure": "textbook",
            "original_publication_year": 2015,
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "software_operation" if i <= 13 else "frontmatter_index"}
                for i in range(1, 17)
            ],
            "summary": "软件操作占优",
            "evidence": [],
        }
        gated = apply_final_safety_gates(
            result, {"title": "CAD工程绘图教程", "book_track": "其他重要书籍"}
        )
        self.assertEqual("REVIEW", gated["decision"])

    def test_final_gate_drops_non_zh_en_bilingual_dictionary_title(self) -> None:
        result = {
            "decision": "PASS",
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "document_structure": "dictionary_entries",
            "original_publication_year": 2008,
            "sample_knowledge_labels": [],
            "summary": "词典结构清晰",
            "evidence": [],
        }
        gated = apply_final_safety_gates(
            result, {"title": "德汉汽车工程词典", "book_track": "辞海类"}
        )
        self.assertEqual("DROP", gated["decision"])
        self.assertIn("非中英文", gated["summary"])

    def test_strong_general_knowledge_promotes_review_to_pass(self) -> None:
        result = {
            "decision": "REVIEW",
            "subject_relevance": "high",
            "ocr_quality": "high",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "general_knowledge" if i <= 13 else "frontmatter_index"}
                for i in range(1, 17)
            ],
            "summary": "模型偏谨慎",
            "evidence": [{"sample_no": 1, "anchor": "正文", "issue": "待复核"}],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("PASS", gated["decision"])

    def test_borderline_specific_content_demotes_pass_to_review(self) -> None:
        types = (
            ["general_knowledge"] * 8
            + ["software_operation"] * 5
            + ["frontmatter_index"] * 3
        )
        result = {
            "decision": "PASS",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": value}
                for i, value in enumerate(types, 1)
            ],
            "summary": "软件教程",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("REVIEW", gated["decision"])

    def test_pass_standard_keeps_technical_tables_as_reusable_context(self) -> None:
        types = ["general_knowledge"] * 2 + ["catalog_table"] * 12 + ["frontmatter_index"] * 2
        result = {
            "decision": "PASS",
            "document_structure": "standard",
            "subject_relevance": "high",
            "ocr_quality": "high",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": value}
                for i, value in enumerate(types, 1)
            ],
            "summary": "技术标准中的规格与参数表可直接抽取",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("PASS", gated["decision"])

    def test_pass_textbook_is_not_demoted_for_small_exercise_share(self) -> None:
        types = ["general_knowledge"] * 11 + ["exercise_question"] * 3 + ["frontmatter_index"] * 2
        result = {
            "decision": "PASS",
            "document_structure": "textbook",
            "subject_relevance": "high",
            "ocr_quality": "high",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": value}
                for i, value in enumerate(types, 1)
            ],
            "summary": "教材正文以知识讲解为主",
            "evidence": [],
        }
        gated = apply_knowledge_extraction_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("PASS", gated["decision"])

    def test_edited_research_collection_is_dropped_by_final_gate(self) -> None:
        result = {
            "decision": "PASS",
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "document_structure": "edited_research_collection",
            "original_publication_year": 2015,
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "general_knowledge"}
                for i in range(1, 17)
            ],
            "edited_collection_signals": {"edited_collection_candidate": True},
            "summary": "虽为多作者编写，但并非独立论文拼接",
            "evidence": [],
        }
        gated = apply_final_safety_gates(
            result, {"title": "Manufacturing Research", "book_track": "其他重要书籍"}
        )
        self.assertEqual("DROP", gated["decision"])
        self.assertNotIn("并非独立论文拼接", gated["summary"])
        summary = gated["summary"]
        gated = apply_final_safety_gates(
            gated, {"title": "Manufacturing Research", "book_track": "其他重要书籍"}
        )
        self.assertEqual(summary, gated["summary"])

    def test_unconfirmed_edited_collection_candidate_is_reviewed_not_dropped(self) -> None:
        result = {
            "decision": "PASS",
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "document_structure": "handbook",
            "original_publication_year": 2015,
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "general_knowledge"}
                for i in range(1, 17)
            ],
            "edited_collection_signals": {"edited_collection_candidate": True},
            "summary": "有合集结构信号但尚未确认是论文合集",
            "evidence": [],
        }
        gated = apply_final_safety_gates(
            result, {"title": "Engineering Handbook", "book_track": "其他重要书籍"}
        )
        self.assertEqual("REVIEW", gated["decision"])

    def test_dictionary_is_not_changed_by_important_book_knowledge_gate(self) -> None:
        result = {
            "decision": "PASS",
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "dictionary_entry"}
                for i in range(1, 17)
            ],
        }
        gated = apply_knowledge_extraction_gates(
            result, "辞海类", expected_sample_count=16
        )
        self.assertEqual("PASS", gated["decision"])
        self.assertFalse(gated["decision_adjusted_by_knowledge_gate"])

    def test_llm_result_requires_complete_knowledge_labels(self) -> None:
        payload = {
            "decision": "PASS",
            "dominant_languages": ["zh"],
            "substantial_non_zh_en": False,
            "matched_l1_paths": ["机械设计"],
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "document_structure": "textbook",
            "dictionary_structure": "not_applicable",
            "entry_definition_alignment": "not_applicable",
            "continuous_prose_dominant": False,
            "original_publication_year": 2020,
            "sample_structure_labels": [
                {"sample_no": i, "structure": "structured_exposition"}
                for i in range(1, 5)
            ],
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "general_knowledge"}
                for i in range(1, 5)
            ],
            "ocr_quality": "high",
            "rule_cleanable": True,
            "summary": "教材正文可用",
            "evidence": [],
            "confidence": 0.9,
        }
        parsed = parse_llm_result(
            json.dumps(payload, ensure_ascii=False),
            {"机械设计"},
            expected_sample_count=4,
            track="其他重要书籍",
        )
        self.assertEqual(4, len(parsed["sample_knowledge_labels"]))
        payload["sample_knowledge_labels"] = payload["sample_knowledge_labels"][:3]
        with self.assertRaisesRegex(ValueError, "知识形态标签未完整覆盖"):
            parse_llm_result(
                json.dumps(payload, ensure_ascii=False),
                {"机械设计"},
                expected_sample_count=4,
                track="其他重要书籍",
            )

    def test_llm_result_ignores_extra_fields_in_per_sample_labels(self) -> None:
        payload = {
            "decision": "PASS",
            "dominant_languages": ["zh"],
            "substantial_non_zh_en": False,
            "matched_l1_paths": [],
            "subject_relevance": "high",
            "knowledge_extraction_suitability": "high",
            "document_structure": "textbook",
            "dictionary_structure": "not_applicable",
            "entry_definition_alignment": "not_applicable",
            "continuous_prose_dominant": False,
            "original_publication_year": 2015,
            "sample_structure_labels": [
                {"sample_no": i, "structure": "structured_exposition", "note": "正文"}
                for i in range(1, 5)
            ],
            "sample_knowledge_labels": [
                {"sample_no": i, "knowledge_type": "general_knowledge", "note": "可复用"}
                for i in range(1, 5)
            ],
            "ocr_quality": "high",
            "rule_cleanable": True,
            "summary": "教材正文可用",
            "evidence": [],
            "confidence": 0.9,
        }

        parsed = parse_llm_result(
            json.dumps(payload, ensure_ascii=False),
            set(),
            expected_sample_count=4,
            track="其他重要书籍",
        )

        self.assertEqual(
            {"sample_no": 1, "structure": "structured_exposition"},
            parsed["sample_structure_labels"][0],
        )
        self.assertEqual(
            {"sample_no": 1, "knowledge_type": "general_knowledge"},
            parsed["sample_knowledge_labels"][0],
        )

    def test_original_publication_year_is_compared_by_code(self) -> None:
        old = apply_original_year_gate({
            "decision": "PASS",
            "original_publication_year": 1898,
            "summary": "正文可用",
            "evidence": [],
        })
        self.assertEqual("DROP", old["decision"])
        current = apply_original_year_gate({
            "decision": "PASS",
            "original_publication_year": 2006,
            "summary": "正文可用",
            "evidence": [],
        })
        self.assertEqual("PASS", current["decision"])

    def test_dictionary_structure_gate_drops_long_article_dominant(self) -> None:
        result = {
            "decision": "PASS",
            "dictionary_structure": "mixed",
            "entry_definition_alignment": "medium",
            "continuous_prose_dominant": True,
            "sample_structure_labels": [
                {"sample_no": i, "structure": "long_article" if i <= 10 else "compact_entries"}
                for i in range(1, 17)
            ],
            "summary": "模型原始判断",
            "evidence": [],
        }
        gated = apply_track_structure_gates(result, "辞海类", expected_sample_count=16)
        self.assertEqual("DROP", gated["decision"])
        self.assertTrue(gated["decision_adjusted_by_structure_gate"])

    def test_other_important_book_is_not_subject_to_dictionary_gate(self) -> None:
        result = {
            "decision": "PASS",
            "dictionary_structure": "absent",
            "entry_definition_alignment": "not_applicable",
            "continuous_prose_dominant": True,
            "sample_structure_labels": [
                {"sample_no": i, "structure": "structured_exposition"}
                for i in range(1, 17)
            ],
            "summary": "教材正文可用",
            "evidence": [],
        }
        gated = apply_track_structure_gates(
            result, "其他重要书籍", expected_sample_count=16
        )
        self.assertEqual("PASS", gated["decision"])

    def test_clear_encyclopedia_long_explanations_are_not_dropped(self) -> None:
        result = {
            "decision": "PASS",
            "document_structure": "encyclopedia_entries",
            "dictionary_structure": "clear",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": True,
            "sample_structure_labels": [
                {"sample_no": i, "structure": "long_article"}
                for i in range(1, 17)
            ],
            "summary": "每篇长文均解释明确词头",
            "evidence": [],
        }
        gated = apply_track_structure_gates(result, "辞海类", expected_sample_count=16)
        self.assertEqual("PASS", gated["decision"])


class MaterializationTests(unittest.TestCase):
    def test_materialization_conserves_rows_and_writes_exclusive_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            rows = []
            for identifier, track, decision in (
                ("a", "辞海类", "PASS"),
                ("b", "辞海类", "REVIEW"),
                ("c", "其他重要书籍", "DROP"),
            ):
                rows.append({
                    "identifier": identifier,
                    "title": identifier,
                    "book_track": track,
                    "final_decision": decision,
                })
            summary = materialize_results(rows, output, ["identifier", "title"])
            self.assertEqual(3, summary["total"])
            found = []
            for track in ("辞海类", "其他重要书籍"):
                for decision in ("PASS", "REVIEW", "DROP"):
                    path = output / track / decision / "本级0611字段书目.csv"
                    self.assertTrue(path.exists())
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        found.extend(row["identifier"] for row in csv.DictReader(handle))
            self.assertCountEqual(["a", "b", "c"], found)


if __name__ == "__main__":
    unittest.main()
