"""Real native payload construction and local cleaning contracts, without a model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from concurrency_calibration import validate_response

try:
    import important_calibration
except ImportError:
    important_calibration = None


def cleaning_packet():
    source = {'name': '齿轮', 'knowledge_point': '', 'description': '齿轮传递机械运动。'}
    return {'kind': 'important_cleaning', 'payload': {'messages': [
        {'role': 'system', 'content': 'native'}, {'role': 'user', 'content': json.dumps({
        'batch_id': 'batch-00000001', 'items': [{'key': 'k1'}]})}]},
        'validation_batch': [{'key': 'k1', 'source_row': source}],
        'validation_args': {'model': 'native', 'confidence_threshold': .8, 'fix_swapped_languages': False}}


def cleaning_reply(**changes):
    item = {'key': 'k1', 'decision': 'keep', 'confidence': .95, 'subject_relevant': True,
            'field_results': {'name': {'decision': 'keep', 'reason': '原文标题'},
                             'knowledge_point': {'decision': 'empty', 'reason': '原文为空'}},
            'pair_consistency': 'not_applicable', 'name': '齿轮', 'knowledge_point': '',
            'definition': '', 'en_definition': '', 'description': '齿轮传递机械运动。',
            'en_description': '', 'reason': '原文可核验', 'quality_flags': [], 'evidence': '齿轮'}
    item.update(changes)
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'batch_id': 'batch-00000001', 'items': [item]}, ensure_ascii=False)}}]}


class ImportantCalibrationTests(unittest.TestCase):
    def test_single_subject_source_can_calibrate_two_real_books(self):
        self.assertIsNotNone(important_calibration)
        validator = getattr(important_calibration, 'validate_workload_coverage', None)
        self.assertTrue(callable(validator), 'native workload coverage validation is missing')
        packets = [{'source_identifier': book, 'subject_slug': 'mechanical_engineering'}
                   for book in ('first', 'second') for _ in (1, 2)]
        rows = [{'identifier': book, 'subject_slug': 'mechanical_engineering'} for book in ('first', 'second')]
        validator(packets, rows)

    def test_available_multiple_subjects_require_representative_packets(self):
        self.assertIsNotNone(important_calibration)
        validator = getattr(important_calibration, 'validate_workload_coverage', None)
        self.assertTrue(callable(validator), 'native workload coverage validation is missing')
        packets = [{'source_identifier': book, 'subject_slug': 'first_subject'}
                   for book in ('first', 'second') for _ in (1, 2)]
        rows = [{'identifier': 'first', 'subject_slug': 'first_subject'},
                {'identifier': 'second', 'subject_slug': 'first_subject'},
                {'identifier': 'third', 'subject_slug': 'second_subject'}]
        with self.assertRaisesRegex(ValueError, 'representative'):
            validator(packets, rows)

    def test_important_calibration_scope_is_preserved_in_applied_profile(self):
        from stage_scheduling import ResourceProfile
        from staged_benchmark import calibration_evidence
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            level = {'concurrency': 32, 'requests': 64, 'protocol_valid': 64,
                     'transport_errors': 0, 'wall_seconds': 10, 'p95_seconds': 2,
                     'population_sha256': 'fixed', 'passes': [], 'groups': {}}
            report = {'schema': 1, 'status': 'completed', 'semantic_quality_approved': False,
                      'front': {'selected_limit': 32, 'levels': [level]},
                      'extraction': {'selected_limit': 32, 'levels': [level]}, 'model_limit': 32,
                      'measurement_scope': ['important_front', 'important_cleaning']}
            path = root / 'report.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            profile = ResourceProfile(front_limit=32, extraction_limit=32, model_limit=32)
            evidence = calibration_evidence(path, profile)
            observed = replace(profile, calibration_evidence=evidence).as_dict()
            self.assertEqual(observed['performance_calibration_scope'], ['important_front', 'important_cleaning'])
            self.assertTrue(observed['cleaning_budget_calibrated'])
            self.assertFalse(observed['mounting_budget_calibrated'])

    def test_cleaning_capture_includes_native_schema_and_grounding_source(self):
        self.assertIsNotNone(important_calibration, 'native cleaning capture is missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'rule_pass.jsonl'
            source.write_text(json.dumps({'id': 'k1', 'name': '齿轮', 'knowledge_point': '',
                              'description': '齿轮传递机械运动。'}, ensure_ascii=False) + '\n', encoding='utf-8')
            command = ['python3', str(Path(__file__).resolve().parents[1] / 'modules/cleaning/scripts/model_clean.py'),
                       '--input', str(source), '--output-dir', str(root / 'output'),
                       '--subject', '机械工程', '--api-url', 'http://127.0.0.1:9/v1/chat/completions',
                       '--model', 'native', '--batch-size', '8', '--max-tokens', '8192',
                       '--response-format', 'schema', '--defer-transport-retry']
            packets = important_calibration.capture_cleaning_command(command, maximum_batches=1)
            self.assertEqual(len(packets), 1)
            packet = packets[0]
            self.assertEqual(packet['payload']['response_format']['type'], 'json_schema')
            self.assertEqual(packet['payload']['max_tokens'], 8192)
            items = json.loads(packet['payload']['messages'][1]['content'])['items']
            self.assertEqual(items[0]['name'], '齿轮')
            self.assertEqual(packet['validation_batch'][0]['source_row']['description'], '齿轮传递机械运动。')
            self.assertFalse((root / 'output').exists())

    def test_valid_cleaning_payload_passes_native_evidence_guards(self):
        self.assertTrue(validate_response(cleaning_reply(), 'important_cleaning', cleaning_packet())['protocol_valid'])

    def test_wrong_cleaning_item_key_is_rejected(self):
        self.assertFalse(validate_response(cleaning_reply(key='other'), 'important_cleaning',
                         cleaning_packet())['protocol_valid'])

    def test_missing_cleaning_contract_fields_are_rejected(self):
        raw = cleaning_reply()
        value = json.loads(raw['choices'][0]['message']['content'])
        del value['items'][0]['field_results']
        raw['choices'][0]['message']['content'] = json.dumps(value)
        self.assertFalse(validate_response(raw, 'important_cleaning', cleaning_packet())['protocol_valid'])

    def test_ungrounded_kept_content_is_rejected_by_the_native_cleaner(self):
        self.assertFalse(validate_response(cleaning_reply(description='数据库事务支持并发控制。'),
                         'important_cleaning', cleaning_packet())['protocol_valid'])

    def test_calibration_rows_are_balanced_without_changing_source_records(self):
        self.assertIsNotNone(important_calibration, 'important native sample selection is missing')
        rows = [{'identifier': f'{subject}{n}', 'subject_slug': subject, 'priority_rank': n}
                for subject in ('first', 'second', 'third') for n in (1, 2, 3)]
        original = copy.deepcopy(rows)
        selected = important_calibration.balanced_rows(rows, maximum=5)
        self.assertEqual([row['identifier'] for row in selected], ['first1', 'second1', 'third1', 'first2', 'second2'])
        self.assertEqual(rows, original)


if __name__ == '__main__':
    unittest.main()
