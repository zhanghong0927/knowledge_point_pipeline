"""验证 Markdown 无损解析和受预算约束的分块。"""

import unittest

from book_extractor.markdown import chunk_units, parse_markdown, token_count


class MarkdownTests(unittest.TestCase):
    """覆盖结构边界、章节上下文及 Unicode 易损场景。"""

    def test_lossless_lines_and_unmapped_text(self) -> None:
        """保留 CRLF、空行、引用定义和 Unicode 分隔符，重组后等于原文。"""
        text = "\r\n# 标题\r\n\r\n正文\u2028仍在同一行。\r\n\r\n[x]: https://example.org\r\n"
        units = parse_markdown(text)
        self.assertEqual("".join(unit.text for unit in units), text)
        self.assertEqual(units[0].start_line, 1)
        self.assertEqual(units[-1].end_line, 6)
        self.assertEqual("".join(chunk.text for chunk in chunk_units(units)), text)
        self.assertEqual(parse_markdown(""), [])
        self.assertEqual(parse_markdown("\n\n")[0].text, "\n\n")

    def test_structure_and_heading_context(self) -> None:
        """围栏内标题不产生章节，小型结构块不被拆散。"""
        text = (
            "章名\n====\n\n```python\n# 不是标题\nx = 1\n```\n\n"
            "| 名称 | 值 |\n| --- | --- |\n| 力 | -20 |\n\n"
            "$$\nF=ma\n\nE=mc^2\n$$\n\n## 第二节\n正文。\n"
        )
        units = parse_markdown(text)
        self.assertEqual(
            [unit.headings for unit in units if unit.kind == "heading"],
            [["章名"], ["章名", "第二节"]],
        )
        chunks = chunk_units(units, 120)
        for unit in units:
            if unit.kind in {"code", "table", "math"}:
                self.assertEqual(sum(unit.text in chunk.text for chunk in chunks), 1)
        self.assertEqual(sum(unit.kind == "math" for unit in units), 1)
        self.assertEqual("".join(chunk.text for chunk in chunks), text)

    def test_long_chinese_and_special_tokens(self) -> None:
        """长中文、表情和字面 tokenizer 特殊标记都能完整分块。"""
        text = "# 中文\n\n" + "力矩与转动条件。中文𠮷😀无空格" * 120 + "<|endoftext|>"
        units = parse_markdown(text)
        chunks = chunk_units(units, 48)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(token_count(chunk.text) <= 48 for chunk in chunks))
        self.assertEqual("".join(chunk.text for chunk in chunks), text)
        self.assertTrue(
            all(set(chunk.unit_ids) <= {unit.id for unit in units} for chunk in chunks)
        )
        self.assertEqual(chunks, chunk_units(units, 48))
        with self.assertRaises(ValueError):
            chunk_units(units, 0)

    def test_pack_adjacent_sections(self) -> None:
        """合并相邻短章节时，保留每个源单元自身的章节范围。"""
        text = "# 甲\n\n甲定义。\n\n## 子节\n\n补充。\n\n# 乙\n\n乙定义。\n"
        units = parse_markdown(text)
        chunks = chunk_units(units, 4000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].headings, ["甲", "子节", "乙"])
        self.assertEqual(chunks[0].text, text)
        self.assertEqual(units[-1].headings, ["乙"])


if __name__ == "__main__":
    unittest.main()
