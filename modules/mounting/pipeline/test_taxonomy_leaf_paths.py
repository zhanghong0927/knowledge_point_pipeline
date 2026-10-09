import unittest

from taxonomy_leaf_paths import leaf_paths


class TaxonomyLeafPathsTest(unittest.TestCase):
    def test_nested_tree_excludes_parent(self):
        tree = {'name': '学科', 'path': '', 'children': [
            {'name': '甲', 'path': '甲', 'children': [
                {'name': '乙', 'path': '甲/乙', 'children': []}]}]}
        self.assertEqual(leaf_paths(tree), {'甲/乙', '学科/甲/乙'})

    def test_management_flat_nodes_use_leaf_flag(self):
        data = {'nodes': [
            {'path_names': ['管理学', '一'], 'is_leaf': False},
            {'path_names': ['管理学', '一', '二'], 'is_leaf': True}]}
        self.assertEqual(leaf_paths(data), {'管理学/一/二', '一/二'})

    def test_philosophy_hierarchy_uses_deepest_real_node(self):
        data = {'hierarchy': [
            {'name': '中哲', 'l2_nodes': [
                {'name': '古代', 'l3_nodes': [
                    {'name': '儒家', 'l4_nodes': ['仁', '礼']},
                    {'name': '道家', 'l4_nodes': ['—']},
                ]},
            ]},
        ]}
        paths = leaf_paths(data)
        self.assertIn('中哲/古代/儒家/仁', paths)
        self.assertIn('中哲/古代/道家', paths)
        self.assertNotIn('中哲/古代/儒家', paths)


if __name__ == '__main__':
    unittest.main()
