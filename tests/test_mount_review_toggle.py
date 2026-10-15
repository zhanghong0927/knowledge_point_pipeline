"""Offline contracts for optional mounting model review and direct export."""

import argparse
import ast
from contextlib import ExitStack, redirect_stdout
import builtins
import inspect
from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pipeline
import staged_benchmark as bench
from stage_scheduling import ResourceProfile
from test_mounting_bridge import bridge, fake_routes, fake_review, fixture
import test_mounting_boundaries as boundary_tests


def routed_fixture(root, mode='off', records=None):
    args, original = fixture(root, records, mount_review_mode=mode)
    fake_routes(args)
    bridge.dump(args.out / 'ROUTED.json', {
        'groups': 1, 'records': len(original)})
    return args, original


class MountReviewPlanTests(unittest.TestCase):
    def test_missing_switch_schedules_direct_export_with_native_outputs(self):
        config, values = pipeline.load_config(bridge.ROOT / 'configs/pipeline.example.json')
        config['mounting'].pop('review_enabled', None)
        tasks = pipeline.plan(config, values)[4]['tasks']
        by_name = {task['name']: task for task in tasks}
        self.assertNotIn('review_mounting', by_name)
        prepare = by_name['prepare_mounting']['command']
        self.assertEqual(prepare[prepare.index('--mount-review-mode') + 1], 'off')
        export = by_name['export_mounting']
        mount = Path(values['run']) / '05_mounting'
        native_output = str(mount / 'groups' / values['slug'] / 'routed.jsonl')
        self.assertIn(str(mount / 'ROUTED.json'), export['requires'])
        self.assertIn(native_output, export['requires'])
        self.assertIn(native_output, by_name['route_mounting']['produces'])
        self.assertFalse(any('REVIEWED' in name or 'review_responses' in name
                             for name in export['requires']))

    def test_enabled_switch_keeps_review_and_receipt_dependencies(self):
        config, values = pipeline.load_config(bridge.ROOT / 'configs/pipeline.example.json')
        config['mounting']['review_enabled'] = True
        tasks = pipeline.plan(config, values)[4]['tasks']
        by_name = {task['name']: task for task in tasks}
        self.assertIn('review_mounting', by_name)
        command = by_name['prepare_mounting']['command']
        self.assertIn('--mount-review-mode', command)
        self.assertEqual(command[command.index('--mount-review-mode') + 1], 'on')
        mount = Path(values['run']) / '05_mounting'
        self.assertIn(str(mount / 'REVIEWED.json'), by_name['export_mounting']['requires'])
        self.assertIn(str(mount / 'review_responses.jsonl'), by_name['export_mounting']['requires'])

    def test_switch_rejects_non_boolean_configuration(self):
        for value in ('false', 'true', 0, 1, None, [], {}):
            with self.subTest(value=value):
                config, values = pipeline.load_config(bridge.ROOT / 'configs/pipeline.example.json')
                config['mounting']['review_enabled'] = value
                with self.assertRaisesRegex(ValueError, 'review_enabled.*boolean'):
                    pipeline.plan(config, values)

    def test_packaged_templates_schedule_review_off_explicitly(self):
        for filename in ('pipeline.example.json', 'pipeline.8card.example.json',
                         'pipeline.important_llm.example.json'):
            with self.subTest(filename=filename):
                config, values = pipeline.load_config(bridge.ROOT / 'configs' / filename)
                self.assertIs(config['mounting'].get('review_enabled'), False)
                self.assertNotIn('review_mounting', {
                    task['name'] for task in pipeline.plan(config, values)[4]['tasks']})

    def test_benchmark_default_plan_omits_disabled_model_review_phases(self):
        document = bench.plan_document(ResourceProfile())
        for track in document['tracks']:
            self.assertFalse(any(phase.startswith('mounting_review_') for phase in track['phases']))
        self.assertIs(document['mounting_review_enabled'], False)


