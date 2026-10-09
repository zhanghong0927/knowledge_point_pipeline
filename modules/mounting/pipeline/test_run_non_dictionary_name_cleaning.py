import unittest

from run_non_dictionary_name_cleaning import validate_counts


class ValidateCountsTest(unittest.TestCase):
    def test_complete_counts(self):
        validate_counts({'records': 3, 'completed': 3, 'counts': {'keep': 1, 'drop': 1, 'review': 1}}, 3)

    def test_incomplete_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_counts({'records': 3, 'completed': 2, 'counts': {'keep': 1, 'drop': 1, 'review': 0}}, 3)

    def test_decision_sum_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_counts({'records': 3, 'completed': 3, 'counts': {'keep': 1, 'drop': 1, 'review': 0}}, 3)


if __name__ == '__main__':
    unittest.main()
