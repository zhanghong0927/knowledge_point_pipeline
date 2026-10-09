"""Small counterexamples for source-evidenced content guards."""
import unittest
from pathlib import Path

import clean_boundary_v42 as v42
import clean_compare1000 as c


ROOT = Path(__file__).parent/'20260922_clean_boundary_v41_regression270'


class BoundaryV42Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = {row['id']: row for row in c.read(ROOT/'INPUT.json')}

    def test_tenure_date_is_not_prose(self):
        for sid in ('B1092', 'B1782'):
            row = self.rows[sid]
            layout = v42.layout_evidence(row, row['raw_content'])
            self.assertTrue(any('January' in line and '–' in line for line in layout))

    def test_wrapped_heading_fragment_is_not_prose(self):
        row = self.rows['B0429']
        self.assertIn('concerning decoupling', v42.layout_evidence(row, row['raw_content']))

    def test_caption_and_author_use_md_context(self):
        row = self.rows['B0217']
        layout = v42.layout_evidence(row, row['raw_content'])
        self.assertTrue(any(line.startswith('Edith Louisa Cavell, ca.') for line in layout))
        self.assertIn('Alexander Mikaberidze', layout)
        self.assertNotIn('In August 1915, Cavell was betrayed', layout)

    def test_image_dependent_paragraph_only(self):
        row = self.rows['B1306']
        layout = v42.layout_evidence(row, row['raw_content'])
        self.assertTrue(any(line.startswith('The illustration shows') for line in layout))
        self.assertFalse(any(line.startswith('Axis interchange is the transposition') for line in layout))

    def test_dependent_definition(self):
        self.assertTrue(v42.DEPENDENT_DEFINITION.match('This act was designed to extend the provisions.'))
        self.assertTrue(v42.DEPENDENT_DEFINITION.match('The second use is historical.'))
        self.assertFalse(v42.DEPENDENT_DEFINITION.match('The Southwest Ordinance was passed in 1790.'))


if __name__ == '__main__':
    unittest.main()
