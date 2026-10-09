import tempfile
import unittest
from pathlib import Path

import n3_rules_extractor as n3


class N3RulesOnlyTests(unittest.TestCase):
    def extract(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / 'book.md'
            md.write_text(text, encoding='utf-8')
            result = n3.extract_document(md)
        for point in result['knowledge_points']:
            for hit in point['occurrences']:
                self.assertIn(point['knowledge_point'], hit['source_quote'])
                self.assertEqual(hit['source_quote'], '\n'.join(text.splitlines()[hit['source_line_start']-1:hit['source_line_end']]))
        return result

    def test_split_adjacent_items_and_continuation(self):
        r = self.extract('# 第一章\n1. 岗位责任制：明确职责。\n补充解释。\n2. 风险评估：评估风险。')
        self.assertEqual([p['knowledge_point'] for p in r['knowledge_points']], ['岗位责任制', '风险评估'])
        self.assertEqual(r['knowledge_points'][0]['source_line_end'], 3)
        self.assertNotIn('风险评估', r['knowledge_points'][0]['source_quote'])

    def test_references_skip_children_and_resume(self):
        r = self.extract('# 第一章\n1. 岗位责任制：明确职责。\n# 参考文献\n## 文献\n1. 噪声理论：排除。\n# 第二章\n1. 风险评估：评估风险。')
        self.assertEqual([p['knowledge_point'] for p in r['knowledge_points']], ['岗位责任制', '风险评估'])

    def test_toc_and_frontmatter(self):
        r = self.extract('# 前言\n1. 前言理论：排除。\n# 目录\n第一章 ... 1\n# 第一章\n1. 岗位责任制：明确职责。')
        self.assertEqual(r['rule']['body_start_line'], 5)
        self.assertEqual(r['summary']['candidate_count'], 1)

    def test_parent_items_and_markdown_item_titles(self):
        r = self.extract('# 第一章\n## 一、管理职能\n1. 计划职能：制定计划。\n（1）风险评估：分析风险。\n2. 组织职能：协调组织。')
        points = {p['knowledge_point']: p for p in r['knowledge_points']}
        self.assertEqual(points['风险评估']['parent_item_lines'], [2, 3])
        self.assertEqual(points['组织职能']['parent_item_lines'], [2])
        self.assertEqual(points['计划职能']['item_type'], 'enumeration')

    def test_definitions_and_no_fabricated_action_labels(self):
        r = self.extract('# 第一章\n1. 风险评估是指识别风险的活动。\n2. 启动前必须检查电源和接地。')
        self.assertEqual(r['knowledge_points'][0]['knowledge_point'], '风险评估')
        self.assertEqual(r['summary']['review_count'], 1)
        self.assertNotIn('设备启动安全条件', str(r['knowledge_points']))

    def test_duplicate_constraints_are_retained(self):
        r = self.extract('# 第一章\n1. 风险评估：如果设备异常，则必须评估。\n2. 风险评估：当设备启动时，应当评估，除检修外。')
        self.assertEqual(len(r['knowledge_points']), 1)
        hits = r['knowledge_points'][0]['occurrences']
        self.assertEqual(len(hits), 2)
        self.assertNotEqual(hits[0]['conditions'], hits[1]['conditions'])
        self.assertEqual(hits[1]['exceptions'], ['除检修外'])

    def test_images_tables_and_generic_labels(self):
        r = self.extract('# 第一章\n1. 岗位责任制：明确职责。\n![](figure/a.jpeg)\n| 名称 | 值 |\n2. 注意事项：必须检查。')
        self.assertEqual(r['summary']['knowledge_point_count'], 1)
        self.assertEqual(r['summary']['review_count'], 1)
        self.assertNotIn('jpeg', r['knowledge_points'][0]['source_quote'])

    def test_unnumbered_rule_vs_expository_prose(self):
        r = self.extract('# 第一章\n管理需要考虑历史条件。\n必须检查电源。')
        self.assertEqual(r['summary']['candidate_count'], 1)
        self.assertEqual(r['review_candidates'][0]['item_type'], 'rule')
