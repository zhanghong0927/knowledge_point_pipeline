import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import n1_heading_extractor as title_filter


class N1TitleFilterTests(unittest.TestCase):
    @staticmethod
    def source_with_markers():
        lines = [""] * 4558
        lines[0] = "Corporate"
        lines[1] = "Governance"
        lines[14] = "## Shital Jhunjhunwala"
        lines[16] = "## Corporate Governance"
        lines[54] = "## ACKNOWLEDGEMENTS"
        lines[66] = "## DISCLAIMER"
        lines[74] = "## CONTENTS"
        lines[542] = "## LIST OF FIGURES"
        lines[648] = "## LIST OF TABLES"
        lines[708] = "## Corporate Governance: An Introduction"
        lines[712] = "## SH Company:"
        lines[724] = "## Questions"
        lines[734] = "## WHAT IS CORPORATE GOVERNANCE?"
        lines[4556] = "## INDEX"
        return "\n".join(lines)

    def make_result(self):
        titles = [
            ("Shital Jhunjhunwala", 15, 20),
            ("Corporate Governance", 17, 40),
            ("ACKNOWLEDGEMENTS", 55, 220),
            ("CONTENTS", 75, 0),
            ("Corporate Governance: An Introduction", 709, 40),
            ("Questions", 725, 100),
            ("WHAT IS CORPORATE GOVERNANCE?", 735, 1200),
            ("Case Study: SH Company", 800, 400),
            ("INDEX", 4557, 0),
        ]
        units = []
        for i, (title, line, chars) in enumerate(titles, 1):
            units.append({
                "unit_id": f"H{i:05d}", "title": title, "level": 2,
                "parent_path": [], "heading_line": line,
                "content_line_start": line + 1, "content_line_end": line + 2,
                "content_char_count": chars, "status": "extracted",
                "knowledge_points": [{"knowledge_point": title}],
                "knowledge_point_count": 1,
            })
        return {
            "source": {"path": "book.md"},
            "summary": {"extracted_units": len(units), "knowledge_point_count": len(units),
                        "review_units": 0, "excluded_units": 0,
                        "index_entry_count": 2, "index_heading_line": 4557},
            "units": units,
            "index_entries": [{"term": "Governance", "page_refs": ["9"]},
                              {"term": "Shareholders", "page_refs": ["10"]}],
        }

    def test_filters_front_matter_labels_and_cover_title(self):
        source = self.source_with_markers()
        result = title_filter.filter_result(self.make_result(), source)
        by_title = {u["title"]: u for u in result["units"]}
        self.assertEqual(by_title["Shital Jhunjhunwala"]["status"], "excluded")
        self.assertEqual(by_title["Corporate Governance"]["exclude_reason"], "title_filter:repeated_cover_title")
        self.assertEqual(by_title["ACKNOWLEDGEMENTS"]["status"], "excluded")
        self.assertEqual(by_title["Questions"]["status"], "excluded")
        self.assertEqual(by_title["Case Study: SH Company"]["status"], "needs_review")

    def test_keeps_body_titles_and_sends_ambiguous_headings_to_review(self):
        source = self.source_with_markers()
        result = title_filter.filter_result(self.make_result(), source)
        by_title = {u["title"]: u for u in result["units"]}
        self.assertEqual(by_title["WHAT IS CORPORATE GOVERNANCE?"]["status"], "extracted")
        self.assertEqual(by_title["Case Study: SH Company"]["status"], "needs_review")

    def test_sends_colon_terminated_case_labels_to_review(self):
        result = self.make_result()
        result["units"][7]["title"] = "SH Company:"
        result["units"][7]["knowledge_points"] = [{"knowledge_point": "SH Company:"}]
        filtered = title_filter.filter_result(result, self.source_with_markers())
        self.assertEqual(filtered["units"][7]["status"], "needs_review")

    def test_removes_page_numbered_toc_run_and_repairs_parent_paths(self):
        units = []
        for i, title in enumerate([
            "第一章 政治制度 …… 4", "第一节 研究对象 …… 4", "第二节 研究方法 …… 7",
            "第一章 政治制度", "第一节 制度结构", "一、国家机构",
            "1. 权力来源", "2. 权力划分",
        ], 1):
            toc = i <= 3
            units.append({
                "unit_id": f"H{i:05d}", "title": title,
                "level": [2, 3, 3, 2, 3, 4, 2, 2][i - 1],
                "parent_path": ["目录里的错误父级"] if i >= 4 else [],
                "heading_line": i * 2,
                "content_char_count": 0 if toc else 100,
                "status": "extracted",
                "knowledge_points": [{"knowledge_point": title}],
                "knowledge_point_count": 1,
            })
        result = {"source": {"path": "book.md"}, "summary": {}, "units": units,
                  "index_entries": [{"term": "保留索引", "page_refs": ["9"]}]}
        filtered = title_filter.filter_result(result, "\n".join(["正文"] * 20))
        self.assertEqual([u["status"] for u in filtered["units"][:3]], ["excluded"] * 3)
        self.assertEqual(filtered["units"][3]["parent_path"], [])
        self.assertEqual(filtered["units"][4]["parent_path"], ["第一章 政治制度"])
        self.assertEqual(filtered["units"][5]["parent_path"], ["第一章 政治制度", "第一节 制度结构"])
        self.assertEqual(filtered["units"][5]["knowledge_points"][0]["knowledge_point"],
                         "国家机构")
        self.assertEqual(filtered["units"][6]["level"], 5)
        self.assertEqual(filtered["units"][7]["level"], 5)
        self.assertEqual(filtered["units"][7]["parent_path"], filtered["units"][6]["parent_path"])
        self.assertEqual(filtered["index_entries"][0]["term"], "保留索引")
        self.assertEqual(filtered["title_filter"]["toc_ranges"][0]["entry_count"], 3)

    def test_strips_leading_numbering_from_display_name_but_keeps_hierarchy(self):
        units = [
            {"unit_id": "H1", "title": "第一章 比较政治制度", "level": 2,
             "parent_path": [], "heading_line": 10, "content_char_count": 100,
             "status": "extracted", "knowledge_points": [{"knowledge_point": "old"}],
             "knowledge_point_count": 1},
            {"unit_id": "H2", "title": "第二节 西方学者研究政治制度的现状和方法", "level": 3,
             "parent_path": [], "heading_line": 20, "content_char_count": 100,
             "status": "extracted", "knowledge_points": [{"knowledge_point": "old"}],
             "knowledge_point_count": 1},
            {"unit_id": "H3", "title": "二、结构-功能主义的分析研究方法", "level": 4,
             "parent_path": [], "heading_line": 30, "content_char_count": 100,
             "status": "extracted", "knowledge_points": [{"knowledge_point": "old"}],
             "knowledge_point_count": 1},
        ]
        result = {"source": {"path": "book.md"}, "summary": {}, "units": units}
        filtered = title_filter.filter_result(result, "\n".join(["正文"] * 40))
        point = filtered["units"][2]["knowledge_points"][0]["knowledge_point"]
        self.assertEqual(point, "结构-功能主义的分析研究方法")
        self.assertEqual(filtered["units"][2]["title"], "二、结构-功能主义的分析研究方法")
        self.assertEqual(filtered["units"][2]["parent_path"], ["第一章 比较政治制度", "第二节 西方学者研究政治制度的现状和方法"])

    def test_filters_chinese_questions_and_bibliography_subheads(self):
        source = self.source_with_markers()
        result = self.make_result()
        extra = []
        for i, title in enumerate(["思考题", "一、中文著作", "参考文献 …… 247"], 20):
            extra.append({"unit_id": f"H{i:05d}", "title": title, "level": 2,
                          "parent_path": [], "heading_line": 4300 + i,
                          "content_char_count": 50, "status": "extracted",
                          "knowledge_points": [{"knowledge_point": title}],
                          "knowledge_point_count": 1})
        result["units"].extend(extra)
        filtered = title_filter.filter_result(result, source)
        self.assertTrue(all(u["status"] == "excluded" for u in filtered["units"][-3:]))

    def test_preserves_index_entries_separate_from_knowledge_csv(self):
        source = self.source_with_markers()
        result = title_filter.filter_result(self.make_result(), source)
        with tempfile.TemporaryDirectory() as temp:
            csv_path = Path(temp) / "points.csv"
            count = title_filter.write_knowledge_points_csv(result, csv_path, identifier="sample")
            text = csv_path.read_text(encoding="utf-8-sig")
        self.assertEqual(count, result["summary"]["knowledge_point_count"])
        self.assertEqual(len(result["index_entries"]), 2)
        self.assertNotIn("Governance,9", text)
        self.assertNotIn("INDEX", text)

    def test_recalculates_counts_and_is_idempotent(self):
        source = self.source_with_markers()
        original = self.make_result()
        filtered = title_filter.filter_result(original, source)
        again = title_filter.filter_result(filtered, source)
        self.assertEqual(filtered, again)
        self.assertEqual(filtered["summary"]["knowledge_point_count"], 2)
        self.assertEqual(filtered["summary"]["title_filter_removed_count"], 6)
        self.assertEqual(original["summary"]["knowledge_point_count"], 9)


if __name__ == "__main__":
    unittest.main()
