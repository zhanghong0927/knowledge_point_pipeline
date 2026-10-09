import unittest
from retry_technical_alt import mapping_excerpt_items


class MappingExcerptTest(unittest.TestCase):
    def test_caps_long_definition_without_mutating_original(self):
        original={'record_id':'M1','name':'古代希腊','definition':'甲'*5000,'description':'短描述'}
        result=mapping_excerpt_items([original],limit=3200)
        self.assertEqual(result[0]['record_id'],'M1')
        self.assertEqual(result[0]['definition'],'甲'*3200)
        self.assertEqual(result[0]['description'],'短描述')
        self.assertEqual(len(original['definition']),5000)

    def test_preserves_short_definition(self):
        original={'record_id':'M2','definition':'短定义'}
        self.assertEqual(mapping_excerpt_items([original],limit=3200),[original])


if __name__=='__main__':unittest.main()
