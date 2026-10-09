import tempfile
import unittest
from pathlib import Path

from auto_resolve_reviews import stage_files


class AutoResolveReviewTest(unittest.TestCase):
    def test_stage_paths_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            d=Path(temporary)
            source,salvage,retry,resolved=stage_files(d,'audit')
            self.assertEqual(source,d/'audit')
            self.assertEqual(len({source,salvage,retry,resolved}),4)


if __name__=='__main__':unittest.main()
