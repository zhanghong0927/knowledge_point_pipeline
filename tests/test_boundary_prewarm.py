"""Boundary-only preparation is an asset prewarm, never a record run."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_mounting_bridge import bridge
from test_mounting_boundaries import generated_fixture

ROOT = Path(__file__).resolve().parents[1]
SLUG = 'mechanical_engineering'
IDENTITY = 'https://upstream.example/v1/chat/completions'
PHYSICAL = 'http://gateway.local/_jobs/taxonomy_mechanical_engineering/v1/chat/completions'


class BoundaryPrewarmTests(unittest.TestCase):
    def args(self, root, out='prewarm'):
        tree = root / 'tree.json'
        bridge.dump(tree, {'name': '机械工程', 'children': [{'name': '传动', 'children': [{'name': '齿轮'}]}]})
        from types import SimpleNamespace
        return SimpleNamespace(out=root / out, input=None, registry=bridge.REGISTRY,
            taxonomy=tree, taxonomy_dir=root, subject_slug=SLUG, require_cleaned=False,
            require_boundaries=True, boundary_only=True, boundary_cache_identity_url=IDENTITY,
            boundary_cache_dir=root / 'cache', boundary_failure_policy='strict',
            boundary_review_mode='off', boundary_context_bytes=50000,
            boundary_rewrite_rounds=2, boundary_reuse_root=None, threshold=.85,
            min_depth=2, max_depth=5, max_related=2, limit=0, api_url=PHYSICAL,
            model='mock', api_key_env='', workers=2, timeout=10, max_tokens=1024,asset_maintenance=True)

    def test_prepare_has_only_requested_subject_and_no_knowledge_points(self):
        with tempfile.TemporaryDirectory() as d:
            args = self.args(Path(d))
            self.assertEqual(bridge.prepare(args)['selected'], 0)
            m = bridge.read(args.out / 'PREPARED.json')
            self.assertEqual(set(m['groups']), {SLUG})
            self.assertTrue(m['boundary_only'])
            self.assertEqual(m['boundary_cache_identity_url'], IDENTITY)
            self.assertEqual(m['groups'][SLUG]['records'], 0)
            for path in (args.out / 'input.snapshot.jsonl', args.out / 'unconfigured.jsonl',
                         args.out / 'groups' / SLUG / 'input.jsonl'):
                self.assertEqual(bridge.rows(path), [])
            self.assertEqual(bridge.read(args.out / 'groups' / SLUG / 'original_records.json'), {})
            self.assertTrue((args.out / 'groups' / SLUG / 'boundary_input.json').exists())

    def test_prepare_rejects_misuse_and_bad_identity_before_creating_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, changes in (
                ('input', {'input': root / 'records.jsonl'}),
                ('cleaned', {'require_cleaned': True}),
                ('boundaries', {'require_boundaries': False}),
                ('slug', {'subject_slug': None}),
                ('url', {'boundary_cache_identity_url': 'https://u:p@example.com/v1?x=1'})):
                with self.subTest(name=name):
                    args = self.args(root, name)
                    vars(args).update(changes)
                    with self.assertRaises(ValueError):
                        bridge.prepare(args)
                    self.assertFalse(args.out.exists())

    def test_unconfigured_taxonomy_fails_before_output(self):
        with tempfile.TemporaryDirectory() as d:
            args = self.args(Path(d))
            args.taxonomy = None
            args.registry = Path(d) / 'registry.json'
            bridge.dump(args.registry, {SLUG: {'name': '机械工程', 'file': None}})
            with self.assertRaisesRegex(ValueError, 'taxonomy'):
                bridge.prepare(args)
            self.assertFalse(args.out.exists())

    def test_cli_rejects_boundary_only_for_nonprepare_action(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, str(ROOT / 'adapters/mounting_bridge.py'),
                'route', '--out', d, '--boundary-only'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--boundary-only is valid only with prepare', result.stderr)

    def test_generation_uses_physical_url_but_logical_identity_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as d:
            args = self.args(Path(d))
            bridge.prepare(args)
            args.boundary_cache_identity_url = 'https://changed.example/v1'
            with self.assertRaisesRegex(ValueError, 'identity'):
                bridge.generate_boundaries(args)
            args.boundary_cache_identity_url = IDENTITY
            def native(command, **kwargs):
                self.assertEqual(command[command.index('--base') + 1], bridge.endpoint(PHYSICAL))
                generated_fixture(args, unreviewed=True)
            with patch.object(bridge.subprocess, 'run', side_effect=native) as run:
                result = bridge.generate_boundaries(args)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result['groups'][SLUG]['cache_identity']['generation']['api_root'], bridge.endpoint(IDENTITY))

    def test_verified_prewarm_reused_by_normal_mount_and_cannot_export_itself(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args = self.args(root)
            bridge.prepare(args)
            with patch.object(bridge.subprocess, 'run', side_effect=lambda *a, **k: generated_fixture(args, unreviewed=True)):
                bridge.generate_boundaries(args)
            bridge.verify_boundaries(args)
            for action in (bridge.route, bridge.review, bridge.export):
                with self.subTest(action=action.__name__), self.assertRaisesRegex(ValueError, 'boundary-only'):
                    action(args)
            normal = self.args(root, 'normal')
            normal.boundary_only = False
            normal.input = root / 'records.jsonl'
            bridge.write_rows(normal.input, [{'id': '1', 'name': '齿轮', 'tag': '机械工程'}])
            bridge.prepare(normal)
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('cache should avoid model')):
                result = bridge.generate_boundaries(normal)
            self.assertTrue(result['groups'][SLUG]['cache_reused'])

    def test_pipeline_ignores_legacy_api_cache_identity_for_fixed_project_assets(self):
        spec = importlib.util.spec_from_file_location('prewarm_pipeline', ROOT / 'pipeline.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        config, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        config['mounting']['boundaries'].update(enabled=True, cache_identity_url=IDENTITY)
        tasks = pipeline.plan(config, values)[4]['tasks']
        for name in ('prepare_mounting', 'generate_mounting_boundaries'):
            cmd = next(t['command'] for t in tasks if t['name'] == name)
            self.assertNotIn('--boundary-cache-identity-url',cmd)
            self.assertNotIn('--boundary-cache-dir',cmd)
        config['mounting']['boundaries']['cache_identity_url'] = 'http://example.com/?q=1'
        self.assertEqual(tasks,pipeline.plan(config,values)[4]['tasks'])

    def test_pipeline_identity_plan_works_in_fresh_process(self):
        command = "import pipeline; from pathlib import Path; c,v=pipeline.load_config(Path('configs/pipeline.example.json')); c['mounting']['boundaries'].update(enabled=True,cache_identity_url='https://upstream.example/v1/chat/completions'); pipeline.plan(c,v)"
        result = subprocess.run([sys.executable, '-c', command], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
