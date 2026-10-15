"""Project assets stay identical across tracks, APIs, and controller lifetimes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_mounting_bridge import bridge,fixture
from test_mounting_boundaries import generated_fixture

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def project_fixture(root):
    """Publish a tiny, fully evidenced existing asset without calling a model."""
    import mounting_tree_cache as cache
    args,records=fixture(root)
    args.out=root/'source_boundaries'
    args.require_boundaries=True
    args.boundary_failure_policy='strict'
    args.boundary_review_mode='off'
    args.boundary_context_bytes=50000
    args.boundary_rewrite_rounds=2
    args.boundary_cache_dir=root/'old_cache'
    bridge.prepare(args)
    with patch.object(bridge.subprocess,'run',side_effect=lambda *a,**k:generated_fixture(args,unreviewed=True)):
        bridge.generate_boundaries(args)
    verified=bridge.verify_boundaries(args)
    asset=Path(verified['groups']['mechanical_engineering']['taxonomy_asset'])
    project=root/'project_assets';project.mkdir()
    shutil.copytree(asset,project/'v1/mechanical_engineering')
    lock=project/'LOCK.json'
    bridge.dump(lock,{'schema':1,'project_version':'test-v1','subjects':{
        'mechanical_engineering':{'asset':'v1/mechanical_engineering',
          'manifest_sha256':bridge.sha(project/'v1/mechanical_engineering/MANIFEST.json')}}})
    return args,records,lock


class ProjectTaxonomyTests(unittest.TestCase):
    @unittest.skipIf(os.name=='nt','The production CLI uses POSIX file locking')
    def test_actual_cli_imports_same_release_for_two_task_directories_without_api_settings(self):
        with tempfile.TemporaryDirectory() as d:
            args,_,lock=project_fixture(Path(d))
            pins=[]
            for track in ('dictionary','important'):
                output=Path(d)/track
                common=['--out',str(output),'--project-assets-lock',str(lock)]
                for action,extra in [('prepare',['--input',str(args.input),'--subject-slug','mechanical_engineering']),
                                     ('activate-project-assets',[]),('verify-boundaries',[])]:
                    result=subprocess.run([sys.executable,'-B',str(ROOT/'adapters/mounting_bridge.py'),action,*common,*extra],
                                          cwd=ROOT,encoding='utf-8',capture_output=True)
                    self.assertEqual(result.returncode,0,result.stderr)
                pin=bridge.frozen(output)
                pins.append(pin['project_assets'])
                self.assertEqual(bridge.read(output/'BOUNDARIES_GENERATED.json')['model_calls'],0)
            self.assertEqual(pins[0],pins[1])

    def test_two_tracks_and_different_apis_use_identical_fixed_assets_without_models(self):
        import project_taxonomy as project
        with tempfile.TemporaryDirectory() as d:
            args,_,lock=project_fixture(Path(d))
            snapshots=[]
            for track,api in [('dictionary','http://first.invalid/v1'),('important','http://second.invalid/v1')]:
                args.out=Path(d)/track
                args.project_assets_lock=lock
                args.boundary_cache_dir=None
                args.api_url=api;args.model=track+'-different-model'
                with patch.object(bridge.subprocess,'run',side_effect=AssertionError('no generation subprocess')):
                    bridge.prepare(args)
                    project.activate(args)
                    bridge.verify_boundaries(args)
                m=bridge.frozen(args.out)
                snapshots.append((m['project_assets']['lock_sha256'],
                    bridge.sha(args.out/'groups/mechanical_engineering/profile/data/knowledge_tree.json'),
                    bridge.sha(args.out/'groups/mechanical_engineering/profile/data/semantic_cards.jsonl')))
                self.assertEqual(m['project_assets']['version'],'test-v1')
                self.assertFalse(m['boundary_model_reviewed'])
            self.assertEqual(snapshots[0],snapshots[1])

    def test_missing_subject_corrupt_file_or_lock_drift_fail_closed(self):
        import project_taxonomy as project
        with tempfile.TemporaryDirectory() as d:
            args,_,lock=project_fixture(Path(d))
            with self.assertRaisesRegex(ValueError,'subject|Subject'):
                project.subject('medicine',lock)
            args.out=Path(d)/'pinned';args.project_assets_lock=lock;args.boundary_cache_dir=None
            bridge.prepare(args)
            saved=lock.read_bytes();lock.write_bytes(saved+b'\n')
            with self.assertRaisesRegex(ValueError,'[Pp]roject.*changed|[Ll]ock.*changed'):
                bridge.frozen(args.out,allow_pending_boundaries=True)
            lock.write_bytes(saved)
            asset=project.subject('mechanical_engineering',lock)['asset_path']
            (Path(asset)/'boundaries/runtime_cards.jsonl').write_text('tampered\n')
            with self.assertRaisesRegex(ValueError,'checksum|changed'):
                project.subject('mechanical_engineering',lock)

    def test_task_taxonomy_cannot_override_project_tree(self):
        import project_taxonomy
        with tempfile.TemporaryDirectory() as d:
            args,_,lock=project_fixture(Path(d))
            args.out=Path(d)/'mismatch';args.project_assets_lock=lock;args.boundary_cache_dir=None
            args.taxonomy.write_text('{"name":"different","children":[]}')
            with self.assertRaisesRegex(ValueError,'[Tt]axonomy.*project|[Pp]roject.*taxonomy'):
                bridge.prepare(args)
            self.assertFalse(args.out.exists())

    def test_normal_pipeline_uses_project_assets_even_when_old_config_says_generate(self):
        import pipeline
        for name in ('pipeline.example.json','pipeline.important_llm.example.json'):
            config,values=pipeline.load_config(ROOT/'configs'/name)
            tasks=[t for s in pipeline.plan(config,values) for t in s['tasks']]
            prepare=next(t for t in tasks if t['name']=='prepare_mounting')
            activate=next(t for t in tasks if t['name']=='generate_mounting_boundaries')
            self.assertIn('--project-assets-lock',prepare['command'])
            self.assertIn('activate-project-assets',activate['command'])
            self.assertNotIn('--api-url',activate['command'])
            self.assertIn('project_assets',values['taxonomy'])


if __name__=='__main__':unittest.main()