class MountReviewExportTests(unittest.TestCase):
    def export_off(self, args):
        try:
            return bridge.export(args)
        except (FileNotFoundError, ValueError) as error:
            self.fail('Direct export unexpectedly requires review artifacts: ' + str(error))

    def test_prepare_defaults_off_and_export_cli_derives_prepared_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from test_project_taxonomy_assets import project_fixture
            from project_taxonomy import activate
            args,original,project_lock=project_fixture(root)
            args.out=root/'production';args.project_assets_lock=project_lock
            args.boundary_cache_dir=None;args.mount_review_mode=None
            bridge.prepare(args);activate(args);bridge.verify_boundaries(args)
            self.assertIs(bridge.read(args.out / 'PREPARED.json').get('mount_review_enabled'), False)
            fake_routes(args)
            bridge.dump(args.out / 'ROUTED.json', {'groups': 1, 'records': len(original)})
            argv = ['export', '--out', str(args.out)]
            if os.name == 'nt':
                # fcntl is the existing Linux-only process lock. This local parser
                # check uses an isolated fixture; Linux keeps the real CLI lock.
                lock = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *args: None)
                with patch.dict(sys.modules, {'fcntl': lock}), \
                        patch.object(sys, 'argv', ['mounting_bridge.py', *argv]), \
                        redirect_stdout(StringIO()):
                    bridge.main()
            else:
                result = subprocess.run([sys.executable, '-X', 'utf8', '-B',
                    str(bridge.ROOT / 'adapters/mounting_bridge.py'), *argv], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            report = bridge.read(args.out / 'SUMMARY.json')
            self.assertEqual(report['counts']['mounted'], 2)
            self.assertIs(report['mounting_review_enabled'], False)

    def test_review_action_off_skips_model_and_creates_no_review_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary))
            with patch.object(bridge, 'reviewer_module', side_effect=AssertionError('Reviewer forbidden')), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')):
                report = bridge.review(args)
            self.assertIs(report['model_review_performed'], False)
            self.assertFalse((args.out / 'REVIEWED.json').exists())

    def test_off_rejects_changed_finalized_route_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary))
            path = args.out / 'groups/mechanical_engineering/routed.jsonl'
            bridge.dump(args.out / 'route/ORCHESTRATION_STATUS.json', {'state': 'finalized',
                'routed_sha256': {'mechanical_engineering': bridge.sha(path)}})
            rows = bridge.rows(path)
            rows[0]['knowledge_labeling']['path_score'] = .96
            bridge.write_rows(path, rows)
            with self.assertRaises((ValueError, FileNotFoundError)) as failed:
                bridge.export(args)
            self.assertIn('route output changed', str(failed.exception))

    def test_off_mounts_real_candidates_with_original_payload_and_no_review_or_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, original = routed_fixture(Path(temporary))
            prepared_sha = bridge.sha(args.out / 'PREPARED.json')
            original_import = builtins.__import__
            def local_import(name, *args, **kwargs):
                if 'mount_pilot' in name or 'review_lib' in name:
                    raise AssertionError('Reviewer import forbidden in OFF export')
                return original_import(name, *args, **kwargs)
            with ExitStack() as stack:
                stack.enter_context(patch('builtins.__import__', side_effect=local_import))
                stack.enter_context(patch.object(bridge, 'reviewer_module',
                    side_effect=AssertionError('Reviewer forbidden in OFF export')))
                for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                               'socket.create_connection', 'socket.getaddrinfo', 'urllib.request.urlopen'):
                    stack.enter_context(patch(target, side_effect=AssertionError('Network forbidden')))
                report = self.export_off(argparse.Namespace(out=args.out, mount_review_mode='off'))
            self.assertEqual(report['counts'], {'mounted': 2, 'review': 0, 'not_mounted': 0,
                                              'technical_failure': 1, 'unconfigured': 0})
            kept = bridge.rows(args.out / 'mounted_standard.jsonl')
            self.assertEqual([row['id'] for row in kept], ['1', '2'])
            self.assertEqual(kept, [{**row, 'main_tags': '机械工程/传动/齿轮',
                                    'related_tags': ['机械工程/传动/传动设计']} for row in original[:2]])
            self.assertEqual(bridge.rows(args.out / 'groups/mechanical_engineering/mounted_standard.jsonl'), kept)
            self.assertEqual(bridge.rows(args.out / 'technical_failure.jsonl')[0]['record'], original[2])
            audit = bridge.rows(args.out / 'mount_audit.jsonl')
            self.assertEqual([row['original'] for row in audit], original)
            self.assertTrue(all(row['path_reviews'] == [] and row['review_request_ids'] == [] for row in audit))
            self.assertIs(report['count_conserved'], True)
            self.assertIs(report['mounting_review_enabled'], False)
            self.assertIs(report['model_review_performed'], False)
            self.assertIs(report['semantic_quality_approved'], False)
            self.assertFalse((args.out / 'REVIEWED.json').exists())
            self.assertFalse((args.out / 'review_responses.jsonl').exists())
            self.assertEqual(bridge.sha(args.out / 'PREPARED.json'), prepared_sha)

    def test_old_manifest_defaults_off_and_explicit_off_ignores_stale_votes(self):
        for prepared_value, explicit in ((None, None), (True, 'off')):
            with self.subTest(prepared_value=prepared_value, explicit=explicit), \
                    tempfile.TemporaryDirectory() as temporary:
                args, _ = routed_fixture(Path(temporary), mode='on')
                manifest = bridge.read(args.out / 'PREPARED.json')
                if prepared_value is None:
                    manifest.pop('mount_review_enabled', None)
                else:
                    manifest['mount_review_enabled'] = prepared_value
                bridge.dump(args.out / 'PREPARED.json', manifest)
                bridge.dump(args.out / 'REVIEWED.json', {'format_or_semantic_unresolved': 99})
                bridge.write_rows(args.out / 'review_responses.jsonl', [{'request_id': 'stale',
                    'final': {'judgment': 'unreasonable'}}])
                prepared_sha = bridge.sha(args.out / 'PREPARED.json')
                with patch.object(bridge, 'reviewer_module', side_effect=AssertionError('Reviewer forbidden')):
                    report = self.export_off(argparse.Namespace(out=args.out, mount_review_mode=explicit))
                self.assertEqual(report['counts']['mounted'], 2)
                self.assertEqual(bridge.sha(args.out / 'PREPARED.json'), prepared_sha)
                self.assertEqual(bridge.read(args.out / 'REVIEWED.json')['format_or_semantic_unresolved'], 99)

    def test_invalid_or_low_score_or_shallow_primary_stays_unresolved(self):
        for invalid in ('score', 'chain', 'depth', 'max_depth', 'nonfinite', 'boolean_score'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                args, _ = routed_fixture(Path(temporary))
                path = args.out / 'groups/mechanical_engineering/routed.jsonl'
                rows = bridge.rows(path)
                label = rows[0]['knowledge_labeling']
                if invalid == 'score':
                    label['path_score'] = .84
                elif invalid == 'nonfinite':
                    label['path_score'] = float('nan')
                elif invalid == 'boolean_score':
                    label['path_score'] = True
                elif invalid == 'chain':
                    label['best_path_codes'] = ['invented']
                elif invalid == 'max_depth':
                    manifest = bridge.read(args.out / 'PREPARED.json')
                    manifest.update(min_depth=1, max_depth=1)
                    bridge.dump(args.out / 'PREPARED.json', manifest)
                    # Keep the other accepted record at a valid level.
                    index = bridge.read(path.parent / 'node_index.json')
                    rows[1]['knowledge_labeling']['best_path_codes'] = next(node['chain_codes'][1:]
                        for node in index.values() if node['depth'] == 1)
                else:
                    index = bridge.read(path.parent / 'node_index.json')
                    label['best_path_codes'] = next(node['chain_codes'][1:]
                        for node in index.values() if node['depth'] == 1)
                bridge.write_rows(path, rows)
                report = self.export_off(args)
                self.assertEqual(report['counts']['mounted'], 1)
                self.assertEqual(report['counts']['review'], 1)
                self.assertEqual(bridge.rows(args.out / 'review.jsonl')[0]['record']['id'], '1')

    def test_route_dispositions_conserve_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = [{'id': str(number), 'name': '齿轮', 'definition': '齿轮用于传动。', 'tag': '机械工程'}
                      for number in range(5)]
            args, _ = routed_fixture(Path(temporary), records=source)
            path = args.out / 'groups/mechanical_engineering/routed.jsonl'
            rows = bridge.rows(path)
            for row, disposition in zip(rows, ('accepted_parent', 'needs_review',
                                               'insufficient', 'out_of_scope', 'failed')):
                row['knowledge_labeling']['status'] = 'failed' if disposition == 'failed' else 'ok'
                row['knowledge_labeling']['decision'] = disposition
                if disposition == 'accepted_parent':
                    accepted = rows[0]['knowledge_labeling']
                    row['knowledge_labeling'] = {**accepted, 'status': 'ok'}
            bridge.write_rows(path, rows)
            report = self.export_off(args)
            self.assertEqual(report['counts'], {'mounted': 1, 'review': 1, 'not_mounted': 2,
                                               'technical_failure': 1, 'unconfigured': 0})
            audit = bridge.rows(args.out / 'mount_audit.jsonl')
            self.assertEqual([row['original']['id'] for row in audit], ['0', '1', '2', '3', '4'])
            self.assertEqual([row['status'] for row in audit], ['mounted', 'review', 'not_mounted',
                                                             'not_mounted', 'technical_failure'])

    def test_related_candidates_keep_limit_truncation_duplicate_and_ancestor_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary))
            manifest = bridge.read(args.out / 'PREPARED.json')
            manifest.update(min_depth=1, max_related=1)
            bridge.dump(args.out / 'PREPARED.json', manifest)
            path = args.out / 'groups/mechanical_engineering/routed.jsonl'
            rows = bridge.rows(path)
            index = bridge.read(path.parent / 'node_index.json')
            ancestor = next(node['chain_codes'][1:] for node in index.values() if node['depth'] == 1)
            label = rows[0]['knowledge_labeling']
            related = label['top_paths'][0]['path_codes']
            label['top_paths'] = [
                {'path_codes': ['invented'], 'path_score': .99},
                {'path_codes': label['best_path_codes'], 'path_score': .99},
                {'path_codes': ancestor, 'path_score': .99},
                {'path_codes': related, 'path_score': .84},
                {'path_codes': related, 'path_score': .99, 'truncated': True},
                {'path_codes': related, 'path_score': .92, 'truncated': False},
                {'path_codes': related, 'path_score': .99, 'truncated': False}]
            bridge.write_rows(path, rows)
            self.export_off(args)
            self.assertEqual(bridge.rows(args.out / 'mounted_standard.jsonl')[0]['related_tags'],
                             ['机械工程/传动/传动设计'])
            manifest['max_related'] = 0
            bridge.dump(args.out / 'PREPARED.json', manifest)
            self.export_off(args)
            self.assertEqual(bridge.rows(args.out / 'mounted_standard.jsonl')[0]['related_tags'], [])

    def test_off_requires_real_route_receipt_and_exact_native_id_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary))
            (args.out / 'ROUTED.json').unlink()
            with self.assertRaises((ValueError, FileNotFoundError)) as failed:
                bridge.export(args)
            self.assertIn('ROUTED', str(failed.exception))
            bridge.dump(args.out / 'ROUTED.json', {'groups': 1, 'records': 3})
            path = args.out / 'groups/mechanical_engineering/routed.jsonl'
            bridge.write_rows(path, bridge.rows(path)[:-1])
            with self.assertRaisesRegex(ValueError, 'coverage'):
                bridge.export(args)

    def test_enabled_export_keeps_recomputed_uncertainty_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary), mode='on')
            reviewer = bridge.reviewer_module()
            with patch.object(reviewer, 'review_one', side_effect=fake_review):
                bridge.review(args)
            args.mount_review_mode = None
            report = bridge.export(args)
            self.assertEqual(report['counts'], {'mounted': 1, 'review': 1, 'not_mounted': 0,
                                               'technical_failure': 1, 'unconfigured': 0})
            self.assertIs(report.get('mounting_review_enabled'), True)
            self.assertIs(report.get('model_review_performed'), True)
            self.assertIs(report.get('semantic_quality_approved'), False)

    def test_invalid_prepared_boolean_or_output_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = routed_fixture(Path(temporary))
            manifest = bridge.read(args.out / 'PREPARED.json')
            for value in ('false', 0, None):
                manifest['mount_review_enabled'] = value
                bridge.dump(args.out / 'PREPARED.json', manifest)
                with self.assertRaisesRegex(ValueError, 'must be a boolean'):
                    bridge.export(args)
            manifest['mount_review_enabled'] = False
            bridge.dump(args.out / 'PREPARED.json', manifest)
            args.mount_review_mode = 'false'
            with self.assertRaisesRegex(ValueError, 'must be off or on'):
                bridge.export(args)

    def test_off_empty_input_and_missing_tag_follow_existing_content_policy(self):
        for records in ([], [{'id': 7, 'name': '齿轮', 'definition': '原文。',
                              'source': {'book': '原书', 'anchor': [1, 2]}}]):
            with self.subTest(records=records), tempfile.TemporaryDirectory() as temporary:
                args, original = fixture(Path(temporary), records, mount_review_mode='off')
                if records:
                    folder = args.out / 'groups/mechanical_engineering'
                    item = bridge.rows(folder / 'input.jsonl')[0]
                    index = bridge.read(folder / 'node_index.json')
                    primary = next(node for node in index.values() if node['depth'] == 2)
                    bridge.write_rows(folder / 'routed.jsonl', [{**item, 'knowledge_labeling': {
                        'status': 'ok', 'decision': 'accepted_leaf',
                        'best_path_codes': primary['chain_codes'][1:], 'path_score': .95}}])
                bridge.dump(args.out / 'ROUTED.json', {'groups': 1 if records else 0,
                                                      'records': len(records)})
                report = self.export_off(args)
                self.assertEqual(report['counts']['mounted'], len(records))
                expected = [{**row, 'tag': '机械工程', 'main_tags': '机械工程/传动/齿轮',
                             'related_tags': []} for row in original]
                self.assertEqual(bridge.rows(args.out / 'mounted_standard.jsonl'), expected)
                self.assertEqual(sum(report['counts'].values()), len(original))

    def test_off_multi_subject_same_ids_and_unconfigured_payload_are_conserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, _ = fixture(root, mount_review_mode='off')
            records = [
                {'id': 'same', 'name': '齿轮', 'tag': '机械工程', 'source': {'book': 'mechanical'}},
                {'id': 'same', 'name': '波动', 'tag': '物理学', 'source': {'book': 'physics'}},
                {'id': 'unknown', 'name': '未配置记录', 'tag': '未配置', 'source': {'book': 'unknown'}}]
            bridge.write_rows(args.input, records)
            bridge.dump(root / 'physics.json', {'name': '物理学', 'children': [
                {'name': '力学', 'children': [{'name': '波动'}]}]})
            bridge.dump(root / 'registry.json', {
                'mechanical_engineering': {'name': '机械工程', 'file': 'tree.json'},
                'physics': {'name': '物理学', 'file': 'physics.json'},
                'unconfigured': {'name': '未配置', 'file': None}})
            args.registry = root / 'registry.json'
            args.taxonomy = None
            args.subject_slug = None
            args.out = root / 'multi_subject'
            bridge.prepare(args)
            for slug in ('mechanical_engineering', 'physics'):
                folder = args.out / 'groups' / slug
                item = bridge.rows(folder / 'input.jsonl')[0]
                index = bridge.read(folder / 'node_index.json')
                node = next(value for value in index.values() if value['depth'] == 2)
                bridge.write_rows(folder / 'routed.jsonl', [{**item, 'knowledge_labeling': {
                    'status': 'ok', 'decision': 'accepted_leaf',
                    'best_path_codes': node['chain_codes'][1:], 'path_score': .95}}])
            bridge.dump(args.out / 'ROUTED.json', {'groups': 2, 'records': 2})
            report = self.export_off(args)
            self.assertEqual(report['counts'], {'mounted': 2, 'review': 0, 'not_mounted': 0,
                                               'technical_failure': 0, 'unconfigured': 1})
            self.assertEqual(bridge.rows(args.out / 'unconfigured.jsonl')[0]['record'], records[2])
            mounted = bridge.rows(args.out / 'mounted_standard.jsonl')
            self.assertEqual([(row['tag'], row['id'], row['source']) for row in mounted],
                             [(row['tag'], row['id'], row['source']) for row in records[:2]])
            self.assertEqual([row['original'] for row in bridge.rows(args.out / 'mount_audit.jsonl')], records[:2])
            for slug, expected_path in (('mechanical_engineering', '机械工程/传动/齿轮'),
                                        ('physics', '物理学/力学/波动')):
                group = bridge.rows(args.out / 'groups' / slug / 'mounted_standard.jsonl')
                self.assertEqual(len(group), 1)
                self.assertEqual(group[0]['main_tags'], expected_path)

    def test_off_keeps_semantic_boundary_degradation_and_empty_candidate_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper = boundary_tests.MountingBoundaryTests()
            args, _ = helper.prepare(Path(temporary), policy='empty')
            args.mount_review_mode = 'off'
            with patch.object(bridge.subprocess, 'run', side_effect=lambda *args_, **kwargs:
                              boundary_tests.generated_fixture(args, fallback=True)):
                bridge.generate_boundaries(args)
            bridge.verify_boundaries(args)
            fake_routes(args)
            bridge.dump(args.out / 'ROUTED.json', {'groups': 1, 'records': 3})
            with patch.object(bridge, 'reviewer_module', side_effect=AssertionError('Reviewer forbidden')):
                report = self.export_off(args)
            boundaries = report['semantic_boundaries']
            self.assertIs(boundaries['quality_degraded'], True)
            self.assertEqual(boundaries['empty_boundary_nodes'], 2)
            self.assertEqual(boundaries['mounted_with_empty_boundary_in_candidates'], 2)
            self.assertIs(boundaries['empty_boundary_is_semantic_approval'], False)
            self.assertIs(report['semantic_quality_approved'], False)


