"""验证规则提名的结构边界、逐字回查、来源偏移与保守缺证据行为。"""

import unittest

from book_extractor.markdown import parse_markdown, token_count
from book_extractor.rule_candidates import extract_rule_candidates


class RuleCandidateTests(unittest.TestCase):
    """使用小型真实 Markdown 验证规则抽取，不调用模型。"""

    def test_names_without_body_are_not_definition_evidence(self) -> None:
        """页码只作呈现标记；仅目录名称不能成为释义证据。"""
        units = parse_markdown("# 目录\n\n- 弹性模量 …… 23\n- 第一章 绪论 1\n- 123\n")
        candidates = extract_rule_candidates(units)
        self.assertEqual([item.name for item in candidates], ["弹性模量"])
        item = candidates[0]
        self.assertEqual(item.origins, ["toc"])
        self.assertTrue(item.name_evidence_ids)
        self.assertEqual(item.evidence_ids, [])
        self.assertEqual(item.evidence_spans, [])
        self.assertIn("insufficient_body_evidence", item.issues)

    def test_exact_english_boundary_and_separate_scopes(self) -> None:
        """英文不翻译且排除子串；跨章节命中独立保留避免提前并义。"""
        units = parse_markdown(
            "# Index\n\n- art 12\n\n# Trade\n\ncart contains nothing.\n\n"
            "# Aesthetics\n\nart is discussed here.\n\n"
            "# Practice\n\nart can mean a practical skill.\n"
        )
        candidates = extract_rule_candidates(units)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            [item.scope for item in candidates], [["Aesthetics"], ["Practice"]]
        )
        source = {unit.id: unit.text for unit in units}
        for item in candidates:
            self.assertEqual(item.name, "art")
            self.assertEqual(
                set(item.evidence_ids), {span.unit_id for span in item.evidence_spans}
            )
            text = "".join(
                source[span.unit_id][span.start : span.end]
                for span in item.evidence_spans
            )
            self.assertNotIn("cart", text)
            self.assertIn("# ", text)
        self.assertEqual(candidates, extract_rule_candidates(units))

    def test_glossary_table_requires_headers_and_keeps_column_order(self) -> None:
        """释义列在前亦可配对，并携带表头；未知列不能猜配对。"""
        units = parse_markdown(
            "# 词汇表\n\n| 定义 | 术语 |\n| --- | --- |\n"
            "| 物体抵抗变形的能力 | 刚度 |\n\n"
            "| 甲 | 乙 |\n| --- | --- |\n| 猜测解释 | 未知名称 |\n"
        )
        candidates = extract_rule_candidates(units)
        self.assertEqual([item.name for item in candidates], ["刚度"])
        source = {unit.id: unit.text for unit in units}
        item = candidates[0]
        excerpts = [
            source[span.unit_id][span.start : span.end] for span in item.evidence_spans
        ]
        self.assertTrue(any("| 定义 | 术语 |" in text for text in excerpts))
        self.assertTrue(any("物体抵抗变形" in text for text in excerpts))

    def test_colon_glossary_preserves_crlf_offsets(self) -> None:
        """冒号释义区间对应原始字符，CRLF 与非 ASCII 文本不发生转换。"""
        units = parse_markdown(
            "# Glossary\r\n\r\n- torque: turning effect.\r\n- 力：作用。\r\n"
        )
        items = extract_rule_candidates(units)
        self.assertEqual([item.name for item in items], ["torque", "力"])
        source = {unit.id: unit.text for unit in units}
        for item in items:
            span = item.evidence_spans[0]
            excerpt = source[span.unit_id][span.start : span.end]
            self.assertIn(item.name, excerpt)
            self.assertTrue(excerpt.endswith("\r\n"))

    def test_truncation_is_explicit_and_bounded(self) -> None:
        """多次命中及长上下文上限必须显式报告，而非静默丢弃。"""
        text = "# 索引\n\n- 应力 1\n\n# 正文\n\n"
        text += ("甲" * 300 + "应力" + "乙" * 300 + "\n\n") * 10
        items = extract_rule_candidates(parse_markdown(text))
        self.assertEqual(len(items), 8)
        for item in items:
            self.assertIn("rule_hits_truncated:8", item.issues)
            self.assertIn("rule_context_truncated:400_chars", item.issues)
            self.assertTrue(
                all(span.end - span.start <= 400 for span in item.evidence_spans)
            )

    def test_no_region_no_nomination_and_no_parent_child_concatenation(self) -> None:
        """普通列表不提名，索引缩进子词不与主词凭空拼接。"""
        self.assertEqual(
            extract_rule_candidates(parse_markdown("# 正文\n\n- 应力 23\n")), []
        )
        units = parse_markdown("# Index\n\n- engine 1\n  - cooling 2\n")
        self.assertEqual(
            [item.name for item in extract_rule_candidates(units)],
            ["engine", "cooling"],
        )

    def test_identical_windows_deduplicate(self) -> None:
        """同一短段落内重复词面只保留一个证据窗口，ID 可复现。"""
        items = extract_rule_candidates(
            parse_markdown(
                "# 索引\n\n- 弯矩 2\n- 弯矩 5\n\n# 正文\n\n"
                "弯矩影响结构，弯矩也可改变。\n"
            )
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "弯矩")

    def test_large_body_table_keeps_complete_header_and_row(self) -> None:
        """大表反查携带原表头和完整行，不能截断列后作为解释。"""
        text = "# 索引\n\n- 弯矩 2\n\n# 正文\n\n"
        text += "| 名称 | 解释 |\n| --- | --- |\n"
        text += "| 其他 | 无关数据 |\n" * 40
        text += "| 弯矩 | 使构件弯曲的力矩 |\n"
        units = parse_markdown(text)
        items = extract_rule_candidates(units)
        self.assertEqual(len(items), 1)
        source = {unit.id: unit.text for unit in units}
        excerpts = [
            source[span.unit_id][span.start : span.end]
            for span in items[0].evidence_spans
        ]
        self.assertIn("| 名称 | 解释 |\n| --- | --- |\n", excerpts)
        self.assertIn("| 弯矩 | 使构件弯曲的力矩 |\n", excerpts)
        self.assertIn("rule_large_table_row_only_or_unsupported", items[0].issues)

    def test_oversized_glossary_row_is_not_partial_column_evidence(self) -> None:
        """长表行无法完整保留时只留名称提名，不将半行当成解释。"""
        units = parse_markdown(
            "# 词汇表\n\n| 术语 | 释义 |\n| --- | --- |\n"
            + "| 弯矩 | "
            + "解释" * 300
            + " |\n"
        )
        item = extract_rule_candidates(units)[0]
        self.assertEqual(item.evidence_ids, [])
        self.assertIn("rule_large_table_row_only_or_unsupported", item.issues)

    def test_real_delivery_fragments(self) -> None:
        """真实交付片段覆盖括号页码、章节号、同级目录标题与索引字母。"""
        import json
        from pathlib import Path

        path = Path(__file__).parent / "fixtures/rule_candidates/real_fragments.json"
        cases = json.loads(path.read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["case_id"]):
                units = parse_markdown(case["text"])
                candidates = extract_rule_candidates(units)
                names = {item.name for item in candidates}
                self.assertTrue(set(case["expected_names"]) <= names, names)
                self.assertEqual("".join(unit.text for unit in units), case["text"])
                source = {unit.id: unit.text for unit in units}
                for item in candidates:
                    for span in item.evidence_spans:
                        self.assertTrue(
                            0 <= span.start < span.end <= len(source[span.unit_id])
                        )

    def test_numeric_terms_are_not_chapter_numbers(self) -> None:
        """索引及词汇表中的数字开头术语不能被目录编号清理改名。"""
        items = extract_rule_candidates(
            parse_markdown(
                "# Index\n\n100 years 24\n\n# Glossary\n\n100 years: a century.\n"
            )
        )
        self.assertEqual({item.name for item in items}, {"100 years"})

    def test_prose_and_bibliography_end_toc(self) -> None:
        """无页码英文完整句及参考文献标题之后不能继续按目录提名。"""
        text = (
            "# Contents\n\nPressure 12\n\nThis book presents an overview.\n\n"
            "Misleading title 20\n\n# Bibliography\n\nAuthor article 2020\n"
        )
        self.assertEqual(
            {item.name for item in extract_rule_candidates(parse_markdown(text))},
            {"Pressure"},
        )

    def test_unheaded_prose_ends_index_and_remains_body_evidence(self) -> None:
        """索引后的无标题正文仍可成为证据，之后的教学列表不得误作索引条目。"""
        units = parse_markdown(
            "# 索引\n\n- 惯性 2\n\n惯性是物体保持运动状态的性质。\n\n- 请完成实验报告\n"
        )
        items = extract_rule_candidates(units)
        self.assertEqual([item.name for item in items], ["惯性"])
        source = {unit.id: unit.text for unit in units}
        text = "".join(
            source[span.unit_id][span.start : span.end]
            for span in items[0].evidence_spans
        )
        self.assertIn("惯性是物体保持运动状态的性质。", text)

    def test_indented_page_entries_do_not_end_index(self) -> None:
        """Markdown缩进子项仍属于导航，不能使后续整段索引漏掉；真代码仍结束区域。"""
        text = (
            "# Index\n\nlight cylinder 20\n\n"
            "    magnetic field at, 65, 267\n\n    radius of, 52, 65\n\n"
            "Neutral hydrogen measurements,\n\n    194\n\n"
            "LIGO, 43\n\n```python\nvalue = 1\n```\n\nNot an entry 7\n"
        )
        names = {c.name for c in extract_rule_candidates(parse_markdown(text))}
        self.assertIn("LIGO", names)
        self.assertNotIn("Not an entry", names)

    def test_heading_match_collects_only_its_following_section(self) -> None:
        """名称只出现在标题时取其后正文，下一节的其他对象不能混入。"""
        units = parse_markdown(
            "# 目录\n\n气体静压润滑机理 2\n\n## 二、气体静压润滑机理\n\n"
            "该润滑由外部提供加压气体，通过节流作用建立承载能力。\n\n"
            "## 三、其他对象\n\n另一种机制来自周期振动。\n"
        )
        items = extract_rule_candidates(units)
        self.assertEqual(len(items), 1)
        source = {unit.id: unit.text for unit in units}
        excerpts = [
            source[span.unit_id][span.start : span.end]
            for span in items[0].evidence_spans
        ]
        self.assertTrue(any("通过节流作用" in text for text in excerpts))
        self.assertFalse(any("周期振动" in text for text in excerpts))
        self.assertLessEqual(sum(map(len, excerpts)), 400)

    def test_empty_heading_and_oversized_table_do_not_fake_definition(self) -> None:
        """空标题、立即遇子节或放不下的完整表格只保留名称，不能伪造定义证据。"""
        for tail in (
            "",
            "\n### 其他对象\n\n这里解释别的对象。\n",
            "\n| 名称 | 定义 |\n| --- | --- |\n| 项 | " + "内容" * 1000 + " |\n",
        ):
            with self.subTest(tail=tail[:30]):
                units = parse_markdown("# 目录\n\n目标 2\n\n## 目标\n" + tail)
                item = extract_rule_candidates(units)[0]
                self.assertEqual(item.evidence_ids, [])
                self.assertEqual(item.evidence_spans, [])
                self.assertIn("insufficient_body_evidence", item.issues)
                self.assertEqual(len(item.name_evidence_ids), 2)

    def test_heading_window_marks_partial_paragraph(self) -> None:
        """长正文截断时明确标记，所有跨度仍是原文半开字符坐标。"""
        units = parse_markdown("# 目录\n\n目标 2\n\n## 目标\n\n" + "这是说明。" * 400)
        item = extract_rule_candidates(units)[0]
        self.assertIn("rule_heading_context_truncated:512_tokens", item.issues)
        source = {unit.id: unit.text for unit in units}
        self.assertLessEqual(
            sum(
                token_count(source[s.unit_id][s.start : s.end])
                for s in item.evidence_spans
            ),
            512,
        )
        first = item.evidence_spans[0]
        self.assertTrue(source[first.unit_id][: first.end].endswith("。"))

    def test_short_token_english_paragraph_is_kept_whole(self) -> None:
        """字符较长但 token 少的英文完整段不能因旧400字符限制截成半个词。"""
        paragraph = (
            "In the context of a CT scan, the X-ray machine does not assess the "
            "attenuation along every line. Instead, the Radon transform is sampled "
            "for a finite number of angles and, at each of these angles, for some "
            "finite number of values of t. Both the angles and the t values are "
            "evenly spaced. So, in this model, the X-ray sources rotate by a fixed "
            "angle from one set of readings to the next and, within each setting, "
            "the individual X-ray beams are evenly spaced. "
            "This is called the parallel beam geometry.\n"
        )
        units = parse_markdown(
            "# Contents\n\nDiscrete Radon transform 2\n\n"
            "## Discrete Radon transform\n\n" + paragraph
        )
        item = extract_rule_candidates(units)[0]
        source = {unit.id: unit.text for unit in units}
        self.assertGreater(len(paragraph), 400)
        self.assertIn(
            paragraph, [source[s.unit_id][s.start : s.end] for s in item.evidence_spans]
        )
        self.assertNotIn("rule_heading_context_truncated:512_tokens", item.issues)

    def test_long_english_without_sentences_stops_at_word_boundary(self) -> None:
        """真正超预算且没有句号的英文段在空白处截断，不留下半个单词。"""
        units = parse_markdown(
            "# Contents\n\nTarget 2\n\n## Target\n\n" + "electromagnetism " * 1000
        )
        item = extract_rule_candidates(units)[0]
        source = {unit.id: unit.text for unit in units}
        first = item.evidence_spans[0]
        self.assertEqual(source[first.unit_id][first.end], " ")
        self.assertIn("rule_heading_context_truncated:512_tokens", item.issues)

    def test_multicolumn_index_is_not_fused_into_name(self) -> None:
        """多栏压平的两个名称及交叉索引短语不得被拼为新名称。"""
        items = extract_rule_candidates(
            parse_markdown(
                "# Index\n\n安分守己 2 浑浑噩噩 91 随遇而安 224\n\n"
                "Academy of Sciences, 1–2, 360 and S. Vavilov, 329, 330\n"
            )
        )
        self.assertEqual(items, [])
