"""The successor must not touch a model before a verified recovery boundary."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import recovery_successor as successor
except ImportError:
    successor = None


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def finished_fixture(root):
    source = root / 'source'
    books = ['a', 'b']
    write(source / 'JOB.json', {'api': 'http://example.invalid', 'model': 'native',
                               'books': [{'spec': {'identifier': ref}} for ref in books]})
    write(source / 'LAUNCH.json', {'pid': 12345, 'start_ticks': 56789, 'session_id': 12345})
    (source / 'recovery_rounds.py').write_text('frozen source', encoding='utf-8')
    write(source / 'STATUS.json', {'state': 'finished_with_unresolved', 'rounds_completed': 3,
          'unresolved_items': 2, 'owned_processes': [{'pid': 12346, 'start_ticks': 56790}]})
    idle = {'upstream': 'http://example.invalid', 'total': {'active': 0, 'queued': 0}}
    write(source / 'gateway/STATS.json', idle)
    for number, cap in enumerate((512, 128, 32), 1):
        write(source / f'round_{number}/RESULT.json', {'round': number, 'global_limit': cap,
              'finished_books': 2, 'gateway_snapshot': idle, 'exit_code': 2})
    write(source / 'round_3/IDLE_CHECK.json', {'zero_samples': 3, 'gateway': idle})
    write(source / 'round_3/native/SUMMARY.json', {'finished_books': 2, 'books': [
          {'identifier': 'a', 'status': 'completed', 'unresolved_items': 0},
          {'identifier': 'b', 'status': 'partial', 'unresolved_items': 2}]})
    write(source / 'merged/RECONCILIATION.json', {'books': [{'identifier': ref} for ref in books],
          'newly_accepted': 1, 'semantic_quality_approved': False})
    write(source / 'PRODUCTION_AFTER_RUN.json', {'production_modified': False,
          'frozen_code': [{'file': 'code/native.py', 'matches': True}]})
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in ('JOB.json', 'LAUNCH.json', 'recovery_rounds.py')}
    return {'dependency_root': str(source), 'dependency_hashes': hashes,
            'dependency_identity': {'pid': 12345, 'start_ticks': 56789, 'session': 12345},
            'dependency_books': books, 'api_url': 'http://example.invalid',
            'model': 'native', 'round_limits': [512, 128, 32]}


class RecoverySuccessorGateTests(unittest.TestCase):
    def test_waiting_writes_zero_calls_and_never_starts_a_gateway(self):
        self.assertIsNotNone(successor)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = finished_fixture(home)
            write(Path(config['dependency_root']) / 'STATUS.json', {'state': 'running', 'round': 2})
            class WaitObserved(Exception):
                pass
            def inspect(value):
                return successor.dependency_evidence(value, process_reader=lambda _: None,
                                                     process_inventory=lambda: [])
            def sleep(_):
                raise WaitObserved()
            with patch.object(successor, 'Gateway', side_effect=AssertionError('gateway during dependency')):
                with self.assertRaises(WaitObserved):
                    successor.wait_for_dependency(home, config, inspect=inspect, sleeper=sleep)
            observed = json.loads((home / 'STATUS.json').read_text())
            self.assertEqual(observed['state'], 'WAITING_DEPENDENCY')
            self.assertEqual(observed['model_calls'], 0)
            self.assertFalse((home / 'calibration').exists())

    def inspect(self, config, processes=None, inventory=None):
        self.assertIsNotNone(successor, 'recovery boundary guard is missing')
        return successor.dependency_evidence(config,
               process_reader=lambda pid: (processes or {}).get(pid),
               process_inventory=lambda: inventory or [])

    def test_terminal_partial_is_ready_only_after_all_receipts_and_processes_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            result = self.inspect(config)
            self.assertTrue(result['ready'])
            self.assertEqual(result['dependency_state'], 'finished_with_unresolved')
            self.assertFalse(result['semantic_quality_approved'])
            self.assertEqual(result['model_calls'], 0)

    def test_a_terminal_status_does_not_retire_a_live_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            live = {12345: {'pid': 12345, 'start_ticks': 56789, 'state': 'S', 'session': 12345}}
            result = self.inspect(config, live)
            self.assertFalse(result['ready'])
            self.assertEqual(result['state'], 'WAITING_DEPENDENCY')

    def test_pid_disappearance_does_not_replace_round_three_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            source = Path(config['dependency_root'])
            (source / 'round_3/RESULT.json').unlink()
            (source / 'merged/RECONCILIATION.json').unlink()
            self.assertFalse(self.inspect(config)['ready'])

    def test_owned_subprocess_must_exit_even_after_supervisor_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            live = {12346: {'pid': 12346, 'start_ticks': 56790, 'state': 'R', 'session': 12345}}
            self.assertFalse(self.inspect(config, live)['ready'])

    def test_unlisted_process_in_owned_session_also_holds_the_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            live = [{'pid': 12347, 'start_ticks': 56791, 'state': 'S', 'session': 12345}]
            self.assertFalse(self.inspect(config, inventory=live)['ready'])

    def test_gateway_must_be_drained_in_both_live_file_and_final_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            source = Path(config['dependency_root'])
            write(source / 'gateway/STATS.json', {'upstream': config['api_url'],
                  'total': {'active': 1, 'queued': 0}})
            self.assertFalse(self.inspect(config)['ready'])
            write(source / 'gateway/STATS.json', {'upstream': config['api_url'],
                  'total': {'active': 0, 'queued': 0}})
            write(source / 'round_3/IDLE_CHECK.json', {'zero_samples': 2,
                  'gateway': {'total': {'active': 0, 'queued': 0}}})
            self.assertFalse(self.inspect(config)['ready'])

    def test_incomplete_native_cohort_never_crosses_the_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            write(Path(config['dependency_root']) / 'round_3/native/SUMMARY.json',
                  {'finished_books': 1, 'books': [{'identifier': 'a', 'status': 'partial'}]})
            with self.assertRaisesRegex(ValueError, 'cohort'):
                self.inspect(config)

    def test_frozen_dependency_identity_drift_is_a_hard_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            source = Path(config['dependency_root'])
            (source / 'recovery_rounds.py').write_text('changed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.inspect(config)

    def test_failed_dependency_without_three_rounds_is_held_without_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = finished_fixture(Path(tmp))
            source = Path(config['dependency_root'])
            write(source / 'STATUS.json', {'state': 'failed', 'error': 'native failed'})
            result = self.inspect(config)
            self.assertEqual(result['state'], 'HELD_DEPENDENCY_FAILED')
            self.assertFalse(result['ready'])
            self.assertEqual(result['model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
