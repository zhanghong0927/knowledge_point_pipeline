import unittest
from consolidate_retry_results import merge_stage


class ConsolidateRetryTest(unittest.TestCase):
    def test_applies_only_proven_recovery_and_preserves_initial_review(self):
        original=[{'item':{'request_id':'r1','name':'甲'},'review':{'judgment':'technical_failure'}},
                  {'item':{'request_id':'r2','name':'乙'},'review':{'judgment':'reasonable'}}]
        retried=[{'item':{'request_id':'r1','name':'甲'},'review':{'judgment':'technical_failure'}},
                 {'item':{'request_id':'r2','name':'乙'},'review':{'judgment':'reasonable'}}]
        repaired=[{'request_id':'r1','recovered_final':{'judgment':'uncertain'}}]
        result=merge_stage(original,[('full','retry',retried),('recovered','salvage',repaired)])
        self.assertEqual(result[0]['review']['judgment'],'uncertain')
        self.assertEqual(result[0]['initial_review']['judgment'],'technical_failure')
        self.assertEqual(result[1]['review']['judgment'],'reasonable')
        self.assertEqual(original[0]['review']['judgment'],'technical_failure')

    def test_rejects_item_change_in_full_overlay(self):
        original=[{'item':{'request_id':'r1','name':'甲'},'review':{'judgment':'technical_failure'}}]
        changed=[{'item':{'request_id':'r1','name':'乙'},'review':{'judgment':'reasonable'}}]
        with self.assertRaises(ValueError):merge_stage(original,[('full','retry',changed)])


if __name__=='__main__':unittest.main()
