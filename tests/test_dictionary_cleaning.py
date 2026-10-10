"""Dictionary cleaning handoff tests; model decisions are explicit fixtures."""

import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adapters'))
import pipeline
import mounting_bridge as bridge


def load_adapter(test):
    path = ROOT / 'adapters/dictionary_cleaning.py'
    if not path.exists():
        test.fail('Dictionary cleaning export adapter is not implemented')
    spec = importlib.util.spec_from_file_location('dictionary_cleaning_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finished_fixture(root):
    input_path = root / 'INPUT.json'
    source = {'identifier': 'B001', 'book_title': 'Example', 'head_line': 1,
              'head_spans': [[2, 8]], 'body_spans': [[9, 22]]}
    original = [{'id': 'kept', 'knowledge_point': 'Energy', 'name': '',
                 'raw_content': 'Energy is ...', 'source': source,
                 'subject_slug': 'mechanical_engineering',
                 'source_context': {'trailing_context': '# Next'}},
                {'id': 'pending', 'knowledge_point': 'Next', 'source': source}]
    input_path.write_text(json.dumps(original), encoding='utf-8')
    native = root / 'native'
    native.mkdir()
    kept = {'id': 'kept', 'knowledge_point': 'Energy', 'name': '', 'definition': '',
            'en_definition': 'Energy is ...', 'description': '', 'en_description': '',
            'source': source}
    bridge.write_rows(native / 'FINAL_RECORDS.jsonl', [kept])
    bridge.write_rows(native / 'DISPOSITIONS.jsonl', [
        {'id': 'kept', 'name': {'decision': 'keep', 'api_status': 'ok'},
         'scope': {'decision': 'keep', 'api_status': 'ok'}, 'content_status': 'keep'},
        {'id': 'pending', 'name': {'decision': 'review', 'api_status': 'ok'},
         'scope': None, 'content_status': 'not_reached'}])
    bridge.dump(native / 'MANIFEST.json', {'input_sha256': bridge.sha(input_path)})
    bridge.dump(native / 'SUMMARY.json', {'input': 2, 'final_keep': 1,
                'technical_failures': 0, 'review': 1, 'source_hashes_verified': True})
    bridge.dump(native / 'STATE.json', {'status': 'finished_with_pending'})
    return input_path, native, original, kept


class DictionaryCleaningTests(unittest.TestCase):
    def test_dictionary_plan_uses_native_cleaner_without_early_normalization(self):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        stages = pipeline.plan(cfg, values)
        names = [t['name'] for t in stages[3]['tasks']]
        self.assertIn('clean_dictionary', names)
        self.assertNotIn('model_clean', names)
        self.assertNotIn('normalize_fields', names)
        cleaning = next(t for t in stages[3]['tasks'] if t['name'] == 'clean_dictionary')
        self.assertIn(str(Path(values['run']) / '03_extraction/verified/INPUT.json'), cleaning['requires'])
        self.assertIn(str(Path(values['run']) / '03_extraction/source_prepared/books.json'), cleaning['requires'])
        self.assertEqual(stages[4]['tasks'][0]['requires'][0], cleaning['produces'][0])

    def test_important_cleaning_plan_remains_unchanged(self):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.important_llm.example.json')
        tasks = pipeline.plan(cfg, values)[3]['tasks']
        self.assertEqual([t['name'] for t in tasks], ['normalize_fields', 'rules', 'model_clean', 'restore_trace'])

    def test_native_export_preserves_id_source_and_boundary_evidence(self):
        adapter = load_adapter(self)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp, native, original, kept = finished_fixture(root)
            out = root / 'export'
            report = adapter.export_records(inp, native, out, 'Mechanical', 'mechanical_engineering')
            exported = bridge.rows(out / 'records.jsonl')
            self.assertEqual(exported, [{**kept, 'tag': 'Mechanical', 'main_tags': '', 'related_tags': []}])
            trace = bridge.rows(out / 'trace.jsonl')[0]
            self.assertEqual(trace['original_record'], original[0])
            self.assertEqual(trace['cleaned_record'], kept)
            self.assertEqual(report['review'], 1)
            self.assertFalse(report['semantic_quality_approved'])
            self.assertEqual(adapter.export_records(inp, native, out, 'Mechanical', 'mechanical_engineering'), report)
            from cleaning_handoff import verify_export
            verify_export(out / 'records.jsonl')
            (native / 'STATE.json').unlink()
            with self.assertRaisesRegex(ValueError, 'cleaning'):
                verify_export(out / 'records.jsonl')
            bridge.dump(native / 'STATE.json', {'status': 'finished_with_pending'})
            (out / 'records.jsonl').write_text('[]\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                adapter.export_records(inp, native, out, 'Mechanical', 'mechanical_engineering')

    def test_pending_or_forged_native_record_cannot_enter_export(self):
        adapter = load_adapter(self)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp, native, _, kept = finished_fixture(root)
            bridge.write_rows(native / 'FINAL_RECORDS.jsonl', [{**kept, 'id': 'pending'}])
            with self.assertRaisesRegex(ValueError, 'passed'):
                adapter.export_records(inp, native, root / 'export', 'Mechanical', 'mechanical_engineering')

    def test_native_content_trace_is_audited_without_changing_export_source(self):
        adapter = load_adapter(self)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp, native, original, kept = finished_fixture(root)
            kept['source'] = {**kept['source'],
                'content_trace': {'en_definition': {'raw_body_spans': [[0, 13]], 'joiners': []}},
                'content_trace_basis': 'raw_content in frozen cleaning input, zero-based Unicode offsets; reconstruct via body_locations'}
            bridge.write_rows(native / 'FINAL_RECORDS.jsonl', [kept])
            adapter.export_records(inp, native, root / 'export', 'Mechanical', 'mechanical_engineering')
            self.assertEqual(bridge.rows(root / 'export/records.jsonl')[0]['source'], original[0]['source'])
            self.assertEqual(bridge.rows(root / 'export/trace.jsonl')[0]['cleaned_record']['source'], kept['source'])

    def test_content_trace_does_not_allow_other_source_changes(self):
        adapter = load_adapter(self)
        for change in ({'identifier': 'OTHER'}, {'body_spans': [[9, 23]]}, {'unexpected': 'value'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                inp, native, _, kept = finished_fixture(root)
                kept['source'] = {**kept['source'], **change, 'content_trace': {},
                    'content_trace_basis': 'raw_content in frozen cleaning input, zero-based Unicode offsets; reconstruct via body_locations'}
                bridge.write_rows(native / 'FINAL_RECORDS.jsonl', [kept])
                with self.assertRaisesRegex(ValueError, 'source identity'):
                    adapter.export_records(inp, native, root / 'export', 'Mechanical', 'mechanical_engineering')

    def test_running_native_cleaner_and_changed_input_are_not_exported(self):
        adapter = load_adapter(self)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp, native, _, _ = finished_fixture(root)
            bridge.dump(native / 'STATE.json', {'status': 'running'})
            with self.assertRaisesRegex(ValueError, 'finished'):
                adapter.export_records(inp, native, root / 'export', 'Mechanical', 'mechanical_engineering')
            bridge.dump(native / 'STATE.json', {'status': 'finished_with_pending'})
            inp.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'input'):
                adapter.export_records(inp, native, root / 'export', 'Mechanical', 'mechanical_engineering')

    def test_unverified_source_body_fails_before_model_execution(self):
        adapter = load_adapter(self)
        sys.path.insert(0, str(ROOT / 'modules/dictionary'))
        import portable_pipeline as native
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = root / 'book.md'
            md.write_bytes(b'# Energy\nBody.\n')
            scope = root / 'scope.json'
            scope.write_text('{}')
            books = root / 'books.json'
            bridge.dump(books, [{'identifier': 'B001', 'title': 'Example', 'md_path': str(md),
                'subject_slug': 'mechanical_engineering', 'scope_config': str(scope)}])
            inp = root / 'INPUT.json'
            row = {'id': 'entry', 'subject_slug': 'mechanical_engineering', 'knowledge_point': 'Energy',
                   'raw_content': 'Invented body', 'source': {'identifier': 'B001', 'md_path': str(md),
                       'md_sha256': bridge.sha(md), 'head_spans': [[2, 8]], 'body_spans': [[9, 14]]}}
            bridge.dump(inp, [row])
            argv = ['dictionary_cleaning.py', '--input', str(inp), '--books', str(books),
                    '--out', str(root / 'native'), '--export', str(root / 'export'), '--subject', 'Mechanical',
                    '--slug', 'mechanical_engineering', '--api-url', 'http://offline.invalid', '--model', 'fixture']
            with patch.object(sys, 'argv', argv), patch.object(native, 'run_cleaning',
                    side_effect=AssertionError('Model execution must not be reached')):
                with self.assertRaisesRegex(ValueError, 'source'):
                    adapter.main()

    def test_mounting_plan_accepts_both_cleaned_inputs(self):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        sources = ['{run}/04_cleaning/export/records.jsonl', '{root}/runs/important/04_cleaning/export/records.jsonl']
        cfg['mounting']['inputs'] = sources
        tasks = pipeline.plan(cfg, values)[4]['tasks']
        expected = [str(Path(s.format_map(values))) for s in sources]
        prepare = tasks[0]
        self.assertTrue(all(p in prepare['requires'] for p in expected))
        command = prepare['command']
        self.assertIn('--require-cleaned', command)
        self.assertEqual([command[i+1] for i, arg in enumerate(command) if arg == '--input'], expected)

    def test_invalid_mounting_input_list_is_rejected(self):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        for inputs in ('not-a-list', [], ['relative/file.jsonl'], ['{run}/x', '{run}/x']):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                cfg['mounting']['inputs'] = inputs
                pipeline.plan(cfg, values)

    def test_native_cli_runs_stages_in_order_and_preserves_context(self):
        adapter = load_adapter(self)
        sys.path.insert(0, str(ROOT / 'modules/dictionary'))
        import portable_pipeline as native
        # The real CLI runs in a child process, without the root scheduler in sys.modules.
        legacy_path = ROOT / 'modules/dictionary/src/cleaning_legacy/pipeline.py'
        spec = importlib.util.spec_from_file_location('dictionary_legacy_for_test', legacy_path)
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        with patch.dict(sys.modules, {'pipeline': legacy}):
            import clean_boundary_v46 as content
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = root / 'book.md'
            md.write_bytes(b'# Energy\nEnergy is a quantity.\n# Next\n')
            scope = root / 'scope.json'
            scope.write_text('{}')
            books = root / 'books.json'
            bridge.dump(books, [{'identifier': 'B001', 'title': 'Example', 'md_path': str(md),
                        'subject_slug': 'mechanical_engineering', 'scope_config': str(scope)}])
            inp = root / 'INPUT.json'
            source = {'identifier': 'B001', 'book_title': 'Example', 'md_path': str(md),
                      'md_sha256': bridge.sha(md), 'head_spans': [[2, 8]], 'body_spans': [[9, 30]]}
            original = {'id': 'energy', 'knowledge_point': 'Energy', 'name': '', 'raw_content': 'Energy is a quantity.',
                        'subject_slug': 'mechanical_engineering', 'source': source,
                        'source_context': {'trailing_context': '# Next'}}
            rejected = {**original, 'id': 'bad'}
            bridge.dump(inp, [original, rejected])
            bridge.dump(root / 'MANIFEST.json', {'kind': 'clean_preparation', 'books': bridge.read(books),
                        'source_hashes': native.source_hashes(bridge.read(books)), 'extraction_files': {}})
            bridge.dump(root / 'PREPARED.json', {'input_sha256': bridge.sha(inp)})
            calls = []
            def filter_fixture(values, stage, *args):
                calls.append((stage, [r['id'] for r in values]))
                key = native.NAME if stage == 'name' else native.SCOPE
                return [{**r, key: {'decision': 'drop' if r['id'] == 'bad' else 'keep', 'api_status': 'ok'}} for r in values]
            def content_fixture(values, *args):
                calls.append(('content', [r['id'] for r in values]))
                self.assertEqual(values[0]['source_context']['trailing_context'], '# Next')
                return [{'id': r['id'], 'status': 'keep', 'record': {
                    'id': r['id'], 'knowledge_point': r['knowledge_point'], 'name': '', 'source': r['source'],
                    'definition': '', 'en_definition': r['raw_content'], 'description': '', 'en_description': ''}} for r in values]
            argv = ['dictionary_cleaning.py', '--input', str(inp), '--books', str(books),
                    '--out', str(root / 'native'), '--export', str(root / 'export'), '--subject', 'Mechanical',
                    '--slug', 'mechanical_engineering', '--api-url', 'http://offline.invalid', '--model', 'fixture']
            with patch.dict(sys.modules, {'pipeline': legacy, 'clean_boundary_v46': content}), \
                    patch.object(sys, 'argv', argv), patch.object(native.urllib.request, 'urlopen',
                    return_value=io.BytesIO(b'{"data":[{"id":"fixture"}]}')), \
                    patch.object(native, 'filter_stage', side_effect=filter_fixture), \
                    patch.object(content.Runner, 'batch', side_effect=content_fixture):
                self.assertEqual(adapter.main(), 0)
            self.assertEqual(calls, [('name', ['energy', 'bad']), ('scope', ['energy']), ('content', ['energy'])])
            output = bridge.rows(root / 'export/records.jsonl')
            self.assertEqual([r['id'] for r in output], ['energy'])
            self.assertEqual(output[0]['source'], source)
            altered = [original, rejected]
            altered[0]['source_context']['head_window'] = 'Invented evidence'
            bridge.dump(inp, altered)
            with patch.object(sys, 'argv', argv), patch.object(native, 'run_cleaning',
                    side_effect=AssertionError('Unverified context must not reach model execution')):
                with self.assertRaisesRegex(ValueError, 'snapshot'):
                    adapter.main()

    def test_multi_input_custom_hook_is_not_silently_single_input(self):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        cfg['mounting']['inputs'] = ['{root}/first.jsonl', '{root}/second.jsonl']
        cfg['hooks']['mounting'] = [['echo', 'fixture']]
        cfg['mounting']['boundaries'] = {'enabled': False}
        with self.assertRaisesRegex(ValueError, 'custom hook'):
            pipeline.plan(cfg, values)


if __name__ == '__main__':
    unittest.main()
