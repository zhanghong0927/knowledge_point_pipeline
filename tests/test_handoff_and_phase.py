"""Offline regressions for native handoffs, incomplete cleaning, and phase receipts."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import staged_benchmark as bench
from benchmark_support import Recorder
from stage_scheduling import ResourceProfile
from validate_offline import offline_network_guard
import modules.cleaning.scripts.model_clean as model_clean


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def write_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')


def resume_module():
    path = Path(__file__).resolve().parent / 'fixtures/legacy_resume/resume_mounting.py'
    spec = importlib.util.spec_from_file_location('tested_resume_mounting', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DictionaryMountHandoffTests(unittest.TestCase):
    def test_only_native_prepared_mounts_enter_boundary_generation(self):
        resume = resume_module()
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            (root / 'dictionary').mkdir()
            out = root / 'clean'
            write_rows(out / 'TECHNICAL_FAILURES.jsonl', [])
            cleaned = [{'subject': 'good', 'out': str(out)}]
            mounts = [{'job_id': name, 'subject': name, 'home': str(root / name)}
                      for name in ('good', 'failed', 'empty')]

            def selected(job, stage, *tasks):
                if tasks == ('prepare_mounting',):
                    if job['job_id'] != 'good':
                        return {'status': job['job_id']}
                    write_json(Path(job['home']) / 'run/05_mounting/PREPARED.json', {'records': 1})
                return {'status': 'completed'}

            def transport(_root, _track, _gateway, _profile, _recorder, jobs, task):
                if task == 'generate_mounting_boundaries':
                    for job in jobs:
                        self.assertTrue((Path(job['home']) / 'run/05_mounting/PREPARED.json').is_file(),
                                        'boundary generation received an unprepared mount')
                    self.assertEqual([job['job_id'] for job in jobs], ['good'])
                return jobs, {}

            with patch.object(bench, 'generate_boundaries', return_value=({'good': {'status': 'completed'}}, root / 'cache')), \
                 patch.object(bench, 'prepare_mounts', return_value=mounts), \
                 patch.object(bench, 'selected_job', side_effect=selected), \
                 patch.object(bench, 'run_mount_transport', side_effect=transport), \
                 patch.object(bench, 'final_output_totals', return_value={'missing_exports': 0}), \
                 patch.object(bench, 'mounting_dispositions', return_value=(0, {}, 0)):
                summary = resume.finish_dictionary(root, cleaned, {}, SimpleNamespace(), ResourceProfile())
            self.assertEqual(summary['completed_mounting_subjects'], 1)
            self.assertIn('mounting_prepare', summary['unresolved_stages'])
            self.assertEqual(summary['prepared_mounting_jobs'], 1)

    def test_no_mounting_input_remains_explicitly_unresolved(self):
        resume = resume_module()
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            with patch.object(bench, 'generate_boundaries', return_value=({}, root / 'cache')), \
                 patch.object(bench, 'prepare_mounts', return_value=[]), \
                 patch.object(bench, 'run_mount_transport', return_value=([], {})):
                summary = resume.finish_dictionary(root, [], {}, SimpleNamespace(), ResourceProfile())
            self.assertIn('no_mounting_input', summary['unresolved_stages'])
            self.assertEqual(summary['completed_mounting_subjects'], 0)


class CleaningCompletionTests(unittest.TestCase):
    def fixture(self, root, rows):
        output = root / 'run/04_cleaning/current/02_model_clean'
        source = root / 'run/04_cleaning/current/01_rule_clean/rule_pass.jsonl'
        write_rows(source, rows)
        write_json(root / 'taxonomy.json', [])
        write_json(root / 'pipeline.json', {})
        command = [sys.executable, str(Path(model_clean.__file__)), '--input', str(source),
                   '--taxonomy', str(root / 'taxonomy.json'), '--output-dir', str(output),
                   '--subject', 'fixture', '--api-url', 'http://127.0.0.1:9/v1',
                   '--model', 'literal-fake', '--workers', '1', '--batch-size', '1']
        task = {'command': command, 'produces': [str(output / name) for name in
                ('clean_standard.jsonl', 'model_clean_report.json', 'model_judgments.jsonl')]}
        return {'job_id': 'book', 'home': str(root), 'config': str(root / 'pipeline.json')}, task, output

    def finalize(self, job, task, results):
        output = Path(task['produces'][0]).parent
        output.mkdir(parents=True, exist_ok=True)
        connection = model_clean.init_db(output / 'model_clean_checkpoint.sqlite')
        try:
            model_clean.save_results(connection, results)
        finally:
            connection.close()
        with patch.object(sys, 'argv', ['model_clean.py', *task['command'][2:], '--finalize-only']):
            model_clean.main()
        write_json(Path(job['home']) / 'run/pipeline_state.json', {'04.model_clean': {
            'status': 'completed', 'returncode': 0, 'command': task['command']}})

    def test_missing_judgments_from_failed_child_remain_retryable(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            job, _, _ = self.fixture(root, [{'cleaning_record_id': 'missing'}])
            write_json(root / 'run/pipeline_state.json', {'04.model_clean': {
                'status': 'failed', 'returncode': 1}})
            self.assertGreater(bench.important_clean_transport_errors(job), 0)

    def test_resume_after_early_crash_preserves_semantic_drop_and_repairs_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            job, task, output = self.fixture(root, [
                {'cleaning_record_id': 'drop', 'name': 'dropped'},
                {'cleaning_record_id': 'missing', 'name': 'missing'}])
            output.mkdir(parents=True)
            connection = model_clean.init_db(output / 'model_clean_checkpoint.sqlite')
            try:
                model_clean.save_results(connection, [{'key': 'drop', 'result': {
                    'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}}])
            finally:
                connection.close()
            write_json(root / 'run/pipeline_state.json', {'04.model_clean': {
                'status': 'failed', 'returncode': 1, 'command': task['command']}})
            requested = []

            def process(_number, batch, _prompt, _args):
                requested.extend(row['key'] for row in batch)
                return {'results': [{'key': row['key'], 'result': {
                    'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}}
                    for row in batch], 'raw': [], 'tokens': 0}

            def run(_home, _label, command):
                with patch.object(sys, 'argv', ['model_clean.py', *command[2:]]), \
                     patch.object(model_clean, 'process_batch', side_effect=process):
                    return model_clean.main()

            with patch.object(bench, 'native_task', return_value=task), \
                 patch.object(bench, 'run_command', side_effect=run):
                result = bench.retry_important_clean(job, 1)
            self.assertTrue((output / 'model_judgments.jsonl').is_file(),
                            'an early child crash was omitted from the retry cohort')
            self.assertEqual(requested, ['missing'])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(bench.read(root / 'run/pipeline_state.json')['04.model_clean']['status'], 'completed')
            self.assertEqual(bench.important_clean_transport_errors(job), 0)

    def test_complete_empty_cleaning_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            job, task, _ = self.fixture(Path(tmp), [])
            self.finalize(job, task, [])
            self.assertEqual(bench.important_clean_transport_errors(job), 0)
            with patch.object(bench, 'native_task', return_value=task), \
                 patch.object(bench, 'run_command', side_effect=AssertionError('empty cleaning requested model')):
                self.assertEqual(bench.retry_important_clean(job, 1)['status'], 'no_technical_failures')

    def test_complete_semantic_drop_and_format_failure_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            job, task, _ = self.fixture(Path(tmp), [
                {'cleaning_record_id': 'drop'}, {'cleaning_record_id': 'format'}])
            self.finalize(job, task, [
                {'key': 'drop', 'result': {'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}},
                {'key': 'format', 'result': {'decision': 'error', 'failure_kind': 'content_or_format',
                                          'policy_version': model_clean.MODEL_POLICY_VERSION}}])
            self.assertEqual(bench.important_clean_transport_errors(job), 0)
            with patch.object(bench, 'native_task', return_value=task), \
                 patch.object(bench, 'run_command', side_effect=AssertionError('semantic result requested again')):
                self.assertEqual(bench.retry_important_clean(job, 1)['status'], 'no_technical_failures')

    def test_partial_transport_retry_reuses_existing_drop_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            job, task, _ = self.fixture(Path(tmp), [
                {'cleaning_record_id': 'drop'}, {'cleaning_record_id': 'transport'}])
            self.finalize(job, task, [
                {'key': 'drop', 'result': {'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}},
                {'key': 'transport', 'result': {'decision': 'error', 'failure_kind': 'transport',
                                             'policy_version': model_clean.MODEL_POLICY_VERSION}}])
            requested = []
            def process(_number, batch, _prompt, _args):
                requested.extend(row['key'] for row in batch)
                return {'results': [{'key': row['key'], 'result': {
                    'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}}
                    for row in batch], 'raw': [], 'tokens': 0}
            def run(_home, _label, command):
                with patch.object(sys, 'argv', ['model_clean.py', *command[2:]]), \
                     patch.object(model_clean, 'process_batch', side_effect=process):
                    return model_clean.main()
            with patch.object(bench, 'native_task', return_value=task), \
                 patch.object(bench, 'run_command', side_effect=run):
                result = bench.retry_important_clean(job, 1)
            self.assertEqual(requested, ['transport'])
            self.assertEqual(result['remaining'], 0)
            self.assertEqual(bench.important_clean_transport_errors(job), 0)

    def test_missing_expected_export_retries_even_with_complete_judgments(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            job, task, output = self.fixture(Path(tmp), [{'cleaning_record_id': 'drop'}])
            self.finalize(job, task, [{'key': 'drop', 'result': {
                'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}}])
            (output / 'clean_standard.jsonl').unlink()
            self.assertGreater(bench.important_clean_transport_errors(job), 0)

    def test_failed_native_receipt_requires_reconciliation_before_completion(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            job, task, _ = self.fixture(root, [])
            self.finalize(job, task, [])
            write_json(root / 'run/pipeline_state.json', {'04.model_clean': {
                'status': 'failed', 'returncode': 1, 'command': task['command']}})
            self.assertGreater(bench.important_clean_transport_errors(job), 0)

    def test_damaged_checkpoint_cannot_repeat_an_existing_semantic_drop(self):
        for damage in ('missing', 'corrupt', 'missing_row', 'changed_payload', 'changed_policy'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp, offline_network_guard():
                root = Path(tmp)
                job, task, output = self.fixture(root, [{'cleaning_record_id': 'drop'}])
                self.finalize(job, task, [{'key': 'drop', 'result': {
                    'decision': 'drop', 'policy_version': model_clean.MODEL_POLICY_VERSION}}])
                checkpoint = output / 'model_clean_checkpoint.sqlite'
                if damage == 'missing':
                    checkpoint.unlink()
                elif damage == 'corrupt':
                    checkpoint.write_bytes(b'corrupt fixture')
                else:
                    connection = sqlite3.connect(checkpoint)
                    try:
                        statement = {'missing_row': 'DELETE FROM results',
                                     'changed_payload': "UPDATE results SET payload = '{}'",
                                     'changed_policy': "UPDATE results SET policy_version = 'different-policy'"}[damage]
                        connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()
                write_json(root / 'run/pipeline_state.json', {'04.model_clean': {
                    'status': 'failed', 'returncode': 1, 'command': task['command']}})
                with patch.object(bench, 'native_task', return_value=task), \
                     patch.object(bench, 'run_command', side_effect=AssertionError('semantic drop requested again')):
                    with self.assertRaisesRegex(ValueError, 'checkpoint'):
                        bench.retry_important_clean(job, 1)


class PhaseReceiptTests(unittest.TestCase):
    def test_mounting_rounds_sync_status_and_keep_dictionary_jobs_list(self):
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root = Path(tmp)
            original_jobs = [{'home': 'frozen', 'out': 'frozen/clean'}]
            write_json(root / 'JOBS.json', original_jobs)
            write_json(root / 'STATUS.json', {'status': 'running', 'phase': 'cleaning_round_1', 'source_root': 'frozen'})
            profile = ResourceProfile()
            recorder = Recorder(root, profile)
            mount = {'job_id': 'mount', 'home': str(root / 'mount')}

            def native(job, task, number, _workers):
                state = bench.read(root / 'STATUS.json')
                self.assertEqual(state['phase'], f'dictionary_route_mounting_round_{number}')
                self.assertEqual(state['phase_limit'], profile.round_limits_for('mounting')[number - 1])
                return {'status': 'awaiting_retry' if number == 1 else 'ready_to_finalize',
                        'native': {'next_round': number + 1}}

            with patch.object(bench, 'transition_phase', return_value={'status': 'ok'}), \
                 patch.object(bench, 'run_mount_round', side_effect=native), \
                 patch.object(bench, 'finalize_mount_task', return_value={'status': 'completed'}):
                completed, _ = bench.run_mount_transport(root, 'dictionary', SimpleNamespace(api_url='http://127.0.0.1:9'),
                                                        profile, recorder, [mount], 'route_mounting')
            self.assertEqual(completed, [mount])
            self.assertEqual(bench.read(root / 'JOBS.json'), original_jobs)
            self.assertEqual(bench.read(root / 'STATUS.json')['source_root'], 'frozen')
            events = [json.loads(line) for line in (root / 'PHASE_EVENTS.jsonl').read_text().splitlines()]
            self.assertEqual([row['phase'] for row in events], [
                'dictionary_route_mounting_round_1', 'dictionary_route_mounting_round_2'])

    def test_taxonomy_assets_import_without_changing_global_model_phase(self):
        from test_project_taxonomy_assets import project_fixture
        import project_taxonomy
        with tempfile.TemporaryDirectory() as tmp, offline_network_guard():
            root=Path(tmp)
            _,_,lock=project_fixture(root)
            write_json(root/'STATUS.json',{'status':'running','phase':'cleaning_round_1'})
            with patch.object(project_taxonomy,'DEFAULT_LOCK',lock), \
                 patch.object(bench,'transition_phase',side_effect=AssertionError('No boundary model phase')):
                results,_=bench.generate_boundaries(root,['mechanical_engineering'],{},None,ResourceProfile())
            result=results['mechanical_engineering']
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['model_calls'],0)
            self.assertEqual(bench.read(root/'STATUS.json')['phase'],'cleaning_round_1')
            self.assertFalse((root/'PHASE_EVENTS.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