class MountReviewStagedTests(unittest.TestCase):
    def test_off_dispositions_ignore_stale_review_error_and_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            mount = home / 'run/05_mounting'
            bridge.dump(mount / 'SUMMARY.json', {'mounting_review_enabled': False,
                'counts': {'mounted': 2, 'review': 1, 'technical_failure': 1}})
            bridge.dump(mount / 'review/ORCHESTRATION_STATUS.json', {'pending_transport': 99})
            bridge.dump(mount / 'REVIEWED.json', {'format_or_semantic_unresolved': 88})
            bridge.dump(mount / 'route/ORCHESTRATION_STATUS.json', {'transport_unresolved': 1})
            transport, counts, unresolved = bench.mounting_dispositions([{'home': str(home)}])
            self.assertEqual(transport, 1)
            self.assertEqual(counts['review'], 1)
            self.assertEqual(counts['technical_failure'], 1)
            self.assertEqual(unresolved, 0)

    def test_run_track_exports_disabled_jobs_without_review_transport(self):
        # Execute the real mounting block of run_track, with model transport replaced
        # only at its existing orchestration boundary and actual per-job configs.
        tree = ast.parse(inspect.getsource(bench.run_track))
        body = tree.body[0].body
        start = next(index for index, node in enumerate(body) if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Tuple)
            and any(isinstance(target, ast.Name) and target.id == 'routed' for target in node.targets[0].elts))
        stop = next(index for index, node in enumerate(body[start:], start) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'dedup' for target in node.targets))
        tail = ast.FunctionDef(name='mount_finish', args=ast.arguments(posonlyargs=[], args=[],
            kwonlyargs=[], kw_defaults=[], defaults=[]), body=[*body[start:stop], ast.Return(value=ast.Name(
                id='ready_mounts', ctx=ast.Load()))], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[tail], type_ignores=[]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = []
            for mode in (False, True):
                config = root / ('on.json' if mode else 'off.json')
                bridge.dump(config, {'mounting': {'review_enabled': mode}})
                jobs.append({'job_id': 'on' if mode else 'off', 'config': str(config)})
            reviewed_ids = []
            def transport(root, track, gateway, profile, recorder, values, task):
                if task == 'review_mounting':
                    reviewed_ids.extend(job['job_id'] for job in values)
                return values, {}
            values = dict(vars(bench))
            values.update(root=root, track='important', gateway=None, profile=ResourceProfile(),
                          recorder=None, mount_ready=jobs, run_mount_transport=transport,
                          selected_job=lambda *args: {'status': 'completed'})
            exec(compile(module, '<production-mounting-block>', 'exec'), values)
            ready = values['mount_finish']()
            self.assertEqual(reviewed_ids, ['on'])
            self.assertEqual({job['job_id'] for job in ready}, {'off', 'on'})


if __name__ == '__main__':
    unittest.main()
