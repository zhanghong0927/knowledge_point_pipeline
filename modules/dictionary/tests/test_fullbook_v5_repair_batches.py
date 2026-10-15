"""Repair coverage, output splitting and conservative source identity regressions."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

import fullbook_llm_v5 as v5
import fullbook_v5_anchors as anchors


def selection(unit, quote):
    return {'unit': unit, 'quote': quote}


def candidate(unit, head, name=None):
    return {'head': [selection(unit, head)],
            'knowledge_point': [selection(unit, name or head)], 'name': [],
            'body': [], 'body_complete': True}


def reply(payload, finish='stop'):
    return {'choices': [{'finish_reason': finish,
                         'message': {'content': json.dumps(payload)}}]}


class RepairBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = ['Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon']
        md = self.root / 'book.md'
        md.write_text(''.join('# ' + name + '\n' for name in self.names), encoding='utf-8')
        self.spec = {'identifier': 'batch-book', 'title': 'Test', 'md_path': str(md)}
        self.requests = []
        self.payloads = []

    def run_book(self, count=3, input_limit=None, output_limit=None, **settings):
        args = argparse.Namespace(api_url='http://unused', model='test', tokenizer=None,
            context=32768, output_tokens=8192, overlap=4, workers=2,
            book_workers=1, timeout=3, out=self.root / 'out', attempts=3,
            transport_round=1, repair_batch_size=8, repair_attempts=2,
            repair_max_requests=128, tokenize_attempts=3)
        for name, value in settings.items():
            setattr(args, name, value)
        runner = v5.Runner(args)
        class Tokenizer:
            def apply_chat_template(_self, messages, **_kwargs):
                packet = json.loads(messages[1]['content'])
                tasks = packet.get('repair_tasks', [])
                size = 100
                if tasks and input_limit is not None:
                    size = 10000 + 10000 * len(tasks) if input_limit == 1 else 30000
                return [0] * size
        runner.tokenizer = Tokenizer()
        runner.backoff = lambda _attempt: None
        def api(*call):
            payload = call[-1]
            self.payloads.append(copy.deepcopy(payload))
            packet = json.loads(payload['messages'][1]['content'])
            self.requests.append(copy.deepcopy(packet))
            if getattr(args,'mixed_transport',False):
                if len(self.requests)==2:raise TimeoutError('temporary model failure')
                if len(self.requests)==5:raise HTTPError('http://unused',504,'timeout',{},None)
            tasks = packet.get('repair_tasks')
            if tasks is None:
                return reply({'scanned_all': True, 'entries': [
                    candidate(unit, self.names[unit], 'Absent') for unit in range(count)]})
            if output_limit is not None and len(tasks) > output_limit:
                return reply({'repairs': []}, finish='length')
            fixes = []
            for task in tasks:
                unit = task['entry']['head'][0]['unit']
                fixed = (task['entry'] if getattr(args, 'unchanged', False)
                         else candidate(unit, self.names[unit]))
                fixes.append({'repair_id': task['repair_id'], 'entry': fixed, 'reason': 'source fix'})
            return reply({'repairs': fixes})
        runner.api = api
        try:
            self.result = runner.run_book(self.spec)
        finally:
            runner.close()
        caches = list((self.root / 'out').glob('*/chunks/*.json'))
        self.pending = [item for path in caches
                        for item in json.loads(path.read_text()) .get('unresolved', [])]
        return self.result

    def test_each_pending_item_gets_a_repair_when_only_one_fits_per_request(self):
        result = self.run_book(count=3, input_limit=1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['entries'], 3)
        self.assertEqual(result['unresolved_items'], 0)

    def test_configured_small_batch_covers_all_five_items(self):
        result = self.run_book(count=5, repair_batch_size=2)
        self.assertEqual(result['entries'], 5)
        batches = [packet['repair_tasks'] for packet in self.requests if 'repair_tasks' in packet]
        self.assertEqual(sorted(len(batch) for batch in batches), [1, 2, 2])

    def test_length_response_splits_repair_batch_and_retains_every_candidate(self):
        result = self.run_book(count=3, output_limit=1, repair_batch_size=4)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['entries'], 3)
        self.assertEqual(result['unresolved_items'], 0)
        entries_file = next((self.root / 'out').glob('*/entries.json'))
        entries = json.loads(entries_file.read_text())
        self.assertEqual(sorted(item['knowledge_point'] for item in entries), ['Alpha', 'Beta', 'Gamma'])
        self.assertEqual(len({item['id'] for item in entries}), 3)

    def test_unchanged_invalid_item_has_two_attempts_and_an_explicit_stop_reason(self):
        result = self.run_book(count=1, unchanged=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.pending[0].get('repair_attempts'), 2)
        self.assertEqual(self.pending[0].get('repair_stop_reason'), 'attempts_exhausted')

    def test_task_that_cannot_fit_alone_is_visible_as_input_budget_failure(self):
        self.run_book(count=1, input_limit=0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.pending[0].get('repair_stop_reason'), 'input_budget')
        self.assertEqual(self.pending[0].get('repair_attempts'), 0)

    def test_cached_timeout_and_content_failure_replay_the_same_later_504_payload(self):
        first=self.run_book(count=2,repair_batch_size=1,unchanged=True,mixed_transport=True)
        self.assertEqual(first['http504_pending_chunks'],1)
        self.assertEqual(first['http504_unresolved_chunks'],0)
        self.assertEqual(len(self.requests),2)
        try:
            second=self.run_book(count=2,repair_batch_size=1,unchanged=True,
                                 mixed_transport=True,transport_round=2)
        except ValueError as exc:
            self.fail(f'The global retry reconstructed a different repair payload: {exc}')
        self.assertEqual(second['http504_pending_chunks'],1)
        self.assertEqual(second['http504_unresolved_chunks'],1)
        self.assertEqual(len(self.requests),5)
        self.assertEqual(self.payloads[1],self.payloads[2])
        third=self.run_book(count=2,repair_batch_size=1,unchanged=True,
                            mixed_transport=True,transport_round=3)
        self.assertEqual(third['http504_pending_chunks'],0)
        self.assertEqual(third['http504_unresolved_chunks'],0)
        self.assertEqual(third['technical_failure_unresolved_chunks'],0)
        self.assertEqual(len(self.requests),7)
        self.assertEqual(self.payloads[4],self.payloads[5])
        self.assertEqual(third['repair_stop_reasons'],{'attempts_exhausted':2})


class EvidencedIdentityTests(unittest.TestCase):
    def source(self, text):
        return {'lo': 0, 'hi': 1, 'units': [
            {'unit': 0, 'offset': 0, 'text': text, 'kind': 'paragraph'}]}

    def test_definition_crop_preserves_the_original_source_name(self):
        part = self.source('double partition A partition consisting of two layers.\n')
        original = candidate(0, 'double partition A partition con', 'double partition')
        fixed = candidate(0, 'double partition')
        self.assertTrue(anchors.check_repair_identity(original, fixed, part))

    def test_definition_crop_requires_existing_literal_name_evidence(self):
        part = self.source('double partition A partition consisting of two layers.\n')
        original = candidate(0, 'double partition A partition con')
        self.assertFalse(anchors.check_repair_identity(original, candidate(0, 'double partition'), part))

    def test_lexical_compound_cannot_be_cropped_even_if_old_name_was_incomplete(self):
        part = self.source('Beta Gamma\n')
        self.assertFalse(anchors.check_repair_identity(
            candidate(0, 'Beta Gamma', 'Beta'), candidate(0, 'Beta'), part))

    def test_literal_parenthetical_acronym_can_extend_the_same_full_name(self):
        part = self.source('National Fire Protection Association (NFPA)\n')
        original = candidate(0, 'National Fire Protection Association')
        fixed = candidate(0, 'National Fire Protection Association (NFPA)')
        self.assertTrue(anchors.check_repair_identity(original, fixed, part))

    def test_source_parenthesis_is_not_enough_for_an_unrelated_alias(self):
        part = self.source('National Fire Protection Association (OTHER)\n')
        self.assertFalse(anchors.check_repair_identity(
            candidate(0, 'National Fire Protection Association'),
            candidate(0, 'National Fire Protection Association (OTHER)'), part))


if __name__ == '__main__':
    unittest.main()
