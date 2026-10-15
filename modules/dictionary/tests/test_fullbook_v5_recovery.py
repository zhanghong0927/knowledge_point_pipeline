"""A fresh recovery run reuses source-bound work without touching the old run."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import fullbook_llm_v5 as v5
import run_fullbook_v5 as driver
try:
    import recover_fullbook_v5 as recovery_cli
except ModuleNotFoundError as exc:
    if exc.name != 'recover_fullbook_v5':raise
    recovery_cli = None


def item(unit, name, broken=False):
    return {'head': [{'unit': unit, 'quote': name}],
            'knowledge_point': [{'unit': unit, 'quote': 'Absent' if broken else name}],
            'name': [], 'body': [], 'body_complete': True}


def response(payload):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(payload)}}]}


def hashes(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file()}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        md = self.root / 'book.md'
        md.write_text('# Alpha\n# Beta\n', encoding='utf-8')
        self.spec = {'identifier': 'recovery-book', 'title': 'Test', 'md_path': str(md)}
        self.calls = []
        def original_api(*call):
            packet = json.loads(call[-1]['messages'][1]['content'])
            if 'repair_tasks' not in packet:
                return response({'scanned_all': True, 'entries': [item(0, 'Alpha'), item(1, 'Beta', True)]})
            return response({'repairs': [{'repair_id': task['repair_id'], 'entry': task['entry'],
                                         'reason': 'still invalid'} for task in packet['repair_tasks']]})
        self.execute(self.root / 'original', original_api)
        self.source = next((self.root / 'original').glob('*/INPUT.json')).parent

    def execute(self, out, api, spec=None, round_number=1):
        args = argparse.Namespace(api_url='http://new-unused', model='test', tokenizer=None,
            context=65536, output_tokens=8192, overlap=4, workers=2, timeout=3,
            out=out, attempts=3, transport_round=round_number)
        runner = v5.Runner(args)
        class Tokenizer:
            def apply_chat_template(self, _messages, **_kwargs):
                return [0] * 100
        runner.tokenizer = Tokenizer()
        runner.api = api
        runner.backoff = lambda _attempt: None
        try:
            return runner.run_book(spec or self.spec)
        finally:
            runner.close()

    def recovering_api(self, *call):
        packet = json.loads(call[-1]['messages'][1]['content'])
        self.calls.append(packet)
        if getattr(self,'defer_first',False) and len(self.calls)==1:
            raise HTTPError('http://new-unused',504,'timeout',{},None)
        if 'repair_tasks' not in packet:
            return response({'scanned_all': True, 'entries': [item(0, 'Alpha'), item(1, 'Beta')]})
        return response({'repairs': [{'repair_id': task['repair_id'], 'entry': item(1, 'Beta'),
                                     'reason': 'literal name restored'} for task in packet['repair_tasks']]})

    def recover(self, out=None, round_number=1):
        return self.execute(out or self.root / 'recovery', self.recovering_api,
                            {**self.spec, 'recovery_source': str(self.source)},round_number=round_number)

    def test_only_unresolved_candidate_is_requested_and_valid_neighbor_is_reused(self):
        before = hashes(self.source)
        result = self.recover()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['entries'], 2)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('repair_tasks', self.calls[0], 'Recovery repeated the entire discovery')
        self.assertEqual(len(self.calls[0]['repair_tasks']), 1)
        self.assertEqual(self.calls[0]['repair_tasks'][0]['entry']['head'][0]['quote'], 'Beta')
        self.assertEqual(hashes(self.source), before)
        target_input = next((self.root / 'recovery').glob('*/INPUT.json'))
        origin = json.loads(target_input.read_text()).get('recovery_source', {})
        self.assertEqual(origin.get('path'), str(self.source.resolve()))

    def test_aborted_book_without_book_summary_can_recover_cached_work(self):
        (self.source / 'SUMMARY.json').unlink()
        before = hashes(self.source)
        result = self.recover()
        self.assertEqual(result['entries'], 2)
        self.assertIn('repair_tasks', self.calls[0])
        self.assertEqual(hashes(self.source), before)

    def test_duplicate_cached_repair_ids_are_not_promoted_without_a_new_repair(self):
        path = self.source / 'responses' / '00000000_00000002_2.json'
        path.write_text(json.dumps(response({'repairs': [
            {'repair_id': 'e00001', 'entry': item(1, 'Beta'), 'reason': 'first'},
            {'repair_id': 'e00001', 'entry': item(1, 'Beta', True), 'reason': 'duplicate'}]})),
            encoding='utf-8')
        result = self.recover()
        self.assertEqual(result['entries'], 2)
        self.assertEqual(len(self.calls), 1, 'Duplicate cached IDs bypassed the repair schema')
        self.assertIn('repair_tasks', self.calls[0])

    def test_changed_md_is_rejected_before_any_model_call(self):
        Path(self.spec['md_path']).write_text('# Foreign\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source|Source|MD'):
            self.recover()
        self.assertEqual(self.calls, [])

    def test_corrupt_saved_accepted_entry_is_rejected_before_any_model_call(self):
        path = next((self.source / 'chunks').glob('*.json'))
        data = json.loads(path.read_text())
        data['accepted_raw'][0]['head'][0]['quote'] = 'Foreign'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source|Source|Saved|saved'):
            self.recover()
        self.assertEqual(self.calls, [])

    def test_output_cannot_overlap_the_original_source(self):
        before = hashes(self.source)
        with self.assertRaisesRegex(ValueError, 'overlap|Overlap|separate|Separate'):
            self.recover(self.source / 'nested-output')
        self.assertEqual(hashes(self.source), before)

    def test_recovery_504_round_replays_the_frozen_seed_and_preserves_valid_neighbors(self):
        self.defer_first=True
        before=hashes(self.source)
        first=self.recover()
        self.assertEqual(first['entries'],1)
        self.assertEqual(first['http504_pending_chunks'],1)
        try:
            second=self.recover(round_number=2)
        except ValueError as exc:
            self.fail(f'The recovery global retry lost its initial queue: {exc}')
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['entries'],2)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0],self.calls[1])
        self.assertEqual(hashes(self.source),before)

    def test_missing_accepted_entries_field_is_refused_instead_of_losing_a_neighbor(self):
        path=next((self.source/'chunks').glob('*.json'))
        data=json.loads(path.read_text())
        del data['accepted_raw']
        path.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'schema|Schema|accepted'):
            self.recover()
        self.assertEqual(self.calls,[])

    def cli_args(self, output=None, context='65536'):
        return ['--source-run', str(self.source), '--out', str(output or self.root / 'cli-recovery'),
                '--api-url', 'http://new-unused', '--model', 'test', '--context', context]

    def test_cli_prepare_only_freezes_a_source_bound_manifest_without_model_calls(self):
        self.assertIsNotNone(recovery_cli, 'The independent recovery CLI is missing')
        before = hashes(self.source)
        self.assertEqual(recovery_cli.main(self.cli_args()), 0)
        output = self.root / 'cli-recovery'
        specs = json.loads((output / 'MANIFEST.json').read_text())
        self.assertEqual(specs[0]['recovery_source'], str(self.source.resolve()))
        self.assertEqual(specs[0]['identifier'], 'recovery-book')
        self.assertFalse((output / 'native').exists())
        self.assertEqual(hashes(self.source), before)
        self.assertEqual(self.calls, [])

    def test_cli_rejects_an_old_output_directory_before_writing(self):
        self.assertIsNotNone(recovery_cli, 'The independent recovery CLI is missing')
        before = hashes(self.source)
        self.assertEqual(recovery_cli.main(self.cli_args(self.source)), 2)
        self.assertEqual(hashes(self.source), before)

    def test_cli_prepared_profile_cannot_be_changed_in_place(self):
        self.assertIsNotNone(recovery_cli, 'The independent recovery CLI is missing')
        self.assertEqual(recovery_cli.main(self.cli_args()), 0)
        before = hashes(self.root / 'cli-recovery')
        self.assertEqual(recovery_cli.main(self.cli_args(context='32768')), 2)
        self.assertEqual(hashes(self.root / 'cli-recovery'), before)

    def test_cli_run_refuses_source_change_between_prepare_and_native_start(self):
        self.assertIsNotNone(recovery_cli, 'The independent recovery CLI is missing')
        def delayed_start(_native_args):
            path=next((self.source/'chunks').glob('*.json'))
            data=json.loads(path.read_text())
            data['accepted_raw'][0]['body_complete']=False
            path.write_text(json.dumps(data),encoding='utf-8')
            spec=json.loads((self.root/'cli-recovery'/'MANIFEST.json').read_text())[0]
            return self.execute(self.root/'cli-recovery'/'native',self.recovering_api,spec)
        with patch.object(recovery_cli.driver,'main',side_effect=delayed_start):
            with self.assertRaisesRegex(ValueError,'fingerprint|Fingerprint|prepared|Prepared'):
                recovery_cli.main(self.cli_args()+['--run'])
        self.assertEqual(self.calls,[])


class DriverRepairSettingsTests(unittest.TestCase):
    def test_explicit_repair_policy_reaches_the_native_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            manifest.write_text('[{"identifier":"one"}]', encoding='utf-8')
            try:
                args = driver.parse_args(['--manifest', str(manifest), '--out', str(root / 'out'),
                    '--api-url', 'http://unused', '--model', 'test', '--context', '65536',
                    '--output-tokens', '8192', '--repair-batch-size', '2', '--repair-attempts', '3',
                    '--repair-max-requests', '20', '--tokenize-attempts', '4'])
            except SystemExit:
                self.fail('The orchestration CLI cannot pass the configured repair policy')
            command = driver.Orchestrator(args).command(1, 8)
            for flag, want in [('--repair-batch-size', '2'), ('--repair-attempts', '3'),
                               ('--repair-max-requests', '20'), ('--tokenize-attempts', '4')]:
                self.assertIn(flag, command)
                self.assertEqual(command[command.index(flag) + 1], want)


if __name__ == '__main__':
    unittest.main()
