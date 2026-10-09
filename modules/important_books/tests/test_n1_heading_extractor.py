import json
from pathlib import Path
import tempfile
import unittest

import n1_heading_extractor as n1


class N1HeadingExtractorTests(unittest.TestCase):
    def test_markdown_hierarchy_extracts_only_knowledge_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("# 第一章 材料\n引言。\n## 1.1 强度\n正文说明材料承受外力而不发生破坏的能力，并需要结合具体受力条件进行分析。\n### 1.1.1 拉伸\n定义。\n## 1.2 刚度\n说明。\n", encoding="utf-8")
            result = n1.extract_document(md)
        units = result["units"]
        self.assertEqual([u["title"] for u in units], ["第一章 材料", "1.1 强度", "1.1.1 拉伸", "1.2 刚度"])
        self.assertEqual(units[2]["parent_path"], ["第一章 材料", "1.1 强度"])
        self.assertEqual(units[1]["knowledge_points"][0]["knowledge_point"], "强度")
        self.assertEqual(set(units[1]["knowledge_points"][0]), {"knowledge_point"})

    def test_numbered_headings_and_appendix_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("第一章 绪论\n本章介绍研究问题、研究范围以及全书的组织安排，并说明相关研究的背景、目标和后续章节内容。\n1.1 背景\n背景正文说明相关理论的发展过程及其对当前研究问题的影响。\n参考文献\n[1] 来源\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual(result["summary"]["extracted_units"], 2)
        self.assertEqual(result["summary"]["excluded_units"], 1)
        self.assertEqual(result["units"][-1]["exclude_reason"], "appendix_or_front_back_matter")

    def test_code_fence_is_not_a_heading_and_jump_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("```md\n# 示例标题\n```\n# 正文\n#### 跳级\n内容\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual([u["title"] for u in result["units"]], ["正文", "跳级"])
        self.assertIn("heading_level_jump", {w["code"] for w in result["warnings"]})

    def test_chinese_numbered_subheading_is_child_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("# 表演\n## 第三节 作品分析 / 94\n本节介绍作品分析的基本方法，并说明实际操作中需要注意的主要问题。\n\n一、理解作品背景\n分析创作背景、时代环境和作者意图之间的联系。\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual(result["units"][1]["title"], "第三节 作品分析")
        self.assertEqual(result["units"][2]["level"], 4)
        self.assertEqual(result["units"][2]["parent_path"], ["表演", "第三节 作品分析"])
        self.assertEqual(result["units"][2]["knowledge_points"][0]["knowledge_point"], "理解作品背景")

    def test_knowledge_point_contains_only_clean_heading_title(self):
        self.assertEqual(n1.make_knowledge_points("二、结构-功能主义的分析研究方法", ["第一章 总论"])[0]["knowledge_point"],
                         "结构-功能主义的分析研究方法")
        self.assertEqual(n1.make_knowledge_points("1.1.2 理论基础", ["第一章 总论"])[0]["knowledge_point"],
                         "理论基础")

    def test_ambiguous_numbered_list_lines_are_not_headings(self):
        lines = ["# 正文", "", "一、检查设备是否正常。", "二、按下启动按钮。", "1. 检查电源连接。", "2. 打开控制面板。"]
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("\n".join(lines), encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual([u["title"] for u in result["units"]], ["正文"])

    def test_chapter_section_and_numeric_depth_mapping(self):
        self.assertEqual(n1.heading_from_line("第一章 总论")[0], 2)
        self.assertEqual(n1.heading_from_line("第二节 研究方法")[0], 3)
        self.assertEqual(n1.heading_from_line("1.1.1 理论基础")[0], 4)
        self.assertEqual(n1.heading_from_line("（一）基本概念", previous_line="# 本章", next_line="概念说明文字")[0], 5)
        self.assertEqual(n1.heading_from_line("## 第一节 研究方法")[0], 3)
        self.assertEqual(n1.heading_from_line("### 1.1.1 理论基础")[0], 4)

    def test_no_headings_warns_and_preserves_source_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("纯文本正文。\n", encoding="utf-8")
            result = n1.extract_document(md)
            digest = n1.sha256(md)
        self.assertEqual(result["summary"]["extracted_units"], 0)
        self.assertIn("no_headings_found", {w["code"] for w in result["warnings"]})
        self.assertEqual(result["source"]["sha256"], digest)

    def test_heading_is_extracted_even_without_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("# 章节\n训练教程\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual(result["units"][0]["knowledge_point_count"], 1)
        self.assertEqual(result["units"][0]["knowledge_points"][0]["knowledge_point"], "章节")

    def test_batch_writes_machine_readable_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            root.mkdir()
            (root / "book.md").write_text("# Chapter\nThis paragraph contains a complete explanation.\n", encoding="utf-8")
            out = Path(tmp) / "out"
            summary = n1.run(root, out)
            item = json.loads(Path(summary["manifest"][0]["result"]).read_text(encoding="utf-8"))
            csv_text = (out / "knowledge_points.csv").read_text(encoding="utf-8-sig")
        self.assertEqual(summary["books_succeeded"], 1)
        self.assertEqual(item["units"][0]["knowledge_points"][0]["knowledge_point"], "Chapter")
        self.assertEqual(set(item["units"][0]["knowledge_points"][0]), {"knowledge_point"})
        self.assertIn("knowledge_point", csv_text.splitlines()[0])
        self.assertIn("Chapter", csv_text)
        self.assertEqual(summary["knowledge_points_csv"], str((out / "knowledge_points.csv").resolve()))
        self.assertEqual(summary["title_review_csv"], str((out / "title_review.csv").resolve()))

    def test_index_keeps_all_letter_groups_and_stops_at_publisher_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("## Index\n\n## A\n\nAcquisition, 13–15\n\n## W\n\nWeighting factors, 9\n\n## Taylor & Francis eBooks\n\nPromo text\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertEqual([e["term"] for e in result["index_entries"]], ["Acquisition", "Weighting factors"])
        self.assertEqual(result["index_entries"][0]["page_refs"], ["13–15"])
        self.assertEqual(result["summary"]["index_entry_count"], 2)
        self.assertTrue(all(u["status"] == "excluded" for u in result["units"]))

    def test_image_only_markdown_is_not_extracted_as_heading_or_index_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "book.md"
            md.write_text("## Chapter\n\nText explanation with enough detail for a section body.\n\n![](figure/image.jpeg)\n\n## Index\n\n## A\n\nAlpha, 1\n\n![](figure/index-image.jpeg)\n", encoding="utf-8")
            result = n1.extract_document(md)
        self.assertNotIn("![](figure/image.jpeg)", [u["title"] for u in result["units"]])
        self.assertNotIn("![](figure/index-image.jpeg)", [e["term"] for e in result["index_entries"]])


if __name__ == "__main__":
    unittest.main()
