import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch


class PostDictionaryRunnerTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('post_dictionary_runner'),
                             'a concrete automatic dictionary-calibration-important chain is required')
        import post_dictionary_runner
        return post_dictionary_runner

    def fixture(self, root):
        source = root / 'old'
        (source / 'code').mkdir(parents=True)
        (source / 'code/staged_benchmark.py').write_text('# frozen old code')
        (source / 'inputs').mkdir()
        (source / 'inputs/important_candidates_verified.json').write_text('[]')
        (source / 'SERVICE_PREFLIGHT.json').write_text(json.dumps({'model': 'fake',
            'api_url': 'http://127.0.0.1:12345', 'configured_context': 32768}))
        identity = {'pid': 9001, 'pgrp': 9001, 'session': 9001, 'uid': 1,
                    'start_ticks': 100, 'state': 'S', 'argv': ['python',
                    str(source / 'code/staged_benchmark.py'), 'run', '--root', str(source)]}
        import dictionary_handoff
        return {'source_root': str(source), 'source_identity': identity,
                'source_protected_hashes': dictionary_handoff.protected_hashes(source),
                'important_root': str(root / 'important'), 'gpu_cards': 16,
                'target_per_track': 30, 'seed': 20261012}

    def test_absent_dictionary_summary_does_not_signal_or_call_model(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            config = self.fixture(Path(temp))
            with patch.object(module, 'process_identity', return_value=config['source_identity']), \
                    patch.object(module, 'retire_launch') as retire, \
                    patch.object(module, 'calibrate_and_continue') as calibrate:
                result = module.inspect_boundary(config)
            self.assertEqual(result['state'], 'waiting_dictionary_whole_track')
            self.assertEqual(result['calibration_requests'], 0)
            retire.assert_not_called()
            calibrate.assert_not_called()

    def test_source_hash_or_process_reuse_blocks_boundary(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            config = self.fixture(Path(temp))
            wrong = dict(config['source_identity'], start_ticks=101)
            with patch.object(module, 'process_identity', return_value=wrong):
                with self.assertRaises(ValueError):
                    module.inspect_boundary(config)
            path = Path(config['source_root']) / 'code/staged_benchmark.py'
            path.write_text('# changed source')
            with patch.object(module, 'process_identity', return_value=config['source_identity']):
                with self.assertRaises(ValueError):
                    module.inspect_boundary(config)

    def test_important_command_uses_measured_limits_and_rule_track_only(self):
        module = self.module()
        report = {'front': {'selected_limit': 32}, 'extraction': {'selected_limit': 96},
                  'model_limit': 96}
        command = module.important_command(Path('/new/important'), report, gpu_cards=16,
                                           target_per_track=30, seed=20261012)
        self.assertEqual(command[command.index('--tracks') + 1], 'important')
        self.assertNotIn('--dictionary', command)
        for option, expected in (('--front-limit', '32'), ('--extraction-limit', '96'),
                                 ('--model-limit', '96'), ('--target-per-track', '30')):
            self.assertEqual(command[command.index(option) + 1], expected)
        self.assertIn('--calibration-report', command)

    def test_important_root_cannot_reuse_source_or_previous_attempt(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'old'
            source.mkdir()
            for destination in (source, source / 'important', Path(temp)):
                with self.assertRaises(ValueError):
                    module.validate_roots(source, destination)
            fresh = Path(temp) / 'new'
            module.validate_roots(source, fresh)
            fresh.mkdir()
            with self.assertRaises(FileExistsError):
                module.validate_roots(source, fresh)
            guardian = Path(temp) / 'guardian'
            with self.assertRaises(ValueError):
                module.validate_roots(source, guardian / 'code/important', guardian)
            with self.assertRaises(ValueError):
                module.validate_roots(source, Path(temp) / 'other', source / 'guardian')

    def test_native_finished_summary_is_required_to_launch_calibration(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            config = self.fixture(Path(temp))
            source = Path(config['source_root'])
            (source / 'dictionary').mkdir()
            (source / 'dictionary/SUMMARY.json').write_text(json.dumps({'track': 'dictionary',
                'status': 'completed_native_stages', 'selected_books': 30,
                'completed_cleaning_books': 30, 'dedup_completed': 1,
                'unresolved_stages': [], 'semantic_quality_approved': False}))
            with patch.object(module, 'process_identity', return_value=config['source_identity']):
                self.assertEqual(module.inspect_boundary(config)['state'], 'dictionary_boundary_ready')

    def test_failed_calibration_cannot_prepare_or_launch_important(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.fixture(root)
            home = root / 'guardian'
            home.mkdir()
            packets = [{'id': f'p{n}', 'native_front_track': 'important', 'payload': {}}
                       for n in range(4)]
            dictionary = [dict(packet, native_front_track='dictionary') for packet in packets]
            with patch.object(module, 'important_front_packets', return_value=packets), \
                    patch.object(module, 'dictionary_front_packets', return_value=dictionary), \
                    patch.object(module, 'dictionary_extraction_packets', return_value=packets), \
                    patch.object(module, 'Gateway') as gateway, \
                    patch.object(module, 'service_preflight', return_value={'model': 'fake'}), \
                    patch.object(module, 'context_checked_packets', side_effect=[packets + dictionary, packets]), \
                    patch.object(module, 'calibrate_stage', side_effect=RuntimeError('No healthy level')), \
                    patch.object(module, 'prepare_important_root') as prepare:
                gateway.return_value.__enter__.return_value.api_url = 'http://127.0.0.1:1'
                with self.assertRaisesRegex(RuntimeError, 'No healthy level'):
                    module.calibrate_and_continue(config, home, {'dictionary': {'summary': {}}})
                prepare.assert_not_called()
            self.assertFalse(Path(config['important_root']).exists())

    def test_frozen_important_snapshot_and_real_cli_accept_measured_caps(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.fixture(root)
            home = root / 'guardian'
            home.mkdir()
            service = json.loads((Path(config['source_root']) / 'SERVICE_PREFLIGHT.json').read_text())
            def result(limit):
                return {'selected_limit': limit, 'levels': [{'concurrency': limit,
                    'requests': 64, 'protocol_valid': 64, 'transport_errors': 0,
                    'wall_seconds': 100, 'p95_seconds': 90, 'population_sha256': 'a' * 64}]}
            report = {'schema': 1, 'status': 'completed', 'front': result(32),
                      'extraction': result(96), 'model_limit': 96, 'service': service,
                      'semantic_quality_approved': False}
            before = (Path(config['source_root']) / 'code/staged_benchmark.py').read_bytes()
            destination = module.prepare_important_root(config, home, service, report)
            snapshot = json.loads((destination / 'CODE_SNAPSHOT.json').read_text())
            self.assertEqual(snapshot['root'], str((destination / 'code').resolve()))
            command = module.important_command(destination, report, gpu_cards=16,
                target_per_track=30, seed=20261012)
            command[command.index('run')] = 'plan'
            document = json.loads(subprocess.check_output(command, cwd=destination / 'code'))
            self.assertEqual([track['name'] for track in document['tracks']], ['important'])
            self.assertTrue(document['resource_profile']['performance_calibrated'])
            self.assertEqual(document['resource_profile']['phase_round_limits']['front'], [32, 8, 2])
            self.assertEqual(document['resource_profile']['phase_round_limits']['cleaning'], [96, 24, 6])
            self.assertEqual(before, (Path(config['source_root']) / 'code/staged_benchmark.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
