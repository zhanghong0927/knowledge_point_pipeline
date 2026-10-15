import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adapters'))
import cleaning_handoff as handoff
import normalize_records as normalize
import mounting_bridge
import pipeline


def dump(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def rows(path, values):
    path.write_text(''.join(json.dumps(v) + '\n' for v in values), encoding='utf-8')


class CleaningHandoffTests(unittest.TestCase):
    def invoke(self, argv):
        with patch.object(sys, 'argv', ['normalize_records.py', *argv]), contextlib.redirect_stdout(io.StringIO()):
            normalize.main()

    def fixture(self, root, source='Book-A'):
        raw = root / 'raw.jsonl'
        rows(raw, [{'id': 'keep', 'knowledge_point': 'Energy', 'source': source},
                   {'id': 'pending', 'knowledge_point': 'Power', 'source': source}])
        initial = root / 'normalized'
        self.invoke(['--input', str(raw), '--out', str(initial), '--format', 'important',
                     '--subject', 'Mechanical', '--slug', 'mechanical'])
        values = list(normalize.records(initial / 'records.jsonl'))
        upstream = root / 'rule_pass.jsonl'
        rows(upstream, [{**r, 'cleaning_record_id': 'key-' + r['id']} for r in values])
        judgments = root / 'judgments.jsonl'
        vote = {'decision': 'keep', **{field: values[0][field] for field in normalize.TEXT_FIELDS}}
        rows(judgments, [{'cleaning_record_id': 'key-keep', 'model_cleaning': vote},
                         {'cleaning_record_id': 'key-pending', 'model_cleaning': {'decision': 'review'}}])
        clean = root / 'clean.jsonl'
        rows(clean, [{key: value for key, value in values[0].items() if key != '_normalization_record_sha256'}])
        dump(root / 'model_report.json', {'stage': 'model_clean', 'subject': 'Mechanical',
             'finished_at': 'fixture', 'input': str(upstream),
             'outputs': {'clean': str(clean), 'judgments': str(judgments)},
             'counts': {'keep': 1, 'review': 1}})
        return initial

    def restore(self, root, initial_proof=True, strict=True, **scope):
        argv = ['--restore', '--input', str(root / 'clean.jsonl'),
                '--trace', str(root / 'normalized/trace.jsonl'), '--out', str(root / 'export'),
                '--subject', scope.get('subject', 'Mechanical'), '--slug', scope.get('slug', 'mechanical')]
        if strict:
            argv.extend(['--cleaning-report', str(root / 'model_report.json')])
        if strict and initial_proof:
            argv.extend(['--normalization-report', str(root / 'normalized/report.json')])
        self.invoke(argv)

    def refresh_trace_hash(self, initial):
        report = json.loads((initial / 'report.json').read_text())
        report.setdefault('output_sha256', {})['trace.jsonl'] = handoff.sha(initial / 'trace.jsonl')
        dump(initial / 'report.json', report)

    def legacy_important_export(self, root):
        export = root / 'export'
        export.mkdir()
        rows(export / 'records.jsonl', [{'id': 'keep', 'knowledge_point': 'Energy',
                                        'source': 'Book-B', 'tag': 'Mechanical'}])
        rows(export / 'trace.jsonl', [{'id': 'keep', 'original_record': {'source': 'Book-B'}}])
        evidence = root / 'old_cleaning_evidence.json'
        dump(evidence, {'status': 'finished'})
        dump(export / 'report.json', {'records': 1,
            'cleaning_export': {'version': 1, 'track': 'important', 'subject': 'Mechanical',
                'passed_only': True, 'evidence_sha256': {str(evidence.resolve()): handoff.sha(evidence)}},
            'output_sha256': {name: handoff.sha(export / name) for name in ('records.jsonl', 'trace.jsonl')}})
        return export

    def mounting_args(self, root):
        registry = root / 'registry.json'
        dump(registry, {'mechanical': {'name': 'Mechanical', 'file': ''}})
        return SimpleNamespace(input=root / 'export/records.jsonl', out=root / 'mount', registry=registry,
            subject_slug='mechanical', taxonomy=None, taxonomy_dir=root, require_cleaned=True,
            limit=0, threshold=.85, min_depth=2, max_depth=5, max_related=2,asset_maintenance=True)

    def test_legacy_important_proof_with_matching_hashes_requires_new_export(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            export = self.legacy_important_export(root)
            with self.assertRaisesRegex(ValueError, 'normalization.*new.*directory'):
                handoff.verify_export(export / 'records.jsonl')

    def test_legacy_important_proof_cannot_enter_mounting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.legacy_important_export(root)
            args = self.mounting_args(root)
            with self.assertRaisesRegex(ValueError, 'normalization.*new.*directory'):
                mounting_bridge.prepare(args)
            self.assertFalse(args.out.exists())

    def test_dictionary_v1_proof_remains_compatible(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            export = self.legacy_important_export(root)
            report = json.loads((export / 'report.json').read_text())
            report['cleaning_export']['track'] = 'dictionary'
            dump(export / 'report.json', report)
            self.assertEqual(handoff.verify_export(export / 'records.jsonl')['cleaning_export']['version'], 1)

    def test_new_important_proof_declares_initial_binding_and_enters_mounting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            self.restore(root)
            proof = handoff.verify_export(root / 'export/records.jsonl')['cleaning_export']
            self.assertEqual(proof['version'], 3)
            self.assertEqual(proof['source_binding'], {'contract': 'initial_normalization', 'version': 2,
                             'record_fingerprint_field': '_normalization_record_sha256',
                             'normalization_report': str((initial / 'report.json').resolve()),
                             'cleaning_report': str((root / 'model_report.json').resolve())})
            self.assertEqual(mounting_bridge.prepare(self.mounting_args(root))['selected'], 1)

    def test_strict_restore_rejects_complete_valid_same_label_proof_swap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b = root / 'A', root / 'B'
            a.mkdir()
            b.mkdir()
            source_a = {'book_title': 'Shared Title', 'identifier': 'A', 'head_spans': [[1, 4]]}
            source_b = {'book_title': 'Shared Title', 'identifier': 'B', 'head_spans': [[8, 11]]}
            initial_a = self.fixture(a, source_a)
            initial_b = self.fixture(b, source_b)
            # Both complete proofs are generated normally and work with their own cleaning results.
            for folder, source in ((a, source_a), (b, source_b)):
                self.restore(folder)
                handoff.verify_export(folder / 'export/records.jsonl')
                self.assertEqual(list(normalize.records(folder / 'export/records.jsonl'))[0]['source'], source)
            first = list(normalize.records(initial_a / 'records.jsonl'))[0]
            second = list(normalize.records(initial_b / 'records.jsonl'))[0]
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(first['source'], 'Shared Title')
            self.assertEqual(first['source'], second['source'])
            self.assertEqual({f: first[f] for f in normalize.TEXT_FIELDS},
                             {f: second[f] for f in normalize.TEXT_FIELDS})
            self.assertNotEqual(first['_normalization_record_sha256'], second['_normalization_record_sha256'])
            out = root / 'swapped'
            with self.assertRaisesRegex(ValueError, 'fingerprint|record binding'):
                self.invoke(['--restore', '--input', str(a / 'clean.jsonl'),
                    '--cleaning-report', str(a / 'model_report.json'),
                    '--normalization-report', str(initial_b / 'report.json'),
                    '--trace', str(initial_b / 'trace.jsonl'), '--out', str(out),
                    '--subject', 'Mechanical', '--slug', 'mechanical'])
            self.assertFalse((out / 'report.json').exists())

    def test_normalized_record_fingerprint_covers_full_source_and_scope(self):
        raw = {'id': 'same', 'knowledge_point': 'Energy',
               'source': {'book_title': 'Shared Title', 'identifier': 'A', 'head_spans': [[1, 4]]}}
        normalized, _ = normalize.standardize(raw, 'important', 'Mechanical', 'mechanical', 1)
        self.assertIn('_normalization_record_sha256', normalized)
        encoded = json.dumps({'original_record': raw, 'format': 'important',
                              'subject_slug': 'mechanical', 'input_line': 1},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        fingerprint = hashlib.sha256(encoded).hexdigest()
        self.assertEqual(normalized['_normalization_record_sha256'], fingerprint)
        reordered = {'source': dict(reversed(list(raw['source'].items()))),
                     'knowledge_point': 'Energy', 'id': 'same'}
        reordered_row, _ = normalize.standardize(reordered, 'important', 'Mechanical', 'mechanical', 1)
        self.assertEqual(reordered_row['_normalization_record_sha256'], fingerprint)
        changes = [({**raw, 'source': {**raw['source'], 'identifier': 'B'}}, 'important', 'mechanical', 1),
                   ({**raw, 'source': {**raw['source'], 'head_spans': [[8, 11]]}}, 'important', 'mechanical', 1),
                   (raw, 'standard', 'mechanical', 1), (raw, 'important', 'other', 1),
                   (raw, 'important', 'mechanical', 2)]
        for original, mode, slug, line in changes:
            with self.subTest(mode=mode, slug=slug, line=line, source=original['source']):
                changed, _ = normalize.standardize(original, mode, 'Mechanical', slug, line)
                self.assertNotEqual(changed['_normalization_record_sha256'], fingerprint)

    def test_strict_restore_rejects_missing_or_wrong_upstream_fingerprint(self):
        for fingerprint in (None, '0' * 64):
            with self.subTest(fingerprint=fingerprint), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                upstream = list(normalize.records(root / 'rule_pass.jsonl'))
                for row in upstream:
                    if fingerprint is None:
                        row.pop('_normalization_record_sha256', None)
                    else:
                        row['_normalization_record_sha256'] = fingerprint
                rows(root / 'rule_pass.jsonl', upstream)
                with self.assertRaisesRegex(ValueError, 'fingerprint.*normalize.*clean.*new.*directory'):
                    self.restore(root)

    def test_pre_fingerprint_important_v2_proof_cannot_enter_mounting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            self.restore(root)
            report = json.loads((root / 'export/report.json').read_text())
            proof = report['cleaning_export']
            proof['version'] = 2
            proof['source_binding']['version'] = 1
            proof['source_binding'].pop('record_fingerprint_field', None)
            proof['source_binding'].pop('cleaning_report', None)
            dump(root / 'export/report.json', report)
            args = self.mounting_args(root)
            with self.assertRaisesRegex(ValueError, 'normalization.*new.*directory'):
                mounting_bridge.prepare(args)
            self.assertFalse(args.out.exists())

    def test_consumer_checks_upstream_fingerprint_not_only_current_byte_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            self.restore(root)
            upstream_path = root / 'rule_pass.jsonl'
            upstream = list(normalize.records(upstream_path))
            for row in upstream:
                row.pop('_normalization_record_sha256', None)
            rows(upstream_path, upstream)
            report = json.loads((root / 'export/report.json').read_text())
            report['cleaning_export']['evidence_sha256'][str(upstream_path.resolve())] = handoff.sha(upstream_path)
            dump(root / 'export/report.json', report)
            with self.assertRaisesRegex(ValueError, 'fingerprint|record binding'):
                handoff.verify_export(root / 'export/records.jsonl')

    def test_restore_does_not_publish_internal_fingerprint(self):
        for strict in (False, True):
            with self.subTest(strict=strict), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                clean = list(normalize.records(root / 'clean.jsonl'))
                clean[0]['_normalization_record_sha256'] = '0' * 64
                rows(root / 'clean.jsonl', clean)
                self.restore(root, strict=strict)
                published = list(normalize.records(root / 'export/records.jsonl'))[0]
                self.assertNotIn('_normalization_record_sha256', published)
                self.assertEqual(published['source'], 'Book-A')

    def test_new_important_proof_rejects_missing_or_unbound_normalization_pointer(self):
        for changed in ('missing_binding', 'missing_report_hash', 'missing_trace_hash', 'missing_cleaning_report_hash'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                initial = self.fixture(root)
                self.restore(root)
                report = json.loads((root / 'export/report.json').read_text())
                proof = report['cleaning_export']
                if changed == 'missing_binding':
                    proof.pop('source_binding', None)
                elif changed == 'missing_cleaning_report_hash':
                    proof['evidence_sha256'].pop(str((root / 'model_report.json').resolve()))
                else:
                    name = 'report.json' if changed == 'missing_report_hash' else 'trace.jsonl'
                    proof['evidence_sha256'].pop(str((initial / name).resolve()))
                dump(root / 'export/report.json', report)
                with self.assertRaisesRegex(ValueError, 'source binding|normalization'):
                    handoff.verify_export(root / 'export/records.jsonl')

    def test_initial_report_binds_source_outputs_and_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            report = json.loads((initial / 'report.json').read_text())
            self.assertIn('input', report)
            self.assertEqual(report['input'], str((root / 'raw.jsonl').resolve()))
            self.assertEqual(report['input_sha256'], handoff.sha(root / 'raw.jsonl'))
            self.assertEqual(report['normalization'], {'version': 2, 'format': 'important',
                             'subject': 'Mechanical', 'subject_slug': 'mechanical',
                             'record_fingerprint_field': '_normalization_record_sha256'})
            self.assertEqual(report['output_sha256'], {
                name: handoff.sha(initial / name) for name in ('records.jsonl', 'trace.jsonl')})

    def test_strict_restore_rejects_book_a_to_book_b_trace_swap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            trace = list(normalize.records(initial / 'trace.jsonl'))
            trace[0]['original_record']['source'] = 'Book-B'
            rows(initial / 'trace.jsonl', trace)
            with self.assertRaisesRegex(ValueError, 'trace|normalization'):
                self.restore(root)
            self.assertFalse((root / 'export/report.json').exists())

    def test_strict_restore_rejects_same_label_different_source_dict(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root, {'book_title': 'Book', 'identifier': 'A', 'head_spans': [[1, 4]]})
            trace = list(normalize.records(initial / 'trace.jsonl'))
            trace[0]['original_record']['source']['identifier'] = 'B'
            rows(initial / 'trace.jsonl', trace)
            with self.assertRaisesRegex(ValueError, 'trace|normalization'):
                self.restore(root)

    def test_strict_restore_rejects_changed_original_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root, {'book_title': 'Book', 'identifier': 'A'})
            raw = list(normalize.records(root / 'raw.jsonl'))
            raw[0]['source']['identifier'] = 'B'
            rows(root / 'raw.jsonl', raw)
            with self.assertRaisesRegex(ValueError, 'input|normalization'):
                self.restore(root)

    def test_strict_restore_rejects_changed_normalized_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            values = list(normalize.records(initial / 'records.jsonl'))
            values[0]['source'] = 'Book-B'
            rows(initial / 'records.jsonl', values)
            with self.assertRaisesRegex(ValueError, 'records|normalization'):
                self.restore(root)

    def test_strict_restore_rejects_duplicate_trace_ids_even_with_updated_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            trace = list(normalize.records(initial / 'trace.jsonl'))
            rows(initial / 'trace.jsonl', [*trace, trace[0]])
            self.refresh_trace_hash(initial)
            with self.assertRaisesRegex(ValueError, 'Duplicate|duplicate|coverage'):
                self.restore(root)

    def test_strict_restore_rejects_trace_scope_or_id_mismatch(self):
        for field, value in (('id', 'other'), ('subject', 'Other'), ('subject_slug', 'other')):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                initial = self.fixture(root)
                trace = list(normalize.records(initial / 'trace.jsonl'))
                trace[0][field] = value
                rows(initial / 'trace.jsonl', trace)
                self.refresh_trace_hash(initial)
                with self.assertRaisesRegex(ValueError, 'scope|coverage|trace|Subject'):
                    self.restore(root)

    def test_strict_restore_rejects_trace_rewrite_even_with_updated_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root, {'book_title': 'Book', 'identifier': 'A'})
            trace = list(normalize.records(initial / 'trace.jsonl'))
            trace[0]['original_record']['source']['identifier'] = 'B'
            rows(initial / 'trace.jsonl', trace)
            self.refresh_trace_hash(initial)
            with self.assertRaisesRegex(ValueError, 'trace|original'):
                self.restore(root)

    def test_strict_restore_rejects_foreign_upstream_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            upstream = list(normalize.records(root / 'rule_pass.jsonl'))
            upstream[0]['id'] = 'foreign'
            rows(root / 'rule_pass.jsonl', upstream)
            clean = list(normalize.records(root / 'clean.jsonl'))
            clean[0]['id'] = 'foreign'
            rows(root / 'clean.jsonl', clean)
            with self.assertRaisesRegex(ValueError, 'upstream ID.*initial normalization'):
                self.restore(root)

    def test_strict_restore_rejects_upstream_source_swap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            upstream = list(normalize.records(root / 'rule_pass.jsonl'))
            upstream[0]['source'] = 'Book-B'
            rows(root / 'rule_pass.jsonl', upstream)
            with self.assertRaisesRegex(ValueError, 'upstream|source'):
                self.restore(root)

    def test_strict_restore_requires_initial_proof_with_migration_message(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'normalize.*new.*directory'):
                self.restore(root, initial_proof=False)

    def test_strict_restore_rejects_legacy_initial_report_with_migration_message(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            dump(initial / 'report.json', {'records': 2, 'subject': 'Mechanical', 'input_sha256': handoff.sha(root / 'raw.jsonl')})
            with self.assertRaisesRegex(ValueError, 'normalize.*new.*directory'):
                self.restore(root)

    def test_strict_restore_rejects_pre_fingerprint_normalization_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            report = json.loads((initial / 'report.json').read_text())
            report['normalization']['version'] = 1
            report['normalization'].pop('record_fingerprint_field')
            dump(initial / 'report.json', report)
            with self.assertRaisesRegex(ValueError, 'normalize.*clean.*new.*directory'):
                self.restore(root)

    def test_strict_restore_reports_malformed_proof_as_validation_error(self):
        for field, value in (('normalization', None), ('output_sha256', ['records.jsonl', 'trace.jsonl'])):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                initial = self.fixture(root)
                report = json.loads((initial / 'report.json').read_text())
                report[field] = value
                dump(initial / 'report.json', report)
                try:
                    self.restore(root)
                except Exception as exc:
                    self.assertIsInstance(exc, ValueError)
                    self.assertRegex(str(exc), 'normalize.*new.*directory')
                else:
                    self.fail('Malformed initial proof must not produce a strict export')

    def test_strict_restore_rejects_initial_scope_mismatch(self):
        for scope in ({'subject': 'Other'}, {'slug': 'other'}):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                with self.assertRaisesRegex(ValueError, 'normalization scope'):
                    self.restore(root, **scope)

    def test_standalone_restore_remains_compatible_without_initial_proof(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            self.restore(root, strict=False)
            published = list(normalize.records(root / 'export/records.jsonl'))
            self.assertEqual(published[0]['source'], 'Book-A')
            self.assertNotIn('cleaning_export', json.loads((root / 'export/report.json').read_text()))

    def test_important_restore_binds_only_native_keep_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = {'book_title': 'Book', 'identifier': 'A', 'head_spans': [[1, 4]], 'metadata': {'edition': 2}}
            initial = self.fixture(root, source)
            self.restore(root)
            proof = handoff.verify_export(root / 'export/records.jsonl')
            self.assertEqual(proof['cleaning_export']['track'], 'important')
            value = json.loads((root / 'export/records.jsonl').read_text())
            self.assertEqual(value['source'], source)
            self.assertEqual(value['id'], 'keep')
            bound = proof['cleaning_export']['evidence_sha256']
            for path in (root / 'raw.jsonl', initial / 'report.json', initial / 'records.jsonl', initial / 'trace.jsonl'):
                self.assertEqual(bound[str(path.resolve())], handoff.sha(path))
            clean, report = root / 'clean.jsonl', root / 'model_report.json'
            rows(clean, [{'id': 'pending', 'name': 'Energy'}])
            with self.assertRaisesRegex(ValueError, 'keep'):
                normalize.verify_model_keep(clean, report, 'Mechanical')
            with self.assertRaisesRegex(ValueError, 'changed'):
                handoff.verify_export(root / 'export/records.jsonl')

    def test_strict_restore_preserves_string_source_and_seals_initial_evidence(self):
        for changed in ('raw.jsonl', 'normalized/records.jsonl', 'normalized/trace.jsonl', 'normalized/report.json'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.fixture(root)
                self.restore(root)
                value = list(normalize.records(root / 'export/records.jsonl'))[0]
                self.assertEqual(value['source'], 'Book-A')
                handoff.verify_export(root / 'export/records.jsonl')
                with (root / changed).open('a', encoding='utf-8') as stream:
                    stream.write(' ')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    handoff.verify_export(root / 'export/records.jsonl')

    def test_strict_restore_rejects_initial_evidence_changed_while_sealing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial = self.fixture(root)
            def changed_before_seal(*args, **kwargs):
                with (initial / 'trace.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(' ')
                return handoff.seal_export(*args, **kwargs)
            with patch.object(normalize, 'seal_export', side_effect=changed_before_seal):
                with self.assertRaisesRegex(ValueError, 'normalization evidence changed'):
                    self.restore(root)
            self.assertFalse((root / 'export/report.json').exists())

    @unittest.skipIf(sys.platform == 'win32', 'Scheduler locks require Linux fcntl')
    def test_resume_validates_cleaning_evidence_before_skip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / 'config.json'
            dump(cfg, {})
            export = root / 'export'
            export.mkdir()
            rows(export / 'records.jsonl', [{'id': '1', 'name': 'Energy'}])
            rows(export / 'trace.jsonl', [{'id': '1'}])
            evidence = root / 'STATE.json'
            dump(evidence, {'status': 'finished'})
            dump(export / 'report.json', {'records': 1, **handoff.seal_export(export, [evidence], 'dictionary', 'Mechanical')})
            task = {'name': 'clean_dictionary', 'command': ['fixture'], 'produces': [str(export / 'records.jsonl')],
                    'requires': [], 'cwd': str(root), 'env': {}, 'blocked': None}
            signature = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
            dump(root / 'pipeline_state.json', {'04.clean_dictionary': {'status': 'completed', 'signature': signature}})
            stages = [{'stage': '04', 'tasks': [task]}]
            pipeline.run_stages(cfg, {}, {'run': str(root)}, stages, True)
            dump(evidence, {'status': 'running'})
            with self.assertRaisesRegex(ValueError, 'changed'), patch.object(pipeline.subprocess, 'run') as call:
                pipeline.run_stages(cfg, {}, {'run': str(root)}, stages, True)
            call.assert_not_called()
