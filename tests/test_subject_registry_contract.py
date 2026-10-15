import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adapters'))
import mounting_bridge as bridge
from cleaning_handoff import seal_export
from benchmark_support import screening_scope


class SubjectRegistryContractTests(unittest.TestCase):
    def test_native_screening_display_names_resolve_to_the_same_subject(self):
        registry = bridge.read(bridge.REGISTRY)
        lookup = bridge.aliases(registry)
        for slug in registry:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                try:
                    _, scope, _ = screening_scope(ROOT, slug, Path(directory))
                except FileNotFoundError:
                    continue
                self.assertEqual(lookup.get(scope['subject_name']), slug)

    def test_passed_cleaning_proof_accepts_configured_display_names(self):
        for slug in ('education', 'electronicinformation'):
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, scope, _ = screening_scope(ROOT, slug, root)
                subject = scope['subject_name']
                tree = root / 'taxonomy.json'
                bridge.dump(tree, {'name': subject, 'children': [{'name': '基础', 'children': []}]})
                exported = root / 'cleaned'
                records = [{'id': 'record-1', 'name': '基础概念', 'definition': '原文定义',
                            'tag': subject, 'source': {'book': 'book-1'}}]
                bridge.write_rows(exported / 'records.jsonl', records)
                bridge.write_rows(exported / 'trace.jsonl', [{'id': 'record-1'}])
                evidence = root / 'evidence.json'
                bridge.dump(evidence, {'source_verified': True})
                bridge.dump(exported / 'report.json', {'records': 1,
                    **seal_export(exported, [evidence], 'dictionary', subject)})
                args = SimpleNamespace(input=[exported / 'records.jsonl'], out=root / 'mount',
                    registry=bridge.REGISTRY, taxonomy=tree, taxonomy_dir=root, subject_slug=slug,
                    require_cleaned=True, require_boundaries=False, boundary_only=False,
                    threshold=.85, min_depth=1, max_depth=5, max_related=2, limit=0,
                    api_url='http://offline.invalid/v1', model='mock', api_key_env='',
                    workers=2, timeout=10, max_tokens=1024,asset_maintenance=True)
                report = bridge.prepare(args)
                self.assertEqual(report['selected'], 1)
                self.assertEqual(set(bridge.frozen(args.out)['groups']), {slug})
                args.out = root / 'wrong_subject'
                args.subject_slug = 'mechanical_engineering'
                with self.assertRaisesRegex(ValueError, 'subject does not match target'):
                    bridge.prepare(args)
                self.assertFalse(args.out.exists())


if __name__ == '__main__':
    unittest.main()
