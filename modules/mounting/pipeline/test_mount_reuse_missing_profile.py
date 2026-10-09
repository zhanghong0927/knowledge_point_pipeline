"""Regression: a partial old run must not crash reuse before mounting."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from runner import mount


class MissingOldProfileTest(unittest.TestCase):
    def test_skips_old_run_with_missing_profile_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=root/'old'
            (old/'remount').mkdir(parents=True)
            (old/'remount'/'with_cards.jsonl').write_text('',encoding='utf-8')
            tree={'name_zh':'测试','children':[]}
            self.assertEqual(mount([],root/'new',tree,[],old),[])

    def test_mount_reserves_output_within_model_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            tree={'name_zh':'测试','children':[]}
            item={'record_id':'r1','name':'测试词条'}
            observed=[]
            def fake_external_call(command,log):
                observed.extend(command)
                (root/'new'/'new.jsonl').write_text(json.dumps({**item,'knowledge_labeling':{'status':'ok'}},ensure_ascii=False)+'\n',encoding='utf-8')
            with patch('runner.call',fake_external_call):
                result=mount([item],root/'new',tree,[],None)
            self.assertEqual(result[0]['knowledge_labeling']['status'],'ok')
            self.assertEqual(observed[observed.index('--max-tokens')+1],'4096')


if __name__=='__main__':unittest.main()
