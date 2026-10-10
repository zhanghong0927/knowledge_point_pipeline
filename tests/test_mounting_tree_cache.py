"""Saved taxonomy assets are reusable, versioned and fail closed."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_mounting_boundaries as boundaries
from test_mounting_bridge import bridge
import mounting_tree_cache as cache_module

ROOT = Path(__file__).resolve().parents[1]


class MountingTreeCacheTests(unittest.TestCase):
    def prepare(self, root, cache, policy='strict'):
        root.mkdir()
        args, records = boundaries.MountingBoundaryTests().prepare_unreviewed(root)
        # Preparation freezes cache configuration along with the review policy.
        args.out = root / 'cached_mount'
        args.boundary_cache_dir = cache
        args.boundary_failure_policy = policy
        bridge.prepare(args)
        return args, records

    def generate(self, args):
        with patch.object(bridge.subprocess, 'run', side_effect=lambda *a, **k:
                          boundaries.generated_fixture(args, unreviewed=True)):
            bridge.generate_boundaries(args)

    def publish(self, root, cache):
        args, _ = self.prepare(root, cache)
        self.generate(args)
        bridge.verify_boundaries(args)
        entries = list(cache.glob('mechanical_engineering/*/MANIFEST.json'))
        self.assertEqual(len(entries), 1)
        return args, entries[0].parent

    def test_verified_tree_is_saved_with_boundary_fields_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args, asset = self.publish(root / 'first', root / 'trees')
            meta = bridge.read(asset / 'MANIFEST.json')
            self.assertFalse(meta['model_reviewed'])
            self.assertEqual(meta['nodes'], 4)
            self.assertEqual(bridge.sha(asset / 'taxonomy_original.json'), bridge.sha(args.taxonomy))
            enriched = bridge.read(asset / 'taxonomy_enriched.json')
            self.assertEqual(enriched['name_zh'], '机械工程')
            self.assertEqual(enriched['children'][0]['children'][0]['name_zh'], '齿轮')
            self.assertEqual(enriched['boundary_status'], 'model_generated_unreviewed')
            self.assertIn('boundary', enriched['semantic_boundary'])
            self.assertTrue(enriched['semantic_card'])
            index = bridge.read(args.out / 'groups/mechanical_engineering/node_index.json')
            self.assertEqual(enriched['code'], next(iter(index)))
            self.assertFalse((asset / 'input.snapshot.jsonl').exists())

    def test_second_run_reuses_saved_tree_without_boundary_model_calls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            self.publish(root / 'first', cache)
            args, _ = self.prepare(root / 'second', cache)
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('No new boundary calls')):
                result = bridge.generate_boundaries(args)
            self.assertTrue(result['groups']['mechanical_engineering']['cache_reused'])
            report = bridge.verify_boundaries(args)
            self.assertEqual(report['operational_nodes'], 4)
            self.assertFalse(report['model_reviewed'])
            self.assertEqual(len(list(cache.glob('mechanical_engineering/*/MANIFEST.json'))), 1)

    def test_unverified_generation_does_not_publish_a_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            args, _ = self.prepare(root / 'first', cache)
            self.generate(args)
            self.assertFalse(cache.exists())
            report = bridge.read(args.out / 'BOUNDARIES_GENERATED.json')
            name = next(name for name in report['groups']['mechanical_engineering']['assets']
                        if Path(name).parent.name == 'groups')
            saved = bridge.read(args.out / name)
            saved['output']['rounds'][0]['generation']['result'] = []
            bridge.dump(args.out / name, saved)
            report['groups']['mechanical_engineering']['assets'][name] = bridge.sha(args.out / name)
            bridge.dump(args.out / 'BOUNDARIES_GENERATED.json', report)
            with self.assertRaises(ValueError):
                bridge.verify_boundaries(args)
            self.assertFalse(cache.exists())

    def test_corrupt_matching_asset_is_not_silently_reused_or_regenerated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            _, asset = self.publish(root / 'first', cache)
            (asset / 'boundaries/runtime_cards.jsonl').write_text('{}\n', encoding='utf-8')
            args, _ = self.prepare(root / 'second', cache)
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('No network on corruption')):
                with self.assertRaisesRegex(ValueError, 'asset|checksum|changed'):
                    bridge.generate_boundaries(args)
            self.assertFalse((args.out / 'BOUNDARIES_GENERATED.json').exists())

    def test_changed_taxonomy_gets_a_new_asset_not_the_old_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            _, asset = self.publish(root / 'first', cache)
            args, _ = self.prepare(root / 'second', cache)
            tree = bridge.read(args.taxonomy)
            tree['children'][0]['children'][0]['name'] = '新齿轮'
            bridge.dump(args.taxonomy, tree)
            args.out = root / 'second/changed_mount'
            bridge.prepare(args)
            self.generate(args)
            result = bridge.read(args.out / 'BOUNDARIES_GENERATED.json')
            self.assertFalse(result['groups']['mechanical_engineering']['cache_reused'])
            bridge.verify_boundaries(args)
            self.assertEqual(len(list(cache.glob('mechanical_engineering/*/MANIFEST.json'))), 2)
            self.assertTrue((asset / 'taxonomy_enriched.json').exists())

    def test_pipeline_freezes_cache_location_for_preparation_and_generation(self):
        spec = importlib.util.spec_from_file_location('cached_pipeline', ROOT / 'pipeline.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        config, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        config['mounting']['boundaries'].update(cache_dir='{run}/../tree_assets', review_mode='off')
        tasks = pipeline.plan(config, values)[4]['tasks']
        expected = str((Path(values['run']) / '../tree_assets').resolve())
        for name in ('prepare_mounting', 'generate_mounting_boundaries'):
            command = next(task['command'] for task in tasks if task['name'] == name)
            self.assertIn('--boundary-cache-dir', command)
            self.assertEqual(command[command.index('--boundary-cache-dir') + 1], expected)

    def test_changed_generation_settings_do_not_reuse_old_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            self.publish(root / 'first', cache)
            for number, (field, value) in enumerate((
                    ('api_url', 'http://other-model-service.invalid/v1'),
                    ('boundary_context_bytes', 25000), ('max_tokens', 2048),
                    ('boundary_rewrite_rounds', 0))):
                with self.subTest(field=field):
                    args, _ = self.prepare(root / ('changed_' + str(number)), cache)
                    setattr(args, field, value)
                    self.generate(args)
                    result = bridge.read(args.out / 'BOUNDARIES_GENERATED.json')
                    self.assertFalse(result['groups']['mechanical_engineering']['cache_reused'])

    def test_extra_file_is_rejected_before_taxonomy_asset_publication(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            args, _ = self.prepare(root / 'first', cache)
            self.generate(args)
            destination = args.out / 'groups/mechanical_engineering/boundaries'
            (destination / 'credentials.txt').write_text('API_SECRET=not-a-real-key', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'coverage|unexpected|extra'):
                bridge.verify_boundaries(args)
            self.assertFalse(cache.exists())

    def test_concurrent_publication_saves_a_distinct_content_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'trees'
            args, first = self.publish(root / 'first', cache)
            target = args.out / 'groups/mechanical_engineering'
            group = bridge.frozen(args.out)['groups']['mechanical_engineering']
            tree = bridge.read(target / 'taxonomy_enriched.json')
            tree['semantic_card'] += ' Different valid generated wording.'
            bridge.dump(target / 'taxonomy_enriched.json', tree)
            generated = bridge.read(args.out / 'BOUNDARIES_GENERATED.json')['groups']['mechanical_engineering']
            report = bridge.read(args.out / 'BOUNDARIES_VERIFIED.json')['groups']['mechanical_engineering']
            second = cache_module.publish(cache, generated['cache_identity'], target, group, report)
            self.assertNotEqual(second, first)
            self.assertEqual(bridge.sha(second / 'taxonomy_enriched.json'), bridge.sha(target / 'taxonomy_enriched.json'))
            self.assertNotEqual(bridge.sha(first / 'taxonomy_enriched.json'), bridge.sha(second / 'taxonomy_enriched.json'))


if __name__ == '__main__':
    unittest.main()
