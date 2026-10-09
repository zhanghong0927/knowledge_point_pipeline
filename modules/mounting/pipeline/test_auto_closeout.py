import json
import tempfile
import unittest
from pathlib import Path

from auto_closeout import boundary_dir,resume_command


class AutoCloseoutTest(unittest.TestCase):
    def test_resume_preserves_original_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            c=dict(tree='/tree.json',base='http://base',model='model',workers=64,max_depth=-1,
                   max_context_bytes=500000,review_mode='advisory',cross_review='off',reuse_run='/old',
                   max_output_tokens=4096,http_timeout_seconds=240)
            (run/'config.json').write_text(json.dumps(c))
            cmd,actual=resume_command(run)
            self.assertEqual(actual,c)
            self.assertIn('--resume',cmd)
            self.assertEqual(cmd[cmd.index('--reuse-run')+1],'/old')

    def test_selects_newest_existing_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            d=Path(temporary);v5=d/'boundaries_v5';v5.mkdir();(v5/'config.json').write_text('{}')
            self.assertEqual(boundary_dir(d),v5)
            v6=d/'boundaries_v6_alt';v6.mkdir();(v6/'config.json').write_text('{}')
            self.assertEqual(boundary_dir(d),v6)


if __name__=='__main__':unittest.main()
