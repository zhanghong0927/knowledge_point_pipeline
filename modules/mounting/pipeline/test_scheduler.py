import unittest
from shared_gate import eligible
class GateTests(unittest.TestCase):
    def test_total_cap(self):self.assertFalse(eligible('primary',1024,0,0,1024,960))
    def test_background_yields_to_waiting_primary(self):self.assertFalse(eligible('background',2,2,1,1024,960))
    def test_reserve_primary_slots(self):self.assertFalse(eligible('background',960,960,0,1024,960))
    def test_primary_can_fill_remaining(self):self.assertTrue(eligible('primary',960,960,0,1024,960))
    def test_background_fills_idle(self):self.assertTrue(eligible('background',10,8,0,1024,960))
