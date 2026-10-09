import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dictionary_md_audit.py")
SPEC = importlib.util.spec_from_file_location("dictionary_md_audit", MODULE_PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def base_result():
    return {
        "title_guess": "测试辞典",
        "document_type": "dictionary",
        "dominant_languages": ["zh"],
        "substantial_non_zh_en": False,
        "publication_year": None,
        "age_risk": "unknown",
        "semantic_quality": "high",
        "ocr_quality": "high",
        "knowledge_density": "high",
        "knowledge_extraction_suitability": "high",
        "sample_structure_labels": [
            {"sample_no": sample_no, "structure": "compact_entries"}
            for sample_no in range(1, 13)
        ],
        "primary_extraction_obstacles": [],
        "rule_cleanable": True,
        "decision": "PASS",
        "summary": "结构与文本均可用",
        "problems": [],
        "manual_review_targets": [],
        "confidence": 0.95,
    }


def add_strict_quality_labels(
    result, *, candidate_headwords=20, generic_headwords=2
):
    result["sample_quality_labels"] = [
        {
            "sample_no": sample_no,
            "content_role": "body",
            "languages": ["zh"],
            "fluency": "fluent",
            "definite_text_error_count": 0,
            "candidate_headword_count": candidate_headwords,
            "generic_or_nonknowledge_headword_count": generic_headwords,
        }
        for sample_no in range(1, 13)
    ]
    return result


def add_knowledge_value_labels(
    result, *, discipline=17, nonknowledge=3, sample_no=1,
    other_language_samples=(), unusable_samples=(), unclear_samples=(),
):
    labels = []
    for current_sample_no in range(1, 13):
        counts = {
            "discipline_concept": 0,
            "method_technology_system": 0,
            "object_material_device": 0,
            "person_work_event": 0,
            "general_language": 0,
            "trivia_anecdote": 0,
            "navigation": 0,
        }
        if current_sample_no == sample_no:
            counts["discipline_concept"] = discipline
            counts["general_language"] = nonknowledge
        labels.append({"sample_no": current_sample_no, **counts})
    result["sample_headword_type_counts"] = labels
    result["sample_knowledge_value_flags"] = [
        {
            "sample_no": current_sample_no,
            "content_role": "body",
            "issue_basis": "source_damage" if current_sample_no in unusable_samples else "uncertain" if current_sample_no in unclear_samples else "none",
            "languages": (
                ["en", "es"] if current_sample_no in other_language_samples else ["en"]
            ),
            "entry_extraction_status": (
                "unusable" if current_sample_no in unusable_samples
                else "unclear" if current_sample_no in unclear_samples
                else "usable"
            ),
        }
        for current_sample_no in range(1, 13)
    ]
    result["knowledge_value_assessment"] = {
        "usability": "high", "dominant_content": "disciplinary_entries",
        "examples": [
            {"sample_no": 1, "headword": "Term one", "anchor": "Definition one"},
            {"sample_no": 2, "headword": "Term two", "anchor": "Definition two"},
        ],
    }
    return result


class DictionaryGateTests(unittest.TestCase):
    def parse(self, result):
        return AUDIT.parse_json_content(
            json.dumps(result, ensure_ascii=False), expected_sample_count=12
        )

    def parse_strict(self, result):
        return AUDIT.parse_json_content(
            json.dumps(result, ensure_ascii=False),
            expected_sample_count=12,
            strict_text_quality=True,
            max_generic_headword_ratio=0.15,
        )

    def parse_knowledge(self, result, **kwargs):
        return AUDIT.parse_json_content(
            json.dumps(result, ensure_ascii=False),
            expected_sample_count=12,
            knowledge_value_mode=True,
            **kwargs,
        )

    def test_missing_dictionary_gate_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "dictionary gate fields"):
            self.parse(base_result())

    def test_clear_dictionary_structure_can_pass(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        self.assertEqual(self.parse(result)["decision"], "PASS")

    def test_continuous_prose_is_forced_to_drop_when_evidence_exists(self):
        result = base_result()
        result.update({
            "dictionary_structure": "absent",
            "subject_relevance": "high",
            "entry_definition_alignment": "low",
            "continuous_prose_dominant": True,
            "decision": "REVIEW",
            "problems": [{
                "sample_no": 2,
                "problem_type": "非辞海结构",
                "anchor": "连续章节正文",
                "reason": "缺少独立词条边界",
                "severity": "major",
                "rule_cleanable": False,
            }],
        })
        try:
            parsed = self.parse(result)
        except ValueError as exc:
            self.fail(f"hard gate should normalize a supported DROP: {exc}")
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(len(parsed["problems"]), 1)
        self.assertEqual(parsed["decision_before_gate"], "REVIEW")
        self.assertTrue(parsed["decision_adjusted_by_gate"])

    def test_mixed_structure_is_downgraded_from_pass_to_review(self):
        result = base_result()
        result.update({
            "dictionary_structure": "mixed",
            "subject_relevance": "high",
            "entry_definition_alignment": "medium",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "REVIEW")
        self.assertTrue(parsed["decision_adjusted_by_gate"])

    def test_low_subject_relevance_is_forced_to_drop(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "low",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "decision": "REVIEW",
            "problems": [{
                "sample_no": 1,
                "problem_type": "学科不相关",
                "anchor": "非目标学科内容",
                "reason": "正文与目标学科低相关",
                "severity": "major",
                "rule_cleanable": False,
            }],
        })
        try:
            parsed = self.parse(result)
        except ValueError as exc:
            self.fail(f"hard gate should normalize a supported DROP: {exc}")
        self.assertEqual(parsed["decision"], "DROP")

    def test_long_article_majority_is_forced_to_drop_even_if_model_calls_it_pass(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "sample_structure_labels": [
                {"sample_no": sample_no, "structure": structure}
                for sample_no, structure in enumerate(
                    ["frontmatter_index", "compact_entries"]
                    + ["long_article"] * 10,
                    1,
                )
            ],
            "problems": [{
                "sample_no": 3,
                "problem_type": "非辞海结构",
                "anchor": "人物传记长文",
                "reason": "一个标题统领多段连续叙述",
                "severity": "major",
                "rule_cleanable": False,
            }],
        })
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertTrue(parsed["decision_adjusted_by_gate"])

    def test_every_input_sample_must_have_one_structure_label(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "sample_structure_labels": result["sample_structure_labels"][:-1],
        })
        with self.assertRaisesRegex(ValueError, "cover every input sample"):
            self.parse(result)

    def test_long_article_majority_synthesizes_evidence_when_model_omits_it(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "sample_structure_labels": [
                {"sample_no": sample_no, "structure": structure}
                for sample_no, structure in enumerate(
                    ["compact_entries"] * 4 + ["long_article"] * 8, 1
                )
            ],
        })
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(parsed["problems"][0]["sample_no"], 5)

    def test_insufficient_compact_entry_ratio_downgrades_pass_to_review(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "sample_structure_labels": [
                {"sample_no": sample_no, "structure": structure}
                for sample_no, structure in enumerate(
                    ["compact_entries"] * 8 + ["long_article"] * 4, 1
                )
            ],
        })
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "REVIEW")
        self.assertTrue(parsed["decision_adjusted_by_gate"])

    def test_model_mixed_sample_label_is_normalized_to_unclear(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_structure_labels"][-1]["structure"] = "mixed"
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertEqual(parsed["sample_structure_counts"]["unclear"], 1)

    def test_model_image_heavy_sample_label_is_normalized_to_unclear(self):
        result = base_result()
        result.update({
            "dictionary_structure": "absent",
            "subject_relevance": "high",
            "entry_definition_alignment": "low",
            "continuous_prose_dominant": True,
            "decision": "DROP",
            "problems": [{
                "sample_no": 12,
                "problem_type": "图表公式缺失",
                "anchor": "图像页",
                "reason": "核心内容主要位于图片中",
                "severity": "major",
                "rule_cleanable": False,
            }],
        })
        result["sample_structure_labels"][-1]["structure"] = "image_heavy"
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(parsed["sample_structure_counts"]["unclear"], 1)

    def test_flattened_result_exports_structure_counts(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse(result)
        row = AUDIT.flattened_result({
            "short_id": "ECO0001",
            "subject": "经济学",
            "file_name": "test.md",
            "input_title": "test",
            "relative_path": "test.md",
            "source_path": "/tmp/test.md",
            "result": parsed,
            "usage": {},
        })
        self.assertEqual(row["compact_entry_samples"], 12)
        self.assertEqual(row["long_article_samples"], 0)
        self.assertEqual(row["substantive_sample_count"], 12)

    def test_long_repeated_phrase_is_compacted_before_prompt(self):
        phrase = "I turned myself into a man, although I was a "
        source = "prefix " + phrase * 12 + " suffix"
        compacted = AUDIT.compact_repetition_for_prompt(source)
        self.assertIn("已压缩", compacted)
        self.assertLess(len(compacted), len(source) // 2)

    def test_non_chinese_english_dominant_language_is_forced_to_drop(self):
        result = base_result()
        result.update({
            "dominant_languages": ["de"],
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertTrue(parsed["decision_adjusted_by_gate"])

    def test_prompt_accepts_translation_only_bilingual_glossaries(self):
        self.assertIn("只有双语词条对照而没有扩展解释，也不因此降级", AUDIT.SYSTEM_PROMPT)

    def test_prompt_treats_long_entry_explanations_as_entries(self):
        self.assertIn("释义篇幅较长或包含多个自然段", AUDIT.SYSTEM_PROMPT)
        self.assertIn("仍应标为 compact_entries", AUDIT.SYSTEM_PROMPT)

    def test_prompt_requires_traditional_chinese_body_to_drop(self):
        self.assertIn("繁体正文主体必须判为 DROP", AUDIT.SYSTEM_PROMPT)

    def test_traditional_chinese_document_gate_forces_drop(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse(result)
        gated = AUDIT.apply_document_hard_gates(parsed, {
            "traditional_chinese_body": True,
            "cjk_chars": 10000,
            "traditional_variant_chars": 3000,
            "traditional_ratio": 0.3,
        })
        self.assertEqual(gated["decision"], "DROP")
        self.assertTrue(gated["decision_adjusted_by_traditional_gate"])
        self.assertEqual(gated["decision_before_traditional_gate"], "PASS")

    def test_repeated_multilingual_heading_signals_are_detected(self):
        text = "\n\n".join([
            "## THÉÂTRE, THEATER, TEATRO\nEnglish definition.",
            "## COMMUNAUTÉ, GEMEINSCHAFT, COMUNIDAD\nEnglish definition.",
            "## SCÈNE, BÜHNE, ESCENA\nEnglish definition.",
            "## ÉCRITURE, SCHREIBEN, ESCRITURA\nEnglish definition.",
            "## CORPOREITÉ, KÖRPERLICHKEIT, CORPORALIDAD\nEnglish definition.",
        ])
        stats = AUDIT.non_zh_en_heading_stats(text)
        self.assertTrue(stats["non_zh_en_heading_signal"])
        self.assertEqual(stats["non_zh_en_heading_count"], 5)

    def test_pinyin_and_dated_person_headings_do_not_trigger_multilingual_gate(self):
        text = "\n".join([
            "## 绿色 lǜsè",
            "## 女性 nǚxìng",
            "## MÉLIÈS, GEORGES, 1861-1938",
            "## TRUFFAUT, FRANÇOIS, 1932-1984",
            "## BUÑUEL, LUIS, 1900-1983",
        ])
        stats = AUDIT.non_zh_en_heading_stats(text)
        self.assertFalse(stats["non_zh_en_heading_signal"])

    def test_multilingual_heading_document_gate_forces_drop(self):
        result = base_result()
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse(result)
        gated = AUDIT.apply_document_hard_gates(parsed, {
            "traditional_chinese_body": False,
            "non_zh_en_heading_signal": True,
            "non_zh_en_heading_count": 5,
            "non_zh_en_heading_chars": 23,
        })
        self.assertEqual(gated["decision"], "DROP")
        self.assertTrue(gated["decision_adjusted_by_heading_language_gate"])

    def test_strict_body_text_error_forces_drop(self):
        result = add_strict_quality_labels(base_result())
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_quality_labels"][3].update({
            "fluency": "error",
            "definite_text_error_count": 1,
        })
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(parsed["strict_body_error_samples"], 1)
        self.assertTrue(parsed["decision_adjusted_by_strict_quality_gate"])

    def test_strict_non_body_error_is_ignored(self):
        result = add_strict_quality_labels(base_result())
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_quality_labels"][0].update({
            "content_role": "non_body",
            "languages": ["other"],
            "fluency": "error",
            "definite_text_error_count": 3,
            "candidate_headword_count": 0,
            "generic_or_nonknowledge_headword_count": 0,
        })
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertEqual(parsed["strict_body_error_samples"], 0)
        self.assertFalse(parsed["strict_body_has_other_language"])

    def test_strict_other_language_in_body_forces_drop(self):
        result = add_strict_quality_labels(base_result())
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_quality_labels"][1]["languages"] = ["en", "other"]
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertTrue(parsed["strict_body_has_other_language"])

    def test_strict_specific_foreign_language_codes_normalize_to_other(self):
        result = add_strict_quality_labels(base_result())
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_quality_labels"][1]["languages"] = ["en", "it", "fr", "de"]
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(
            parsed["sample_quality_labels"][1]["languages"], ["en", "other"]
        )

    def test_strict_generic_headword_ratio_above_threshold_forces_drop(self):
        result = add_strict_quality_labels(
            base_result(), candidate_headwords=20, generic_headwords=4
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertAlmostEqual(parsed["generic_headword_ratio"], 0.2)

    def test_strict_generic_headword_ratio_at_threshold_can_pass(self):
        result = add_strict_quality_labels(
            base_result(), candidate_headwords=20, generic_headwords=3
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertAlmostEqual(parsed["generic_headword_ratio"], 0.15)

    def test_strict_unclear_body_quality_downgrades_pass_to_review(self):
        result = add_strict_quality_labels(base_result())
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        result["sample_quality_labels"][2]["fluency"] = "unclear"
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "REVIEW")
        self.assertEqual(parsed["strict_unclear_body_samples"], 1)

    def test_strict_too_few_candidate_headwords_requires_review(self):
        result = add_strict_quality_labels(
            base_result(), candidate_headwords=1, generic_headwords=0
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_strict(result)
        self.assertEqual(parsed["decision"], "REVIEW")
        self.assertEqual(parsed["candidate_headword_count"], 12)

    def test_strict_prompt_states_zero_body_error_and_generic_limit(self):
        prompt = AUDIT.build_system_prompt(
            strict_text_quality=True, max_generic_headword_ratio=0.15
        )
        self.assertIn("任何一个正文片段", prompt)
        self.assertIn("明确错字", prompt)
        self.assertIn("15%", prompt)
        self.assertIn("候选词头", prompt)

    def test_flattened_result_exports_strict_quality_metrics(self):
        result = add_strict_quality_labels(
            base_result(), candidate_headwords=20, generic_headwords=2
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_strict(result)
        row = AUDIT.flattened_result({
            "short_id": "ART0001",
            "subject": "艺术学",
            "file_name": "test.md",
            "input_title": "test",
            "relative_path": "test.md",
            "source_path": "/tmp/test.md",
            "result": parsed,
            "usage": {},
        })
        self.assertTrue(row["strict_text_quality"])
        self.assertEqual(row["candidate_headword_count"], 240)
        self.assertEqual(row["generic_or_nonknowledge_headword_count"], 24)
        self.assertAlmostEqual(row["generic_headword_ratio"], 0.1)

    def test_knowledge_value_prompt_distinguishes_long_entries_and_headword_types(self):
        prompt = AUDIT.build_system_prompt(knowledge_value_mode=True)
        self.assertIn("long_entry", prompt)
        self.assertIn("knowledge_value_assessment", prompt)
        self.assertIn("普通语言学习", prompt)
        self.assertIn("趣闻轶事", prompt)
        self.assertIn("词头、同义词和多语种对应词也属于正文语言证据", prompt)

    def test_knowledge_value_long_entries_count_as_valid_dictionary_structure(self):
        result = add_knowledge_value_labels(base_result(), discipline=20, nonknowledge=0)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
            "sample_structure_labels": [
                {"sample_no": sample_no, "structure": "long_entry"}
                for sample_no in range(1, 13)
            ],
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertEqual(parsed["long_entry_samples"], 12)

    def test_knowledge_value_nonknowledge_ratio_at_fifteen_percent_can_pass(self):
        result = add_knowledge_value_labels(base_result(), discipline=17, nonknowledge=3)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertIsNone(parsed["nonknowledge_headword_ratio"])

    def test_knowledge_value_middle_ratio_does_not_downgrade(self):
        result = add_knowledge_value_labels(base_result(), discipline=16, nonknowledge=4)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertFalse(parsed["decision_adjusted_by_knowledge_value_gate"])

    def test_knowledge_value_majority_count_does_not_downgrade(self):
        result = add_knowledge_value_labels(base_result(), discipline=8, nonknowledge=12)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertFalse(parsed["decision_adjusted_by_knowledge_value_gate"])

    def test_knowledge_value_word_count_is_not_an_evidence_gate(self):
        result = add_knowledge_value_labels(base_result(), discipline=1, nonknowledge=3)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result, min_headword_evidence=10)
        self.assertEqual(parsed["decision"], "PASS")
        self.assertEqual(parsed["knowledge_value_evidence_count"], 2)

    def test_knowledge_value_other_language_sample_forces_drop(self):
        result = add_knowledge_value_labels(
            base_result(), discipline=20, nonknowledge=0, other_language_samples=(4,)
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(parsed["knowledge_other_language_samples"], 1)

    def test_knowledge_value_systematic_unusable_entry_boundaries_force_drop(self):
        result = add_knowledge_value_labels(
            base_result(), discipline=20, nonknowledge=0, unusable_samples=(2, 5, 8)
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "DROP")
        self.assertEqual(parsed["unusable_entry_samples"], 3)

    def test_knowledge_value_single_unusable_entry_boundary_requires_review(self):
        result = add_knowledge_value_labels(
            base_result(), discipline=20, nonknowledge=0, unusable_samples=(2,)
        )
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        self.assertEqual(parsed["decision"], "REVIEW")

    def test_hybrid_samples_mix_uniform_and_heading_anchored_context(self):
        sections = [
            f"## 术语{i}\n这是术语{i}的定义和详细解释。" + ("内容" * 80)
            for i in range(1, 25)
        ]
        samples = AUDIT.hybrid_entry_samples(
            "\n\n".join(sections), sample_count=12, chunk_chars=300
        )
        kinds = {sample["sampling_kind"] for sample in samples}
        self.assertEqual(len(samples), 12)
        self.assertEqual(kinds, {"uniform", "entry_anchor"})
        anchored = [sample for sample in samples if sample["sampling_kind"] == "entry_anchor"]
        self.assertTrue(all(sample["text"].startswith("## ") for sample in anchored))

    def test_flattened_result_exports_knowledge_value_metrics(self):
        result = add_knowledge_value_labels(base_result(), discipline=17, nonknowledge=3)
        result.update({
            "dictionary_structure": "clear",
            "subject_relevance": "high",
            "entry_definition_alignment": "high",
            "continuous_prose_dominant": False,
        })
        parsed = self.parse_knowledge(result)
        row = AUDIT.flattened_result({
            "short_id": "SOC0001",
            "subject": "社会学",
            "file_name": "test.md",
            "input_title": "test",
            "relative_path": "test.md",
            "source_path": "/tmp/test.md",
            "result": parsed,
            "usage": {},
        })
        self.assertTrue(row["knowledge_value_mode"])
        self.assertEqual(row["knowledge_value_policy"], AUDIT.KNOWLEDGE_VALUE_POLICY_VERSION)
        self.assertEqual(row["knowledge_value_evidence_count"], 2)
        self.assertIsNone(row["nonknowledge_headword_ratio"])

    def test_prompt_snapshot_uses_actual_run_configuration(self):
        snapshot = AUDIT.build_prompt_snapshot([{
            "subject": "艺术学",
            "sampling_rule": "最多16个均匀分布片段，每片段最多1200字符",
            "api_url": "http://new-job/v1/chat/completions",
            "model": "test-model",
            "strict_text_quality": True,
            "max_generic_headword_ratio": 0.15,
            "min_candidate_headwords": 20,
            "system_prompt": "STRICT PROMPT",
        }])
        self.assertIn("http://new-job/v1/chat/completions", snapshot)
        self.assertIn("最多16个均匀分布片段", snapshot)
        self.assertIn("严格正文质量：启用", snapshot)
        self.assertIn("STRICT PROMPT", snapshot)


if __name__ == "__main__":
    unittest.main()
