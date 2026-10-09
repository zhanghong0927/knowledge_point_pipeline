import unittest
from name_queue import verify_report,can_follow
class NameQueueTests(unittest.TestCase):
    def test_follow_only_finished_mount_queue(self):
        self.assertTrue(can_follow('completed'));self.assertTrue(can_follow('finished_with_issues'))
        self.assertFalse(can_follow('paused_on_failures'));self.assertFalse(can_follow('audit'))
    def test_coverage_rejects_incomplete_report(self):
        r={'records':3,'completed':3,'counts':{'keep':1,'drop':1,'review':1}}
        verify_report(r,3)
        for k in ['records','completed']:
            bad=dict(r);bad[k]=2
            with self.assertRaises(ValueError):verify_report(bad,3)
        with self.assertRaises(ValueError):verify_report(dict(r,counts={'keep':2}),3)
