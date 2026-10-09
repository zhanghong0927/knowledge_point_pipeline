import unittest

from full_branch_dedup import dedup_records


def item(identifier,name,english,path,definition=''):
    return dict(id=identifier,name=name,knowledge_point=english,main_tags=path,
                related_tags=[],definition=definition,description='',en_definition='',en_description='')


class LeafDedupTest(unittest.TestCase):
    def test_dictionary_wins_duplicates_only_within_exact_leaf(self):
        dictionary=[item('d1','甲','Alpha','甲分支/一','short')]
        full=[item('f1','甲','Beta','甲分支/二','much longer definition'),
              item('f2','乙','alpha','甲分支/一'),
              item('f3','甲','Alpha','乙分支/一'),
              item('f4','','','甲分支/四'),item('f5','','','甲分支/五')]
        kept,audit=dedup_records(full,dictionary)
        self.assertEqual([r['id'] for r in kept],['d1','f1','f3','f4','f5'])
        self.assertEqual(kept[0]['related_tags'],[])
        self.assertEqual(sum(r['origin']=='dictionary' for r in kept),1)
        self.assertEqual(len(audit),1)

    def test_same_id_from_full_is_reconciled_even_if_path_changed(self):
        dictionary=[item('42','词','Term','新分支/一')]
        full=[item('42','旧词','Old','旧分支/一')]
        kept,audit=dedup_records(full,dictionary)
        self.assertEqual([r['id'] for r in kept],['42'])
        self.assertEqual(kept[0]['main_tags'],'新分支/一')
        self.assertEqual(len(audit),1)

    def test_longer_definition_wins_when_no_dictionary_record(self):
        full=[item('a','同名','','一/甲','x'),item('b','同名','','一/甲','longer')]
        kept,_=dedup_records(full,[])
        self.assertEqual([r['id'] for r in kept],['b'])

    def test_same_title_in_sibling_leaves_is_retained(self):
        full=[item('a','同名','','一/甲'),item('b','同名','','一/乙')]
        kept,audit=dedup_records(full,[])
        self.assertEqual([r['id'] for r in kept],['a','b'])
        self.assertEqual(audit,[])

    def test_paths_must_match_literally(self):
        full=[item('a','同名','','甲/乙'),item('b','同名','','甲 / 乙'),
              item('c','同名','','甲/乙/')]
        kept,audit=dedup_records(full,[])
        self.assertEqual([r['id'] for r in kept],['a','b','c'])
        self.assertEqual(audit,[])

    def test_unverified_node_is_not_deduplicated(self):
        full=[item('a','同名','','一/父'),item('b','同名','','一/父'),
              item('c','另一词','','一/父/叶'),item('d','另一词','','一/父/叶')]
        kept,audit=dedup_records(full,[],eligible_leaf_paths={'一/父/叶'})
        self.assertEqual([r['id'] for r in kept],['a','b','c'])
        self.assertEqual([r['removed_id'] for r in audit],['d'])


if __name__=='__main__':unittest.main()
