"""Offline recovery must preserve identities, journals and real round numbers."""
import contextlib
import copy
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'modules/dictionary'))
import portable_pipeline as native

try:
    import resume_integrity
except ImportError:
    resume_integrity = None


def isolated_dictionary_content():
    """Legacy native modules use the name pipeline; preserve the root ledger module."""
    with patch.dict(sys.modules), patch.object(sys, 'path', [str(native.ROOT / 'src'), *sys.path]):
        sys.modules.pop('pipeline', None)
        import clean_boundary_v46
        return clean_boundary_v46


class CheckpointResumeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        md, scope = self.root / 'book.md', self.root / 'scope.json'
        md.write_text('A body', encoding='utf-8')
        scope.write_text('{}', encoding='utf-8')
        self.books = [{'identifier': 'B001', 'title': 'Test', 'md_path': str(md),
                       'scope_config': str(scope)}]
        self.input = self.root / 'INPUT.json'
        self.rows = [{'id': key, 'knowledge_point': key.upper(), 'name': '',
                      'raw_content': 'A body', 'source': {'identifier': 'B001'}} for key in ('a', 'b')]
        native.write(self.input, self.rows)
        self.cfg = {'api_url': 'http://127.0.0.1:1', 'model': 'test', 'workers': 2,
                    'context_limit': 32768, 'no_auth': True}
        self.source = self.root / 'frozen' / 'dictionary'
        native.write(self.source / 'MANIFEST.json', native.staged_manifest(self.input, self.cfg, self.books))
        votes = [{**row, native.NAME: {'decision': 'review', 'api_status': 'api_error',
                                      'failure_kind': 'transport'}} for row in self.rows]
        native.jsonl(self.source / 'name/FIRST.jsonl', votes)
        native.jsonl(self.source / 'name/FINAL.jsonl', votes)
        previous = {'state': 'awaiting_retry', 'status': 'awaiting_retry', 'round': 1,
                    'next_round': 2, 'checkpoint_hashes': native.staged_checkpoint_hashes(self.source)}
        native.write(self.source / 'ORCHESTRATION_STATUS.json', previous)
        native.write(self.source / 'STATE.json', {'status': 'round_running', 'round': 2})
        native.jsonl(self.source / 'name/round_2/input.jsonl', self.rows)
        native.jsonl(self.source / 'name/round_2/llm_name_title_format_judgments.jsonl', [
            {'id': 'a', 'decision': 'keep', 'api_status': 'ok', 'policy_version': 'test'}])
        self.freeze = self.root / 'FREEZE_COMPLETE.json'
        self.book_proof = self.root / 'BOOK_CHECKPOINTS.json'
        native.write(self.freeze, {'state': 'frozen', 'scope': 'dictionary_only',
            'root': str(self.source.parent), 'gateway_drained': True,
            'processes': [{'state': 'T', 'pid': 123}]})
        native.write(self.book_proof, [{'out': str(self.source), 'files': {
            name: {'sha256': native.digest(self.source / name)}
            for name in ('STATE.json', 'ORCHESTRATION_STATUS.json')}}])

    def tearDown(self):
        self.temp.cleanup()

    def module(self):
        self.assertIsNotNone(resume_integrity, 'offline checkpoint inspection and migration are required')
        return resume_integrity

    def migrate(self):
        module = self.module()
        target = self.root / 'new_attempt' / 'dictionary'
        config = dict(self.cfg, api_url='http://127.0.0.1:2')
        receipt = module.migrate_checkpoint(self.source, target, self.input, config, self.books, native,
            freeze_receipt=self.freeze, book_checkpoints=self.book_proof)
        return target, config, receipt

    def test_interrupted_round_requires_stable_prefix_and_current_request_journal(self):
        report = self.module().inspect_checkpoint(self.source, self.input, native)
        self.assertIs(report['round_complete'], False)
        self.assertEqual(report['resume_round'], 2)
        self.assertEqual(report['completed_round'], 1)
        self.assertEqual(report['journal_records'], 1)
        self.assertIn('STATE.json', report['files'])
        self.assertIn('name/round_2/input.jsonl', report['files'])

    def test_changed_completed_prefix_is_refused_before_migration(self):
        (self.source / 'name/FIRST.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'prefix|checkpoint'):
            self.module().inspect_checkpoint(self.source, self.input, native)

    def test_torn_or_foreign_journal_is_refused(self):
        path = self.source / 'name/round_2/llm_name_title_format_judgments.jsonl'
        for data in ('{"id":"a"', '{"id":"foreign","api_status":"ok"}\n'):
            with self.subTest(data=data):
                path.write_text(data, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.module().inspect_checkpoint(self.source, self.input, native)

    def test_independent_copy_seals_old_and_new_manifest_without_source_writes(self):
        module = self.module()
        before = module.snapshot_files(self.source)
        target, config, receipt = self.migrate()
        self.assertEqual(module.snapshot_files(self.source), before)
        self.assertEqual((target / 'name/round_2/input.jsonl').read_bytes(),
                         (self.source / 'name/round_2/input.jsonl').read_bytes())
        self.assertEqual(native.read(target / 'MANIFEST.json')['config']['api_url'], config['api_url'])
        observed = native.read(receipt)
        self.assertIs(observed['round_complete'], False)
        self.assertEqual(observed['old_manifest']['input_sha256'], observed['new_manifest']['input_sha256'])
        self.assertIs(observed['semantic_quality_approved'], False)
        module.verify_resume(target, receipt, self.input, config, self.books, native, 2)
        (target / 'name/round_2/input.jsonl').write_text('[]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed|hash'):
            module.verify_resume(target, receipt, self.input, config, self.books, native, 2)

    def test_migrated_interrupted_round_can_be_reinspected_after_gateway_rebinding(self):
        target, config, receipt = self.migrate()
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_controller_rebind', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        command = ['python', '--api-url', config['api_url']]
        job = {'out': str(target), 'input': str(self.input), 'books': str(self.root / 'books.json'),
               'job_id': 'test', 'resume_receipt': str(receipt), 'command': command}
        native.write(job['books'], self.books)
        for port in (123, 456):
            gateway = types.SimpleNamespace(job_url=lambda _job: f'http://127.0.0.1:{port}')
            controller._rebind_resumed_job(job, gateway)
            inspection = self.module().inspect_checkpoint(target, self.input, native)
            self.assertEqual(inspection['resume_round'], 2)
            self.assertIs(inspection['round_complete'], False)
        self.assertEqual(native.read(receipt)['source'], str(self.source))

    def test_signed_inspection_can_be_reused_while_every_copied_byte_is_verified(self):
        module = self.module()
        inspection_path = self.root / 'inspection.json'
        native.write(inspection_path, module.inspect_checkpoint(self.source, self.input, native))
        expected = native.digest(inspection_path)
        target = self.root / 'reuse_attempt' / 'dictionary'
        with patch.object(module, 'inspect_checkpoint', side_effect=AssertionError('verified journal parsed twice')):
            receipt = module.migrate_checkpoint(self.source, target, self.input, self.cfg, self.books, native,
                freeze_receipt=self.freeze, book_checkpoints=self.book_proof,
                inspection_receipt=inspection_path, inspection_sha256=expected)
        self.assertEqual(native.read(receipt)['files'], module.snapshot_files(target))
        with self.assertRaisesRegex(ValueError, '[Ii]nspection'):
            module.migrate_checkpoint(self.source, self.root / 'bad_inspection', self.input, self.cfg,
                self.books, native, freeze_receipt=self.freeze, book_checkpoints=self.book_proof,
                inspection_receipt=inspection_path, inspection_sha256='0' * 64)
        journal = self.source / 'name/round_2/llm_name_title_format_judgments.jsonl'
        journal.write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'copied|hash|changed'):
            module.migrate_checkpoint(self.source, self.root / 'bad_copy', self.input, self.cfg,
                self.books, native, freeze_receipt=self.freeze, book_checkpoints=self.book_proof,
                inspection_receipt=inspection_path, inspection_sha256=expected)

    def test_model_context_or_input_drift_cannot_be_migrated(self):
        module = self.module()
        for changes in ({'model': 'other'}, {'context_limit': 65536}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'identity'):
                module.migrate_checkpoint(self.source, self.root / ('bad_' + next(iter(changes))),
                    self.input, dict(self.cfg, **changes), self.books, native,
                    freeze_receipt=self.freeze, book_checkpoints=self.book_proof)
        native.write(self.input, self.rows[:1])
        with self.assertRaisesRegex(ValueError, 'input|identity'):
            module.inspect_checkpoint(self.source, self.input, native)

    def test_same_round_resume_is_explicit_and_does_not_claim_prior_round_complete(self):
        target, config, receipt = self.migrate()
        content = isolated_dictionary_content()
        with self.assertRaisesRegex(ValueError, 'inspection|receipt'):
            native.run_cleaning_round(self.input, config, target, self.books, 2)
        def stage(values, label, *_):
            annotation = native.NAME if label == 'name' else native.SCOPE
            return [{**row, annotation: {'decision': 'keep', 'api_status': 'ok'}} for row in values]
        with patch.dict(sys.modules, {'clean_boundary_v46': content, 'fullbook_llm_v2': types.SimpleNamespace(
                book_lock=lambda _: contextlib.nullcontext())}), patch.object(
                native.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"data":[{"id":"test"}]}')), \
                patch.object(native, 'filter_stage', side_effect=stage), patch.object(
                content.Runner, 'batch', return_value=[]):
            status = native.run_cleaning_round(self.input, config, target, self.books, 2,
                                               resume_receipt=receipt)
        self.assertEqual(status['round'], 2)
        self.assertIsNone(status['next_round'])
        self.assertEqual(native.read(target / 'STATE.json')['status'], 'round_complete')

    def test_temporary_models_preflight_backoff_keeps_the_same_round(self):
        module = self.module()
        saved_state = (self.source / 'STATE.json').read_bytes()
        delays = []
        failures = [HTTPError('http://offline/v1/models', 503, 'busy', {}, None), TimeoutError('slow'),
                    io.BytesIO(b'{"data":[{"id":"test"}]}')]
        with patch.object(native.urllib.request, 'urlopen', side_effect=failures):
            module.models_preflight(self.cfg, self.root / 'SERVICE_PAUSE.json', 2,
                                    open_fn=native.urllib.request.urlopen, wait_fn=delays.append)
        self.assertEqual(delays, [5, 10])
        self.assertEqual((self.source / 'STATE.json').read_bytes(), saved_state)
        self.assertEqual(native.read(self.root / 'SERVICE_PAUSE.json')['round'], 2)

    def test_wrong_model_identity_stops_without_backoff(self):
        delays = []
        with self.assertRaisesRegex(ValueError, 'model'), patch.object(
                native.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"data":[{"id":"other"}]}')):
            self.module().models_preflight(self.cfg, self.root / 'SERVICE_PAUSE.json', 2,
                open_fn=native.urllib.request.urlopen, wait_fn=delays.append)
        self.assertEqual(delays, [])

    def test_compensation_requires_revocation_and_later_transport_failure(self):
        module = self.module()
        old, current = self.root / 'old', self.root / 'current'
        relative = 'content/calls/explanation/a.json'
        original = {'status': 'failed', 'round': 1, 'packet': {'subject': 'A'}, 'prompt': 'unchanged',
                    'attempts': [{'error': '<HTTPError 404: job unregistered>'}]}
        latest = {**original, 'round': 3, 'failure_kind': 'transport',
                  'attempts': [{'error': '<HTTPError 502: Bad Gateway>'}]}
        native.write(old / relative, original)
        native.write(current / relative, latest)
        native.write(current / 'content/results/a.json', {'id': 'a', 'status': 'failed'})
        proof = {'schema': 1, 'event': 'gateway_job_unregistered', 'job_id': 'old-job',
                 'request_files': {relative: native.digest(old / relative)}}
        plan = module.compensation_plan(old, current, proof)
        self.assertEqual([row['request_file'] for row in plan['eligible']], [relative])
        self.assertEqual(plan['eligible'][0]['history_round'], 3)
        self.assertEqual(plan['eligible'][0]['opportunities'], 1)
        native.write(current / 'content/results/a.json', {'id': 'a', 'status': 'drop'})
        self.assertEqual(module.compensation_plan(old, current, proof)['eligible'], [])
        proof['event'] = 'upstream_http_404'
        with self.assertRaisesRegex(ValueError, 'revocation|unregistered'):
            module.compensation_plan(old, current, proof)

    def test_migration_requires_matching_frozen_source_evidence(self):
        module = self.module()
        with self.assertRaisesRegex(ValueError, 'freeze|frozen'):
            module.migrate_checkpoint(self.source, self.root / 'unproven', self.input,
                                       self.cfg, self.books, native)
        proof = native.read(self.freeze)
        proof['processes'][0]['state'] = 'R'
        native.write(self.freeze, proof)
        with self.assertRaisesRegex(ValueError, 'freeze|frozen|stopped'):
            self.migrate()

    def test_same_round_filter_journal_reuses_success_and_already_attempted_failure(self):
        script = native.ROOT / 'name_filters/01_name_title_quality.py'
        spec = importlib.util.spec_from_file_location('resume_name_fixture', script)
        filter_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(filter_module)
        journal = self.source / 'name/round_2/llm_name_title_format_judgments.jsonl'
        native.jsonl(journal, [{'id': row['id'], 'decision': 'keep' if row['id'] == 'a' else 'review',
            'api_status': 'ok' if row['id'] == 'a' else 'api_error_after_retries',
            'failure_kind': '' if row['id'] == 'a' else 'transport',
            'policy_version': filter_module.POLICY_VERSION} for row in self.rows])
        votes = native.loadl(journal)
        votes[1].pop('policy_version')  # The real native error constructor omits it.
        votes[1]['api_error'] = 'TimeoutError: timed out'
        native.jsonl(journal, votes)
        config = dict(self.cfg, staged_round=2, resume_current_round=True)
        with patch.object(native.subprocess, 'run', side_effect=AssertionError('completed request replayed')):
            result = native.filter_stage(self.rows, 'name', config, self.source / 'name')
        self.assertEqual([row[native.NAME]['api_status'] for row in result],
                         ['ok', 'api_error_after_retries'])
        votes[1].update(api_status='missing_result', api_error='', reason='missing_result',
                        failure_kind='content_or_format')
        native.jsonl(journal, votes)
        cached, pending = self.module().filter_journal_records(
            journal.parent, script, self.rows, 'llm_name_title_format')
        self.assertEqual(pending, [])
        self.assertEqual(cached['b'][native.NAME]['api_status'], 'missing_result')

    def test_seeded_history_can_prove_a_legitimately_replaced_round_one_aggregate(self):
        module = self.module()
        original = self.root / 'original_round_one'
        initial = [{**row, native.NAME: {'decision': 'review',
            'api_status': 'api_error_after_retries', 'failure_kind': 'content_or_format'}} for row in self.rows]
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            native.jsonl(original / 'name' / name, initial)
            seeded = copy.deepcopy(initial)
            for row in seeded:
                row[native.NAME]['failure_kind'] = 'transport'
            native.jsonl(self.source / 'name' / name, seeded)
        native.jsonl(original / 'name/initial/llm_name_title_format_judgments.jsonl', [
            {'id': row['id'], 'api_status': 'api_error_after_retries',
             'api_error': '<HTTPError 404: job unregistered>'} for row in self.rows])
        native.write(self.source / 'SOURCE_CACHE.json', {'source': str(original), 'files': {
            path.relative_to(original).as_posix(): native.digest(path)
            for path in original.rglob('*') if path.is_file()}})
        previous = native.read(self.source / 'ORCHESTRATION_STATUS.json')
        previous['checkpoint_hashes'] = native.staged_checkpoint_hashes(self.source)
        # Round-two files were not present at the end of round one.
        previous['checkpoint_hashes'] = {key: value for key, value in previous['checkpoint_hashes'].items()
                                         if not key.startswith('name/round_2/')}
        native.write(self.source / 'ORCHESTRATION_STATUS.json', previous)
        script = native.ROOT / 'name_filters/01_name_title_quality.py'
        spec = importlib.util.spec_from_file_location('history_name_fixture', script)
        formatter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(formatter)
        vote = {'id': 'a', 'line_no': 1, 'decision': 'keep', 'api_status': 'ok',
                'policy_version': formatter.POLICY_VERSION}
        native.jsonl(self.source / 'name/round_2/llm_name_title_format_judgments.jsonl', [vote])
        outputs = {key: self.root / (key + '.jsonl')
                   for key in ('all_full', 'keep_full', 'drop_full', 'review_full')}
        records = formatter.build_records(self.source / 'name/round_2/input.jsonl', 0, 0, 'jsonl', 0)
        formatter.write_full_record_outputs(records[:1], {formatter.record_key(records[0]): vote}, outputs)
        merged = native.loadl(outputs['all_full']) + seeded[1:]
        native.jsonl(self.source / 'name/FIRST.jsonl', merged)
        native.jsonl(self.source / 'name/FINAL.jsonl', merged)
        inspection = module.inspect_checkpoint(self.source, self.input, native)
        self.assertEqual(len(inspection['historical_seed_proofs']), 2)
        merged[0][native.NAME]['decision'] = 'drop'
        native.jsonl(self.source / 'name/FINAL.jsonl', merged)
        with self.assertRaisesRegex(ValueError, 'aggregate|journal'):
            module.inspect_checkpoint(self.source, self.input, native)

    def test_compensation_permission_is_bound_to_failed_request_and_consumed_once(self):
        module = self.module()
        old, current = self.root / 'claim_old', self.root / 'claim_current'
        relative = 'content/calls/explanation/a.json'
        original = {'status': 'failed', 'round': 1, 'packet': {'subject': 'A'}, 'prompt': 'same',
                    'attempts': [{'error': '<HTTPError 404: job unregistered>'}]}
        latest = {**original, 'round': 3, 'failure_kind': 'transport',
                  'attempts': [{'error': '<HTTPError 502: Bad Gateway>'}]}
        native.write(old / relative, original)
        native.write(current / relative, latest)
        proof = {'schema': 1, 'event': 'gateway_job_unregistered', 'job_id': 'job',
                 'request_files': {relative: native.digest(old / relative)}}
        plan_path = self.root / 'compensation.json'
        native.write(plan_path, module.compensation_plan(old, current, proof))
        config = {'compensation_ids': ['a'], 'compensation_token': {
            'path': str(plan_path), 'sha256': native.digest(plan_path)}}
        self.assertTrue(module.claim_compensation(config, 'a', 'explanation', latest))
        self.assertFalse(module.claim_compensation(config, 'a', 'explanation', latest))
        with self.assertRaisesRegex(ValueError, 'eligibility'):
            module.claim_compensation(config, 'a', 'explanation', dict(latest, status='ok'))

    def test_controller_offline_inspect_entry_does_not_start_a_gateway(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_controller_test', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        main = getattr(controller, 'main', None)
        self.assertTrue(callable(main), 'controller needs explicit offline inspect/migrate/resume CLI')
        report = self.root / 'INSPECTION.json'
        with patch.object(controller, 'Gateway', side_effect=AssertionError('offline inspection started gateway')):
            code = main(['--inspect-book', str(self.source), '--input', str(self.input),
                         '--report-out', str(report)])
        self.assertEqual(code, 0)
        self.assertEqual(native.read(report)['resume_round'], 2)

    def test_retained_passed_baseline_binds_the_immediately_frozen_current_outputs(self):
        module = self.module()
        passed = {**self.rows[0], 'definition': 'A body', 'description': ''}
        native.jsonl(self.source / 'FINAL_RECORDS.jsonl', [passed])
        target, _, receipt = self.migrate()
        proof = native.read(receipt)
        self.assertEqual(set(proof['retained_passed']['records']), {'a'})
        module.verify_retained_passes(target, receipt)
        native.jsonl(target / 'FINAL_RECORDS.jsonl', [])
        with self.assertRaisesRegex(ValueError, 'passed|retained'):
            module.verify_retained_passes(target, receipt)
        native.jsonl(target / 'FINAL_RECORDS.jsonl', [dict(passed, definition='changed')])
        with self.assertRaisesRegex(ValueError, 'passed|retained'):
            module.verify_retained_passes(target, receipt)

    def test_resumed_controller_passes_full_32_card_profile_to_mounting(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_controller_profile', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        target, _, receipt = self.migrate()
        root = target.parent
        native.write(target / 'STATE.json', {'status': 'round_complete', 'round': 3})
        native.write(target / 'ORCHESTRATION_STATUS.json', {'state': 'ready_to_finalize', 'round': 3})
        native.write(root / 'JOBS.json', [{'out': str(target), 'home': str(root), 'job_id': 'test',
            'resume_receipt': str(receipt), 'resume_receipt_sha256': native.digest(receipt)}])
        native.write(root / 'RESUME_PLAN.json', {'jobs': 1, 'source': str(self.source)})
        native.write(root / 'SERVICE_PREFLIGHT.json', {'api_url': 'http://offline', 'model': 'test'})
        observed = []
        class Gateway:
            api_url = 'http://offline'
            def __init__(self, *_args, **_kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        def finish(_root, _cleaned, _service, _gateway, profile):
            observed.append(profile)
            return {'unresolved_stages': []}
        with patch.object(controller, '__file__', str(root / 'resume_dictionary.py')), \
                patch.object(controller.bench, 'verify_code_snapshot'), \
                patch.object(controller, 'Gateway', Gateway), \
                patch.object(controller, '_rebind_resumed_job'), \
                patch.object(controller, 'invoke', return_value={'status': 'completed'}), \
                patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
                    'resume_mounting': types.SimpleNamespace(finish_dictionary=finish)}):
            self.assertEqual(controller.run_resumed(root), 0)
        self.assertEqual(observed[0].round_limits, (1024, 256, 64))
        self.assertEqual(observed[0].front_workers, 1024)
        self.assertEqual((observed[0].cpu_workers, observed[0].production_workers, observed[0].mount_workers), (8, 8, 8))

    def test_current_and_completed_cohorts_borrow_the_whole_round_cap_per_job(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_controller_cohorts', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        root = self.root / 'cohort_attempt'
        jobs = []
        for number in range(3):
            out = root / str(number) / 'dictionary'
            native.write(out / 'STATE.json', {'status': 'round_running' if number < 2 else 'round_complete', 'round': 2})
            native.write(out / 'ORCHESTRATION_STATUS.json', {'state': 'awaiting_retry',
                'round': 1 if number < 2 else 2, 'next_round': 2 if number < 2 else 3})
            receipt = out.parent / 'receipt.json'
            native.write(receipt, {'round_complete': number == 2})
            jobs.append({'job_id': str(number), 'out': str(out), 'home': str(out.parent),
                'resume_receipt': str(receipt), 'resume_receipt_sha256': native.digest(receipt)})
        native.write(root / 'JOBS.json', jobs)
        native.write(root / 'RESUME_PLAN.json', {'jobs': 3, 'source': str(self.source)})
        native.write(root / 'SERVICE_PREFLIGHT.json', {'api_url': 'http://offline', 'model': 'test'})
        calls = []
        def invoke(job, round_number, workers, finalize=False):
            if finalize:
                return {'status': 'completed'}
            calls.append((job['job_id'], round_number, workers))
            native.write(Path(job['out']) / 'STATE.json', {'status': 'round_complete', 'round': round_number})
            native.write(Path(job['out']) / 'ORCHESTRATION_STATUS.json', {
                'state': 'awaiting_retry' if round_number == 2 else 'ready_to_finalize',
                'round': round_number, 'next_round': 3 if round_number == 2 else None})
            return {'status': 'awaiting_retry' if round_number == 2 else 'ready_to_finalize'}
        class Gateway:
            api_url = 'http://offline'
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_): return False
        with patch.object(controller, '__file__', str(root / 'resume_dictionary.py')), \
                patch.object(controller.bench, 'verify_code_snapshot'), patch.object(controller, 'Gateway', Gateway), \
                patch.object(controller, '_rebind_resumed_job'), patch.object(controller, 'transition_phase'), \
                patch.object(controller, 'invoke', side_effect=invoke), patch.dict(sys.modules, {
                    'fcntl': types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
                    'resume_mounting': types.SimpleNamespace(finish_dictionary=lambda *_: {'unresolved_stages': []})}):
            controller.run_resumed(root)
        self.assertEqual(sorted(calls), [('0', 2, 256), ('0', 3, 64),
            ('1', 2, 256), ('1', 3, 64), ('2', 3, 64)])

    def test_controller_cli_flags_bind_current_r2_and_completed_r2_next_r3_receipts(self):
        target, _, receipt = self.migrate()
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_controller_flags', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        job = {'out': str(target), 'home': str(target.parent), 'job_id': 'job',
               'resume_receipt': str(receipt), 'command': ['python', 'offline-adapter']}
        commands = []
        def child(command, **_):
            commands.append(command)
            number = int(command[command.index('--only-round') + 1])
            native.write(target / 'STATE.json', {'status': 'round_complete', 'round': number})
            native.write(target / 'ORCHESTRATION_STATUS.json', {'round': number,
                'state': 'awaiting_retry' if number == 2 else 'ready_to_finalize',
                'next_round': 3 if number == 2 else None,
                'checkpoint_hashes': native.staged_checkpoint_hashes(target)})
            return types.SimpleNamespace(returncode=0)
        with patch.object(controller.subprocess, 'run', side_effect=child):
            controller.invoke(job, 2, 256)
            inspection = self.module().inspect_checkpoint(target, self.input, native)
            updated = native.read(receipt)
            updated.update({key: inspection[key] for key in ('completed_round', 'resume_round', 'round_complete', 'files')})
            native.write(receipt, updated)
            controller.invoke(job, 3, 64)
        self.assertEqual([cmd[cmd.index('--only-round') + 1] for cmd in commands], ['2', '3'])
        self.assertEqual([cmd[cmd.index('--workers') + 1] for cmd in commands], ['256', '64'])
        for command in commands:
            self.assertIn('--resume', command)
            self.assertEqual(command[command.index('--resume-receipt') + 1], str(receipt))
    def test_name_and_scope_lost_opportunity_plans_exclude_later_normal_drop(self):
        module = self.module()
        original, current = self.root / 'filter_original', self.root / 'filter_current'
        request_files = {}
        for folder, annotation, stem in (
                ('name', native.NAME, 'llm_name_title_format'),
                ('scope/group_abc', native.SCOPE, 'subject_scope')):
            relative = folder + '/initial/' + stem + '_judgments.jsonl'
            native.jsonl(original / relative, [{'id': 'a', 'api_status': 'api_error_after_retries',
                'api_error': '<HTTPError 404: job unregistered>', 'decision': 'review'}])
            request_files[relative] = native.digest(original / relative)
            for name in ('FIRST.jsonl', 'FINAL.jsonl'):
                native.jsonl(current / folder / name, [{**self.rows[0], annotation: {
                    'decision': 'review', 'api_status': 'api_error_after_retries', 'failure_kind': 'transport'}}])
            native.jsonl(current / folder / 'round_3' / (stem + '_judgments.jsonl'), [
                {'id': 'a', 'decision': 'review', 'api_status': 'api_error_after_retries',
                 'failure_kind': 'transport'}])
        native.write(current / 'ORCHESTRATION_STATUS.json', {'round': 3})
        proof = {'schema': 1, 'event': 'gateway_job_unregistered', 'job_id': 'old-job',
                 'request_files': request_files}
        plan = module.compensation_plan(original, current, proof)
        self.assertEqual(sorted(row['stage'] for row in plan['eligible']), ['name', 'scope'])
        self.assertTrue(all(row['missing_opportunity'] == 1 for row in plan['eligible']))
        dropped = {**self.rows[0], native.NAME: {'decision': 'drop', 'api_status': 'ok'}}
        native.jsonl(current / 'name/FINAL.jsonl', [dropped])
        self.assertEqual([row['stage'] for row in module.compensation_plan(original, current, proof)['eligible']],
                         ['scope'])

    def test_controller_restart_recovers_each_committed_gateway_rebind(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_rebind_transaction', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        target, config, receipt = self.migrate()
        root = target.parent
        books_path = root / 'books.json'
        native.write(books_path, self.books)
        jobs = [{'out': str(target), 'home': str(root), 'job_id': 'first',
                 'input': str(self.input), 'books': str(books_path),
                 'command': ['python', '--api-url', config['api_url']],
                 'resume_receipt': str(receipt), 'resume_receipt_sha256': native.digest(receipt)}]
        other_out = root / 'dictionary_two'
        other_receipt = self.module().migrate_checkpoint(self.source, other_out, self.input, config,
            self.books, native, freeze_receipt=self.freeze, book_checkpoints=self.book_proof)
        other = dict(jobs[0], job_id='second', out=str(other_out), resume_receipt=str(other_receipt),
                     resume_receipt_sha256=native.digest(other_receipt))
        jobs.append(other)
        native.write(root / 'JOBS.json', jobs)
        native.write(root / 'RESUME_PLAN.json', {'jobs': 2, 'source': str(self.source)})
        native.write(root / 'SERVICE_PREFLIGHT.json', {'api_url': 'http://offline', 'model': 'test'})
        class Gateway:
            api_url = 'http://offline'
            def __init__(self, *_args, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def job_url(self, job): return 'http://127.0.0.1:99/' + job
        actual_rebind = controller._rebind_resumed_job
        def interrupted(job, gateway, **kwargs):
            if job['job_id'] == 'second':
                raise InterruptedError('stop after first committed book')
            return actual_rebind(job, gateway, **kwargs)
        with patch.object(controller, 'ROOT', root), patch.object(controller.bench, 'verify_code_snapshot'), \
                patch.object(controller, 'Gateway', Gateway), patch.object(
                    controller, '_rebind_resumed_job', side_effect=interrupted):
            with self.assertRaisesRegex(InterruptedError, 'first committed'):
                controller._run_resumed_locked()
        first = native.read(root / 'JOBS.json')[0]
        self.assertEqual(first['resume_receipt_sha256'], native.digest(receipt),
                         'a later book interrupt must not leave earlier JOBS receipt hashes stale')
        self.assertTrue(first.get('gateway_rebind_transaction'), 'restart authorization needs a sealed hash chain')

    def test_completed_inspection_uses_the_existing_full_snapshot_hashes(self):
        value = native.read(self.source / 'ORCHESTRATION_STATUS.json')
        value['checkpoint_hashes'] = native.staged_checkpoint_hashes(self.source)
        native.write(self.source / 'ORCHESTRATION_STATUS.json', value)
        native.write(self.source / 'STATE.json', {'status': 'round_complete', 'round': 1})
        with patch.object(native, 'verify_staged_checkpoint', side_effect=AssertionError('duplicate full file hash')):
            self.assertEqual(self.module().inspect_checkpoint(self.source, self.input, native)['completed_round'], 1)

    def compensation_fixture(self):
        native.write(self.books[0]['scope_config'], {'subject': 'Test', 'subject_definition': 'Test definition',
            'subject_boundary': 'Test boundary', 'l1_nodes': [{'name': 'L1', 'definition': 'Definition', 'boundary': 'Boundary'}]})
        current, original = self.root / 'new_run/dictionary', self.root / 'original_run/dictionary'
        rows = [{**self.rows[0], 'id': key, 'knowledge_point': key.upper()} for key in ('a', 'b', 'c', 'd', 'e')]
        input_path = self.root / 'COMPENSATION_INPUT.json'
        native.write(input_path, rows)
        keep = {'decision': 'keep', 'api_status': 'ok'}
        failed = {'decision': 'review', 'api_status': 'api_error_after_retries', 'failure_kind': 'transport'}
        names = [{**row, native.NAME: failed if row['id'] == 'a' else
                  {'decision': 'drop', 'api_status': 'ok'} if row['id'] == 'e' else keep} for row in rows]
        group = 'scope/group_' + native.hashlib.sha256(self.books[0]['scope_config'].encode()).hexdigest()[:12]
        scoped = [{**row, native.SCOPE: failed if row['id'] == 'b' else keep}
                  for row in names if row['id'] in ('b', 'c', 'd')]
        for folder, annotation, stem, values, sid in (
                ('name', native.NAME, 'llm_name_title_format', names, 'a'),
                (group, native.SCOPE, 'subject_scope', scoped, 'b')):
            for artifact in ('FIRST.jsonl', 'FINAL.jsonl'):
                native.jsonl(current / folder / artifact, values)
            native.jsonl(current / folder / 'round_3' / (stem + '_judgments.jsonl'), [dict(failed, id=sid)])
            native.jsonl(current / folder / 'round_3/input.jsonl', [row for row in rows if row['id'] == sid])
            native.jsonl(original / folder / 'initial' / (stem + '_judgments.jsonl'), [
                {'id': sid, 'decision': 'review', 'api_status': 'api_error_after_retries',
                 'api_error': '<HTTPError 404: Not Found>'}])
        relative = 'content/calls/explanation/c.json'
        old = {'status': 'failed', 'round': 1, 'packet': {'subject': 'C'}, 'prompt': 'same',
               'attempts': [{'error': '<HTTPError 404: Not Found>'}]}
        latest = dict(old, round=3, failure_kind='transport', attempts=[{'error': '<HTTPError 503: Busy>'}])
        native.write(original / relative, old)
        native.write(current / relative, latest)
        native.write(current / 'content/results/c.json', {'id': 'c', 'status': 'failed'})
        passed = {**rows[3], 'definition': 'A body', 'description': ''}
        native.write(current / 'content/results/d.json', {'id': 'd', 'status': 'keep', 'record': passed})
        native.jsonl(current / 'FINAL_RECORDS.jsonl', [passed])
        native.write(current / 'STANDARD_RECORDS.json', [{key: passed.get(key, '') for key in native.STANDARD}])
        ledger = [{'id': row['id'], 'name': row[native.NAME],
            'scope': next((s[native.SCOPE] for s in scoped if s['id'] == row['id']), None),
            'content_status': 'failed' if row['id'] == 'c' else 'keep' if row['id'] == 'd' else 'not_reached',
            'content_failure_kind': 'transport' if row['id'] == 'c' else None} for row in names]
        native.jsonl(current / 'DISPOSITIONS.jsonl', ledger)
        native.jsonl(current / 'TECHNICAL_FAILURES.jsonl', ledger[:3])
        native.jsonl(current / 'REVIEW.jsonl', ledger[:3])
        native.write(current / 'SUMMARY.json', {'input': 5, 'technical_failures': 3, 'review': 3,
            'final_keep': 1, 'round': 3, 'semantic_quality_approved': False})
        manifest = native.staged_manifest(input_path, self.cfg, self.books)
        native.write(current / 'MANIFEST.json', manifest)
        native.write(original / 'MANIFEST.json', manifest)
        proof = {'source': str(original), 'files': {
            p.relative_to(original).as_posix(): native.digest(p) for p in original.rglob('*') if p.is_file()}}
        native.write(current / 'SOURCE_CACHE.json', proof)
        native.write(current / 'ORCHESTRATION_STATUS.json', {'state': 'technical_failure', 'round': 3,
            'next_round': None, 'checkpoint_hashes': native.staged_checkpoint_hashes(current)})
        native.write(current / 'STATE.json', {'status': 'round_complete', 'round': 3})
        native.write(current.parent / 'dictionary_RESUME_RECEIPT.json', {'source': str(self.source),
            'schema': 1, 'target': str(current), 'destination': str(current), 'new_manifest': manifest,
            'retained_passed': self.module().retained_passed_baseline(current)})
        evidence = self.root / 'EVIDENCE.json'
        capture = self.root / 'READONLY_CAPTURE.json'
        native.write(evidence, {'api_check': {'url': self.cfg['api_url'], 'get_health': 404,
            'get_v1_models': 404, 'response_type': 'nginx HTML 404'},
            'user_confirmed_cause': 'API expired and deregistered; user is restarting'})
        native.write(capture, {'production_root': str(original.parent),
                              'gateway': {'phase': 'dictionary_cleaning_round_1'}})
        return current, original, input_path, evidence, capture, group, latest

    def test_deployment_retirement_proof_is_bound_to_exact_service_and_original_cache(self):
        module = self.module()
        builder = getattr(module, 'deployment_revocation_proof', None)
        self.assertTrue(callable(builder), 'upstream deployment retirement evidence needs its own verified proof')
        current, original, _, evidence, capture, _, _ = self.compensation_fixture()
        proof = builder(current, evidence, capture, self.cfg['api_url'])
        self.assertEqual(proof['event'], 'service_deployment_deregistered')
        self.assertEqual(proof['source'], str(original))
        self.assertIn('content/calls/explanation/c.json', proof['request_files'])
        with self.assertRaisesRegex(ValueError, 'service|upstream'):
            builder(current, evidence, capture, 'http://another-service')

    def test_isolated_compensation_executes_once_and_backfills_without_rewriting_rounds(self):
        module = self.module()
        executor = getattr(native, 'run_compensation', None)
        self.assertTrue(callable(executor), 'verified lost opportunity plans must have an actual isolated executor')
        current, original, input_path, evidence, capture, _, _ = self.compensation_fixture()
        proof = module.deployment_revocation_proof(current, evidence, capture, self.cfg['api_url'])
        plan_path = current.parent / 'COMPENSATION_PLAN.json'
        native.write(plan_path, module.compensation_plan(original, current, proof))
        old_files = module.snapshot_files(original)
        requests = []
        def filter_process(command, **_):
            script = Path(command[1])
            values = native.loadl(command[command.index('--input') + 1])
            target = Path(command[command.index('--out-dir') + 1])
            stem = 'llm_name_title_format' if '01_name' in script.name else 'subject_scope'
            annotation = native.NAME if '01_name' in script.name else native.SCOPE
            requests.extend((annotation, row['id']) for row in values)
            native.jsonl(target / (stem + '_all_full.jsonl'), [
                {**row, annotation: {'decision': 'keep', 'api_status': 'ok'}} for row in values])
            # The subprocess boundary returns native per-request receipts as well.
            formatter = module._filter_module(script)
            native.jsonl(target / (stem + '_judgments.jsonl'), [
                {'id': row['id'], 'decision': 'keep', 'api_status': 'ok', 'line_no': n + 1,
                 'policy_version': formatter.POLICY_VERSION} for n, row in enumerate(values)])
            return types.SimpleNamespace(returncode=0)
        content = isolated_dictionary_content()
        def body(runner, row):
            if row['id'] == 'c':
                self.assertTrue(module.claim_compensation(runner.cfg, 'c', 'explanation',
                    native.read(runner.out / 'calls/explanation/c.json')))
            else:
                self.assertTrue(module.claim_first_downstream(runner.cfg, row['id'], 'content', 'explanation'))
            requests.append(('content', row['id']))
            return {'id': row['id'], 'status': 'keep', 'record': {**row, 'definition': 'A body'}}
        with patch.dict(sys.modules, {'clean_boundary_v46': content, 'fullbook_llm_v2': types.SimpleNamespace(
                book_lock=lambda _: contextlib.nullcontext())}), patch.object(native.subprocess, 'run',
                side_effect=filter_process), patch.object(content.Runner, 'content', body), \
                patch.object(native.urllib.request, 'urlopen', side_effect=lambda *_a, **_k:
                             io.BytesIO(b'{"data":[{"id":"test"}]}')):
            with patch.object(content.Runner, 'batch', side_effect=InterruptedError('model phase interrupted')):
                with self.assertRaisesRegex(InterruptedError, 'model phase'):
                    executor(input_path, self.cfg, current, self.books, plan_path, native.digest(plan_path))
            # A controller restart binds a fresh gateway epoch before the private cache resumes.
            script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
            spec = importlib.util.spec_from_file_location('resume_compensation_restart', script)
            controller = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(controller)
            books_path = current.parent / 'books.json'
            native.write(books_path, self.books)
            receipt = current.parent / 'dictionary_RESUME_RECEIPT.json'
            job = {'job_id': 'test', 'out': str(current), 'home': str(current.parent),
                   'input': str(input_path), 'books': str(books_path), 'resume_receipt': str(receipt),
                   'command': ['python', '--api-url', self.cfg['api_url']],
                   'resume_receipt_sha256': native.digest(receipt)}
            controller._rebind_resumed_job(job, types.SimpleNamespace(job_url=lambda _: 'http://127.0.0.1:2'))
            config = dict(self.cfg, api_url='http://127.0.0.1:2')
            actual_replace = Path.replace
            writes = []
            def interrupted_commit(path, target):
                if path.name.endswith('.compensation.tmp') and Path(target).is_relative_to(current):
                    writes.append(str(target))
                    if len(writes) == 2:
                        raise InterruptedError('backfill partially committed')
                return actual_replace(path, target)
            with patch.object(Path, 'replace', interrupted_commit):
                with self.assertRaisesRegex(InterruptedError, 'partially committed'):
                    executor(input_path, config, current, self.books, plan_path, native.digest(plan_path))
            # The real controller entry must recover COMMIT before inspecting/rebinding this out.
            native.write(current.parent / 'JOBS.json', [job])
            native.write(current.parent / 'RESUME_PLAN.json', {'jobs': 1, 'source': str(self.source)})
            native.write(current.parent / 'SERVICE_PREFLIGHT.json', self.cfg)
            class Gateway:
                api_url = 'http://127.0.0.1:3'
                def __init__(self, *_a, **_k): pass
                def __enter__(self): return self
                def __exit__(self, *_): return False
            def inspected_after_recovery(_job, _gateway, **_):
                native.verify_staged_checkpoint(current, native.read(current / 'ORCHESTRATION_STATUS.json'))
                raise InterruptedError('recovery verified before rebind')
            with patch.object(controller, 'ROOT', current.parent), patch.object(controller.bench, 'verify_code_snapshot'), \
                    patch.object(controller, 'Gateway', Gateway), patch.object(controller, '_rebind_resumed_job',
                    side_effect=inspected_after_recovery):
                with self.assertRaisesRegex(InterruptedError, 'recovery verified'):
                    controller._run_resumed_locked()
            result = executor(input_path, config, current, self.books, plan_path, native.digest(plan_path))
            first_requests = list(requests)
            self.assertEqual(executor(input_path, config, current, self.books, plan_path,
                                      native.digest(plan_path)), result)
        self.assertEqual(requests, first_requests, 'completed compensation must be idempotent')
        self.assertEqual({sid for _, sid in requests}, {'a', 'b', 'c'})
        self.assertEqual([sid for stage, sid in requests if stage == native.NAME], ['a'])
        self.assertEqual(native.read(current / 'ORCHESTRATION_STATUS.json')['round'], 3)
        self.assertEqual(native.read(current / 'ORCHESTRATION_STATUS.json').get('retryable_technical_failures'), 0)
        self.assertEqual(native.read(current / 'STATE.json')['round'], 3)
        self.assertEqual({row['id'] for row in native.loadl(current / 'FINAL_RECORDS.jsonl')}, {'a', 'b', 'c', 'd'})
        native.verify_staged_checkpoint(current, native.read(current / 'ORCHESTRATION_STATUS.json'))
        self.assertEqual(module.snapshot_files(original), old_files)
        self.assertIs(result['semantic_quality_approved'], False)
        from compensation_recovery import home
        session = native.read(home(current) / native.digest(plan_path) / 'SESSION.json')
        self.assertEqual(session['gateway_migrations'][-1]['to'], config['api_url'])
        final = native.finalize_cleaning(input_path, config, current, self.books)
        self.assertEqual(final['final_keep'], 4)
        self.assertEqual(native.read(current / 'ORCHESTRATION_STATUS.json')['state'], 'finalized')

    def test_body_compensation_is_the_only_exception_to_same_round_no_replay(self):
        current, original, _, evidence, capture, _, latest = self.compensation_fixture()
        module = self.module()
        proof_builder = getattr(module, 'deployment_revocation_proof', None)
        self.assertTrue(callable(proof_builder), 'the body exception requires verified deployment retirement evidence')
        plan_path = current.parent / 'BODY_PLAN.json'
        native.write(plan_path, module.compensation_plan(original, current,
            proof_builder(current, evidence, capture, self.cfg['api_url'])))
        content = isolated_dictionary_content()
        cfg = dict(self.cfg, staged_round=3, compensation_mode=True, compensation_ids=['c'],
                   compensation_token={'path': str(plan_path), 'sha256': native.digest(plan_path)})
        runner = content.Runner(current / 'content', cfg)
        response = b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"ok\\":true}"}}]}'
        with patch.object(content.urllib.request, 'urlopen', return_value=io.BytesIO(response)) as boundary:
            result = runner._staged_call('c', 'explanation', latest['packet'], latest['prompt'], lambda _: None)
            self.assertEqual(result['status'], 'ok', 'a sealed one-time token must allow this same-round failed request')
            runner._staged_call('c', 'explanation', latest['packet'], latest['prompt'], lambda _: None)
        self.assertEqual(boundary.call_count, 1)
        stored = native.read(current / 'content/calls/explanation/c.json')
        self.assertEqual(stored['round'], 3)
        self.assertEqual(stored['compensation']['history_round'], 3)

    def test_retired_name_credit_follows_the_current_failed_scope_without_replaying_name(self):
        module = self.module()
        current, original, _, evidence, capture, group, _ = self.compensation_fixture()
        keep = {'decision': 'keep', 'api_status': 'ok'}
        failed = {'decision': 'review', 'api_status': 'api_error_after_retries', 'failure_kind': 'transport'}
        names = native.loadl(current / 'name/FINAL.jsonl')
        names[0][native.NAME] = keep
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            native.jsonl(current / 'name' / name, names)
            scopes = native.loadl(current / group / name)
            native.jsonl(current / group / name, [{**names[0], native.SCOPE: failed}, *scopes])
        for number in (2, 3):
            native.jsonl(current / group / f'round_{number}/subject_scope_judgments.jsonl', [dict(failed, id='a')])
        ledger = native.loadl(current / 'DISPOSITIONS.jsonl')
        ledger[0].update(name=keep, scope=failed)
        native.jsonl(current / 'DISPOSITIONS.jsonl', ledger)
        proof = module.deployment_revocation_proof(current, evidence, capture, self.cfg['api_url'])
        plan = module.compensation_plan(original, current, proof)
        row = next(row for row in plan['eligible'] if row['id'] == 'a')
        self.assertEqual((row['pipeline_stage'], row['stage']), ('scope', 'scope'))
        self.assertEqual(row['origin_request_file'], 'name/initial/llm_name_title_format_judgments.jsonl')
        self.assertLessEqual(row['maximum_effective_opportunities'], 2)
        self.assertEqual(len({row['id'] for row in plan['eligible']}), len(plan['eligible']))

    def test_real_body_chain_and_result_rebuild_reuse_completed_calls_after_compensation(self):
        module = self.module()
        current, original, _, evidence, capture, _, _ = self.compensation_fixture()
        content = isolated_dictionary_content()
        # Runner.select imports content_units lazily. Keep its native legacy alias
        # scoped to this test and restore the root controller module afterwards.
        legacy_module_patch = patch.dict(sys.modules, {'pipeline': content.c.legacy})
        legacy_module_patch.start()
        self.addCleanup(legacy_module_patch.stop)
        row = {**self.rows[0], 'id': 'c', 'head': 'C', 'knowledge_point': 'C',
            'raw_content': 'C is a concept.', 'source': {'identifier': 'B001', 'head_line': 1},
            'source_context': {'head_context': [{'line': 1, 'text': 'C'}], 'body_edges': []}}
        for folder, number, code in ((original, 1, 404), (current, 3, 503)):
            (folder / 'content/calls/explanation/c.json').unlink()
            runner = content.Runner(folder / 'content', dict(self.cfg, staged_round=number))
            with patch.object(content.urllib.request, 'urlopen', side_effect=HTTPError(
                    self.cfg['api_url'], code, 'Not Found' if code == 404 else 'Busy', {}, None)):
                failed_report = runner.content(row)
                self.assertEqual(failed_report['status'], 'failed')
                self.assertTrue((folder / 'content/calls/explanation/c.json').exists(), failed_report)
        sealed = native.read(current / 'SOURCE_CACHE.json')
        sealed['files']['content/calls/explanation/c.json'] = native.digest(original / 'content/calls/explanation/c.json')
        native.write(current / 'SOURCE_CACHE.json', sealed)
        plan_path = current.parent / 'REAL_BODY_PLAN.json'
        native.write(plan_path, module.compensation_plan(original, current,
            module.deployment_revocation_proof(current, evidence, capture, self.cfg['api_url'])))
        cfg = dict(self.cfg, staged_round=3, compensation_mode=True, compensation_ids=['c'],
                   compensation_token={'path': str(plan_path), 'sha256': native.digest(plan_path)})
        stages = []
        def response(request, **_):
            packet = json.loads(json.loads(request.data)['messages'][1]['content'])
            stages.append('alignment' if 'text_fields' in packet else 'definition' if len(stages) else 'explanation')
            if 'text_fields' in packet:
                vote = {'record_decision': 'keep', 'reason': 'Literal evidence',
                    'fields': {key: 'keep' for key in content.c.FIELDS},
                    'entry_check': {'kind': 'independent', 'name_complete': True, 'evidence': 'C'},
                    'definition_checks': {'en_definition': {'role': 'definition', 'needs_prior': False,
                        'identifies_subject': True, 'evidence': 'C is a concept.'}}}
            else:
                vote = {'ranges': [{'first': 0, 'last': 0, 'language': 'en'}], 'exclude_units': []}
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(vote)}}]}).encode())
        runner = content.Runner(current / 'content', cfg)
        with patch.object(content.urllib.request, 'urlopen', side_effect=response):
            self.assertEqual(runner.batch([row], 'results', runner.content)[0]['status'], 'keep')
            native.write(current / 'content/results/c.json', {'id': 'c', 'status': 'failed', 'error': 'interrupted aggregate'})
            self.assertEqual(runner.batch([row], 'results', runner.content)[0]['status'], 'keep')
        self.assertEqual(stages, ['explanation', 'definition', 'alignment'])

    def test_code_migration_proof_requires_equal_prompts_and_exact_old_new_hashes(self):
        module = self.module()
        build = getattr(module, 'code_migration_proof', None)
        self.assertTrue(callable(build), 'operational QA3 to final code changes require a consumed identity proof')
        roots = [self.root / name for name in ('qa3_code', 'final_code', 'frozen_code')]
        for i, root in enumerate(roots):
            root.mkdir()
            (root / 'policy.py').write_text(f'PROMPT = "same"\nworkers = {i}\n', encoding='utf-8')
        proof = build(*roots)
        self.assertEqual(proof['prompt_identity'], module.prompt_identity(roots[0]))
        self.assertNotEqual(proof['old']['files'], proof['new']['files'])
        path = self.root / 'CODE_MIGRATION_PROOF.json'
        native.write(path, proof)
        token = {'path': str(path), 'sha256': native.digest(path)}
        module.verify_code_migration(token, proof['old']['files'], proof['new']['files'])
        with self.assertRaisesRegex(ValueError, 'identity'):
            module.verify_code_migration(token, {}, proof['new']['files'])
        (roots[1] / 'policy.py').write_text('PROMPT = "changed"\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'prompt'):
            build(*roots)

    def test_terminal_earlier_rounds_with_no_transport_retry_do_not_block_finalization(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_early_terminal', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        current, _, _, _, _, _, _ = self.compensation_fixture()
        job = {'out': str(current), 'job_id': 'test'}
        for number in (1, 2):
            native.write(current / 'STATE.json', {'status': 'round_complete', 'round': number})
            native.write(current / 'ORCHESTRATION_STATUS.json', {'state': 'technical_failure',
                'round': number, 'next_round': None, 'retryable_technical_failures': 0})
            self.assertIsNone(controller._prepare_compensation_job(job, {}))
        native.write(current / 'STATE.json', {'status': 'round_running', 'round': 3})
        with self.assertRaisesRegex(ValueError, 'completed|running|cohort'):
            controller._prepare_compensation_job(job, {})

    def test_explanation_retirement_credit_follows_a_failed_definition_once(self):
        module = self.module()
        current, original, _, evidence, capture, _, latest = self.compensation_fixture()
        native.write(current / 'content/calls/explanation/c.json', dict(latest, status='ok', round=2))
        native.write(current / 'content/calls/definition_en/c.json', dict(latest, packet={'subject': 'C', 'units': []},
                                                                      prompt='definition', round=3))
        proof = module.deployment_revocation_proof(current, evidence, capture, self.cfg['api_url'])
        plan = module.compensation_plan(original, current, proof)
        row = next(row for row in plan['eligible'] if row['id'] == 'c')
        self.assertEqual(row['stage'], 'definition_en')
        self.assertEqual(row['origin_request_file'], 'content/calls/explanation/c.json')
        token_path = current.parent / 'FOLLOWED_PLAN.json'
        native.write(token_path, plan)
        cfg = {'compensation_ids': ['c'], 'compensation_token': {
            'path': str(token_path), 'sha256': native.digest(token_path)}}
        self.assertTrue(module.claim_compensation(cfg, 'c', 'definition_en',
            native.read(current / 'content/calls/definition_en/c.json')))
        self.assertFalse(any(row['id'] == 'c' for row in module.compensation_plan(original, current, proof)['eligible']),
                         'regenerating another plan must never mint a second retirement credit')

    def test_controller_compensation_cli_pins_the_plan_before_launching_its_worker(self):
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_compensation_cli', script)
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        current, _, _, evidence, capture, _, _ = self.compensation_fixture()
        root = current.parent
        (root / 'EVIDENCE.json').write_bytes(evidence.read_bytes())
        (root / 'READONLY_CAPTURE.json').write_bytes(capture.read_bytes())
        job = {'job_id': 'test', 'out': str(current), 'home': str(root), 'command': ['python', 'adapter']}
        commands, caps = [], []
        from compensation_recovery import home
        def child(command, **_):
            commands.append(command)
            token = native.read(root / 'JOBS.json')[0]['compensation_plan']
            self.assertEqual(command[command.index('--compensation-plan') + 1], token['path'])
            self.assertEqual(command[command.index('--compensation-sha256') + 1], token['sha256'])
            self.assertEqual(command[command.index('--workers') + 1], '64')
            native.write(home(current) / token['sha256'] / 'RESULT.json', {'plan_sha256': token['sha256']})
            return types.SimpleNamespace(returncode=0)
        with patch.object(controller, 'ROOT', root), patch.object(controller.subprocess, 'run', side_effect=child), \
                patch.object(controller, 'transition_phase', side_effect=lambda _url, cap, _phase: caps.append(cap)):
            controller._compensate_jobs([job], self.cfg, types.SimpleNamespace(api_url='http://offline'))
            controller._compensate_jobs([job], self.cfg, types.SimpleNamespace(api_url='http://offline'))
        self.assertEqual(len(commands), 1)
        self.assertEqual(caps, [64])

    def test_upstream_compensation_credit_can_rebuild_a_result_from_successful_body_calls(self):
        module = self.module()
        current, original, _, evidence, capture, _, _ = self.compensation_fixture()
        plan_path = current.parent / 'UPSTREAM_PLAN.json'
        native.write(plan_path, module.compensation_plan(original, current,
            module.deployment_revocation_proof(current, evidence, capture, self.cfg['api_url'])))
        token = {'path': str(plan_path), 'sha256': native.digest(plan_path)}
        saved = {'status': 'ok', 'round': 3, 'packet': {'subject': 'A'}, 'prompt': 'same',
                 'compensation': {'plan_sha256': token['sha256'], 'first_downstream_request': True}}
        native.write(current / 'content/calls/explanation/a.json', saved)
        native.write(current / 'content/results/a.json', {'id': 'a', 'status': 'failed', 'error': 'old aggregate'})
        content = isolated_dictionary_content()
        runner = content.Runner(current / 'content', dict(self.cfg, staged_round=3, compensation_mode=True,
            compensation_ids=['a'], compensation_token=token))
        def rebuild(row):
            cached = runner._staged_call('a', 'explanation', saved['packet'], saved['prompt'], lambda _: None)
            self.assertEqual(cached['status'], 'ok')
            return {'id': 'a', 'status': 'keep', 'record': row}
        with patch.object(content.urllib.request, 'urlopen', side_effect=AssertionError('successful cache replayed')):
            self.assertEqual(runner.batch([self.rows[0]], 'results', rebuild)[0]['status'], 'keep')

    def test_only_new_authorized_dead_writer_locks_can_be_removed(self):
        module = self.module()
        verify = getattr(module, 'verify_writer_idle', None)
        self.assertTrue(callable(verify), 'resume must distinguish a dead new writer from a live/unknown/source lock')
        target, _, receipt = self.migrate()
        lock = target / '.running'
        owner = {'schema': 1, 'kind': 'dictionary_resumed_writer', 'host': module.writer_host(),
                 'pid': 987654, 'process_start_ticks': 111, 'target': str(target),
                 'resume_receipt_sha256': native.digest(receipt)}
        native.write(lock, owner)
        with patch.object(module.os, 'kill', side_effect=ProcessLookupError('dead pid')):
            verify(target, receipt, clear_stale=True)
        self.assertFalse(lock.exists())
        self.assertTrue((target.parent / (target.name + '_LOCK_RECOVERY.jsonl')).exists())
        native.write(lock, owner)
        with patch.object(module.os, 'kill', return_value=None):
            with self.assertRaisesRegex(ValueError, 'live|active'):
                verify(target, receipt, clear_stale=True)
        self.assertTrue(lock.exists())
        native.write(self.source / '.running', owner)
        with patch.object(module.os, 'kill', side_effect=ProcessLookupError('dead pid')):
            with self.assertRaisesRegex(ValueError, 'authorized|target|independent'):
                verify(self.source, receipt, clear_stale=True)
        self.assertTrue((self.source / '.running').exists())
        for malformed in ({'pid': 987654}, dict(owner, host='another-host'), dict(owner, process_start_ticks=None)):
            native.write(lock, malformed)
            with patch.object(module.os, 'kill', side_effect=ProcessLookupError('dead pid')):
                with self.assertRaises(ValueError):
                    verify(target, receipt, clear_stale=True)
            self.assertTrue(lock.exists())

    def test_new_resume_lock_records_owner_identity_and_blocks_commit_from_other_live_worker(self):
        module = self.module()
        lock_factory = getattr(module, 'resumed_book_lock', None)
        self.assertTrue(callable(lock_factory), 'new resumed workers must write recoverable lock identity')
        target, _, receipt = self.migrate()
        with patch.object(module, 'process_start_ticks', return_value=123456):
            with lock_factory(target, receipt):
                owner = native.read(target / '.running')
                self.assertEqual(owner['process_start_ticks'], 123456)
                self.assertEqual(owner['host'], module.writer_host())
                self.assertEqual(owner['pid'], os.getpid())
                module.verify_writer_idle(target, receipt, allow_current=True)
                from compensation_recovery import recover_commit, home
                native.write(home(target) / 'ACTIVE.json', {'state': 'committing', 'plan_sha256': 'unused',
                    'commit': {'path': 'must-not-be-read', 'sha256': 'unused'}})
                native.write(target / '.running', dict(owner, pid=os.getpid() + 100000))
                with patch.object(module.os, 'kill', return_value=None):
                    with self.assertRaisesRegex(ValueError, 'live|active'):
                        recover_commit(target)
                native.write(target / '.running', owner)
        self.assertFalse((target / '.running').exists())

    def round_baseline_fixture(self, round_number):
        module = self.module()
        target = self.root / ('round_baseline_' + str(round_number)) / 'dictionary'
        config = dict(self.cfg, api_url='http://127.0.0.1:2')
        rows = [{**row, 'head': row['id'].upper(), 'raw_content': row['id'].upper() + ' is a concept.',
                 'source': {'identifier': 'B001', 'head_line': 1}, 'source_context': {
                     'head_context': [{'line': 1, 'text': row['id'].upper()}], 'body_edges': []}}
                for row in self.rows]
        input_path = self.root / ('ROUND_INPUT_' + str(round_number) + '.json')
        native.write(input_path, rows)
        native.write(self.books[0]['scope_config'], {'subject': 'Test', 'subject_definition': 'Definition',
            'subject_boundary': 'Boundary', 'l1_nodes': [{'name': 'L1', 'definition': 'Definition', 'boundary': 'Boundary'}]})
        native.write(target / 'MANIFEST.json', native.staged_manifest(input_path, config, self.books))
        keep = {'decision': 'keep', 'api_status': 'ok'}
        failed = {'decision': 'review', 'api_status': 'api_error_after_retries', 'failure_kind': 'transport'}
        names = [{**row, native.NAME: keep if round_number == 3 and row['id'] == 'a' else failed} for row in rows]
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            native.jsonl(target / 'name' / name, names)
        content = isolated_dictionary_content()
        if round_number == 3:
            group = 'scope/group_' + native.hashlib.sha256(self.books[0]['scope_config'].encode()).hexdigest()[:12]
            for name in ('FIRST.jsonl', 'FINAL.jsonl'):
                native.jsonl(target / group / name, [{**names[0], native.SCOPE: keep}])
            with patch.dict(sys.modules, {'pipeline': content.c.legacy}), patch.object(
                    content.urllib.request, 'urlopen', side_effect=HTTPError(config['api_url'], 502, 'Busy', {}, None)):
                report = content.Runner(target / 'content', dict(config, staged_round=2)).content(rows[0])
            native.write(target / 'content/results/a.json', report)
            self.assertTrue((target / 'content/calls/explanation/a.json').exists(), report)
        ledger = [{'id': row['id'], 'name': names[i][native.NAME],
                   'scope': keep if round_number == 3 and row['id'] == 'a' else None,
                   'content_status': 'failed' if round_number == 3 and row['id'] == 'a' else 'not_reached',
                   'content_failure_kind': 'transport' if round_number == 3 and row['id'] == 'a' else None}
                  for i, row in enumerate(rows)]
        for name in ('DISPOSITIONS.jsonl', 'TECHNICAL_FAILURES.jsonl', 'REVIEW.jsonl'):
            native.jsonl(target / name, ledger)
        native.jsonl(target / 'FINAL_RECORDS.jsonl', [])
        native.write(target / 'STANDARD_RECORDS.json', [])
        native.write(target / 'SUMMARY.json', {'input': 2, 'technical_failures': 2, 'review': 2, 'final_keep': 0,
                                              'round': round_number - 1, 'semantic_quality_approved': False})
        previous = {'state': 'awaiting_retry', 'round': round_number - 1, 'next_round': round_number,
                    'checkpoint_hashes': native.staged_checkpoint_hashes(target)}
        native.write(target / 'ORCHESTRATION_STATUS.json', previous)
        native.write(target / 'STATE.json', {'status': 'round_complete', 'round': round_number - 1})
        receipt = target.parent / 'dictionary_RESUME_RECEIPT.json'
        native.write(receipt, {'schema': 1, 'target': str(target), 'source': str(self.source),
            'source_files': module.snapshot_files(self.source), 'old_manifest': native.read(self.source / 'MANIFEST.json'),
            'new_manifest': native.read(target / 'MANIFEST.json'), 'retained_passed': module.retained_passed_baseline(target),
            'files': module.snapshot_files(target), 'round_complete': True,
            'completed_round': round_number - 1, 'resume_round': round_number,
            'stable_completed_prefix_verified': True, 'semantic_quality_approved': False})
        return target, config, input_path, rows, previous, receipt, content

    def test_fresh_round_two_uses_exact_compressed_baseline_and_never_captures_a_running_mix(self):
        module = self.module()
        capture = getattr(module, 'capture_round_baseline', None)
        self.assertTrue(callable(capture), 'new rounds need an immutable previous-prefix baseline')
        target, config, input_path, rows, previous, receipt, content = self.round_baseline_fixture(2)
        token = capture(target, 2, previous, receipt)
        native.write(target / 'STATE.json', {'status': 'round_running', 'round': 2, 'round_baseline': token})
        with self.assertRaisesRegex(ValueError, 'complete|running'):
            capture(target, 2, previous, receipt)
        formatter = module._filter_module(native.ROOT / 'name_filters/01_name_title_quality.py')
        votes = [{'id': row['id'], 'line_no': i + 1, 'decision': 'review',
                  'api_status': 'api_error_after_retries', 'api_error': '<HTTPError 503: Busy>',
                  'failure_kind': 'transport', 'policy_version': formatter.POLICY_VERSION} for i, row in enumerate(rows)]
        votes[0].update(decision='keep', api_status='ok', failure_kind=None)
        native.jsonl(target / 'name/round_2/input.jsonl', rows)
        native.jsonl(target / 'name/round_2/llm_name_title_format_judgments.jsonl', votes)
        projected = [module._project_filter(formatter, row, vote, i + 1) for i, (row, vote) in enumerate(zip(rows, votes))]
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            native.jsonl(target / 'name' / name, projected)
        report = module.inspect_checkpoint(target, input_path, native)
        self.assertFalse(report['round_complete'])
        self.assertEqual(report['resume_round'], 2)
        self.assertTrue(any('round baseline' in row['method'] for row in report['historical_seed_proofs']))

    def test_round_baseline_copies_only_body_calls_that_can_be_overwritten(self):
        module = self.module()
        target, _, _, _, previous, receipt, _ = self.round_baseline_fixture(2)
        ledger = native.loadl(target / 'DISPOSITIONS.jsonl')
        ledger[0].update(content_status='failed', content_failure_kind='transport')
        native.jsonl(target / 'DISPOSITIONS.jsonl', ledger)
        for stage, status, kind in (('explanation', 'ok', None), ('definition_en', 'failed', 'transport'),
                                    ('alignment', 'failed', 'content')):
            native.write(target / 'content/calls' / stage / 'a.json', {
                'packet': {}, 'prompt': stage, 'round': 1, 'status': status, 'failure_kind': kind})
        previous['checkpoint_hashes'] = native.staged_checkpoint_hashes(target)
        native.write(target / 'ORCHESTRATION_STATUS.json', previous)
        token = module.capture_round_baseline(target, 2, previous, receipt)
        files = native.read(token['path'])['files']
        self.assertIn('content/calls/definition_en/a.json', files)
        self.assertNotIn('content/calls/explanation/a.json', files)
        self.assertNotIn('content/calls/alignment/a.json', files)

    def test_round_three_resume_proves_overwritten_aggregates_body_and_gateway_manifest(self):
        module = self.module()
        capture = getattr(module, 'capture_round_baseline', None)
        self.assertTrue(callable(capture), 'round-three resume must retain completed round-two bytes')
        target, config, input_path, rows, previous, receipt, content = self.round_baseline_fixture(3)
        token = capture(target, 3, previous, receipt)
        native.write(target / 'STATE.json', {'status': 'round_running', 'round': 3, 'round_baseline': token})
        formatter = module._filter_module(native.ROOT / 'name_filters/01_name_title_quality.py')
        vote = {'id': 'b', 'line_no': 1, 'decision': 'review', 'api_status': 'api_error_after_retries',
                'api_error': '<HTTPError 504: Busy>', 'failure_kind': 'transport', 'policy_version': formatter.POLICY_VERSION}
        native.jsonl(target / 'name/round_3/input.jsonl', rows[1:])
        native.jsonl(target / 'name/round_3/llm_name_title_format_judgments.jsonl', [vote])
        first = native.loadl(target / 'name/FIRST.jsonl')
        first[1] = module._project_filter(formatter, rows[1], vote, 1)
        for name in ('FIRST.jsonl', 'FINAL.jsonl'):
            native.jsonl(target / 'name' / name, first)
        saved = native.read(target / 'content/calls/explanation/a.json')
        with patch.object(content.urllib.request, 'urlopen', side_effect=HTTPError(config['api_url'], 503, 'Busy', {}, None)):
            content.Runner(target / 'content', dict(config, staged_round=3))._staged_call(
                'a', 'explanation', saved['packet'], saved['prompt'], lambda _: None)
        native.write(target / 'content/results/a.json', {'id': 'a', 'status': 'failed', 'error': 'current round failed'})
        summary = native.read(target / 'SUMMARY.json');summary['elapsed_seconds'] = 5
        native.write(target / 'SUMMARY.json', summary)
        inspection = module.inspect_checkpoint(target, input_path, native)
        value = native.read(receipt)
        value.update({key: inspection[key] for key in ('completed_round', 'resume_round', 'round_complete', 'files')})
        native.write(receipt, value)
        script = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_dictionary.py'
        spec = importlib.util.spec_from_file_location('resume_round_baseline_gateway', script)
        controller = importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
        books_path = target.parent / 'books.json';native.write(books_path, self.books)
        job = {'out': str(target), 'job_id': 'baseline', 'input': str(input_path), 'books': str(books_path),
               'command': ['python', '--api-url', config['api_url']], 'resume_receipt': str(receipt)}
        for port in (3, 4):
            controller._rebind_resumed_job(job, types.SimpleNamespace(job_url=lambda _job: f'http://127.0.0.1:{port}'))
            self.assertEqual(module.inspect_checkpoint(target, input_path, native)['resume_round'], 3)
        model_posts = []
        def no_models(request, **_):
            if not isinstance(request, str):
                model_posts.append(request.full_url)
                raise AssertionError('completed same-round body request was replayed')
            return io.BytesIO(b'{"data":[{"id":"test"}]}')
        config = dict(config, api_url='http://127.0.0.1:4')
        with patch.dict(sys.modules, {'pipeline': content.c.legacy, 'clean_boundary_v46': content}), \
                patch.object(native.subprocess, 'run', side_effect=AssertionError('completed filter replayed')), \
                patch.object(native.urllib.request, 'urlopen', side_effect=no_models):
            result = native.run_cleaning_round(input_path, config, target, self.books, 3, resume_receipt=receipt)
        self.assertEqual(result['round'], 3)
        self.assertEqual(native.read(target / 'STATE.json')['status'], 'round_complete')
        self.assertEqual(model_posts, [])
        # A kill after ORCH's durable completion receipt but before STATE's final write.
        native.write(target / 'STATE.json', {'status': 'round_running', 'round': 3, 'round_baseline': token})
        pending = module.inspect_checkpoint(target, input_path, native)
        self.assertTrue(pending['round_complete'])
        self.assertTrue(pending.get('native_completion_pending'))
        controller._rebind_resumed_job(job, types.SimpleNamespace(job_url=lambda _: 'http://127.0.0.1:5'))
        self.assertEqual(native.read(target / 'STATE.json')['status'], 'round_complete')
        self.assertEqual(native.read(target / 'STATE.json')['round'], 3)

    def test_dead_lock_cleanup_and_new_owner_creation_share_one_guard(self):
        module = self.module()
        target, _, receipt = self.migrate()
        lock = target / '.running'
        native.write(lock, {'schema': 1, 'kind': 'dictionary_resumed_writer', 'host': module.writer_host(),
            'pid': 987654, 'process_start_ticks': 111, 'target': str(target),
            'resume_receipt_sha256': native.digest(receipt)})
        validated, release, attempted, acquired, leave = [threading.Event() for _ in range(5)]
        errors = []
        unlink = Path.unlink
        def paused_unlink(path, *args, **kwargs):
            if path == lock and threading.current_thread().name == 'stale_cleaner':
                validated.set()
                if not release.wait(5):
                    raise AssertionError('test did not release the stale cleaner')
            return unlink(path, *args, **kwargs)
        def cleaner():
            try:
                module.verify_writer_idle(target, receipt, clear_stale=True)
            except BaseException as error:
                errors.append(repr(error))
        def writer():
            try:
                attempted.set()
                with module.resumed_book_lock(target, receipt):
                    acquired.set()
                    if not leave.wait(5):
                        raise AssertionError('test did not release the new writer')
            except BaseException as error:
                errors.append(repr(error))
        with patch.object(module.os, 'kill', side_effect=ProcessLookupError('old owner is dead')), \
                patch.object(Path, 'unlink', paused_unlink):
            old = threading.Thread(target=cleaner, name='stale_cleaner');old.start()
            self.assertTrue(validated.wait(5))
            new = threading.Thread(target=writer, name='new_writer');new.start()
            self.assertTrue(attempted.wait(5))
            blocked = not acquired.wait(0.1)
            release.set()
            self.assertTrue(acquired.wait(5))
            old.join(5)
            owner_present = lock.exists()
            leave.set();new.join(5)
        self.assertTrue(blocked, 'a new owner entered between another cleanup check and unlink')
        self.assertTrue(owner_present)
        self.assertEqual(errors, [])
        self.assertFalse(lock.exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX flock cross-process regression')
    def test_dead_lock_guard_blocks_another_process_until_cleanup_finishes(self):
        import select
        module = self.module()
        target, _, receipt = self.migrate()
        lock = target / '.running'
        native.write(lock, {'schema': 1, 'kind': 'dictionary_resumed_writer', 'host': module.writer_host(),
            'pid': 2147483647, 'process_start_ticks': 111, 'target': str(target),
            'resume_receipt_sha256': native.digest(receipt)})
        validated, release = threading.Event(), threading.Event()
        errors = []
        original_unlink = Path.unlink
        def paused_unlink(path, *args, **kwargs):
            if path == lock and threading.current_thread().name == 'stale_cleaner':
                validated.set()
                if not release.wait(10):
                    raise AssertionError('test did not release the stale cleaner')
            return original_unlink(path, *args, **kwargs)
        def cleaner():
            try:
                module.verify_writer_idle(target, receipt, clear_stale=True)
            except BaseException as error:
                errors.append(repr(error))
        script = '''import fcntl, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import resume_integrity as integrity
original_flock = fcntl.flock
def observed_flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        print("ATTEMPT", flush=True)
    return original_flock(fd, operation)
fcntl.flock = observed_flock
with integrity.resumed_book_lock(Path(sys.argv[2]), Path(sys.argv[3])):
    print("ACQUIRED", flush=True)
    sys.stdin.readline()
'''
        child = None
        with patch.object(Path, 'unlink', paused_unlink):
            old = threading.Thread(target=cleaner, name='stale_cleaner');old.start()
            try:
                self.assertTrue(validated.wait(5))
                child = subprocess.Popen([sys.executable, '-u', '-c', script,
                    str(Path(module.__file__).parent), str(target), str(receipt)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'child never reached OS flock')
                self.assertEqual(child.stdout.readline().strip(), 'ATTEMPT')
                self.assertFalse(select.select([child.stdout], [], [], .1)[0],
                                 'another process acquired an owner while stale cleanup still held its guard')
                release.set();old.join(5)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'child did not acquire after cleanup')
                self.assertEqual(child.stdout.readline().strip(), 'ACQUIRED')
                self.assertEqual(native.read(lock)['pid'], child.pid)
                child.stdin.write('\n');child.stdin.flush()
                self.assertEqual(child.wait(5), 0)
                self.assertEqual(errors, [])
                self.assertFalse(lock.exists())
            finally:
                release.set();old.join(5)
                if child is not None:
                    if child.poll() is None:
                        child.terminate();child.wait(5)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        stream.close()


if __name__ == '__main__':
    unittest.main()
