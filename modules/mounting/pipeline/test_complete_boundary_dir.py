import json
import tempfile
import unittest
from pathlib import Path

from runner import complete_boundary_dir


class CompleteBoundaryDirTest(unittest.TestCase):
    def test_prefers_complete_fallback_over_incomplete_prior_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name, count in [('boundaries_v5',9),('boundaries_v6_alt',10)]:
                folder=root/name;folder.mkdir()
                (folder/'summary.json').write_text(json.dumps(dict(total_tree_nodes=10,accepted_candidate_cards=count,source_unchanged=True)))
            self.assertEqual(complete_boundary_dir(root),root/'boundaries_v6_alt')

    def test_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);folder=root/'boundaries_v5';folder.mkdir()
            (folder/'summary.json').write_text(json.dumps(dict(total_tree_nodes=10,accepted_candidate_cards=10,source_unchanged=False)))
            self.assertIsNone(complete_boundary_dir(root))


if __name__=='__main__':unittest.main()
