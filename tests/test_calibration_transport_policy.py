"""Calibration health and native content evidence are separate offline contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import concurrency_calibration as calibration


def cleaning_packet(model='grounded'):
    return {'id': model, 'kind': 'important_cleaning',
            'payload': {'model': model, 'messages': [
                {'role': 'system', 'content': 'native cleaning'},
                {'role': 'user', 'content': json.dumps({
                    'batch_id': 'batch-00000001', 'items': [{'key': 'k1'}]})}]},
            'validation_batch': [{'key': 'k1', 'source_row': {
                'name': '齿轮', 'knowledge_point': '', 'description': '齿轮传递机械运动。'}}],
            'validation_args': {'model': model, 'confidence_threshold': .8,
                                'fix_swapped_languages': False}}


def cleaning_reply(description='齿轮传递机械运动。'):
    item = {'key': 'k1', 'decision': 'keep', 'confidence': .95, 'subject_relevant': True,
            'field_results': {'name': {'decision': 'keep', 'reason': '原文标题'},
                             'knowledge_point': {'decision': 'empty', 'reason': '原文为空'}},
            'pair_consistency': 'not_applicable', 'name': '齿轮', 'knowledge_point': '',
            'definition': '', 'en_definition': '', 'description': description,
            'en_description': '', 'reason': '原文可核验', 'quality_flags': [], 'evidence': '齿轮'}
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
        'batch_id': 'batch-00000001', 'items': [item]}, ensure_ascii=False)}}]}


def level(limit, wall, latency, *, content=0, api=100, transport=0):
    counts = {'requests': 100, 'protocol_valid': content, 'content_valid': content,
              'api_protocol_valid': api, 'transport_errors': transport,
              'api_protocol_errors': 100 - api - transport,
              'content_validation_errors': api - content}
    return dict(counts, concurrency=limit, wall_seconds=wall, p95_seconds=latency,
                population_sha256='same-population', groups={'important_cleaning': dict(counts)})


class ResponseOpener:
    """Replace only the remote HTTP boundary; validators and receipts stay real."""
    def __init__(self, replies):
        self.replies = replies

    def open(self, request, timeout):
        model = json.loads(request.data)['model']
        return BytesResponse(json.dumps(self.replies[model], ensure_ascii=False).encode())


class BytesResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, maximum):
        return self.body[:maximum]


class CalibrationTransportPolicyTests(unittest.TestCase):
    def test_ungrounded_content_keeps_api_health_and_fails_native_content(self):
        checked = calibration.validate_response(
            cleaning_reply('数据库事务支持并发控制。'), 'important_cleaning', cleaning_packet())
        self.assertIs(checked.get('api_protocol_valid'), True)
        self.assertIs(checked.get('content_valid'), False)
        self.assertIs(checked['protocol_valid'], False)  # Keep the legacy strict guard.
        self.assertIn('evidence validation failed', checked['content_validation_error'])
        self.assertNotIn('api_protocol_error', checked)

    def test_complete_api_response_with_bad_model_json_is_a_content_failure(self):
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{broken'}}]}
        checked = calibration.validate_response(raw, 'extraction')
        self.assertIs(checked.get('api_protocol_valid'), True)
        self.assertIs(checked.get('content_valid'), False)
        self.assertIn('content_validation_error', checked)

    def test_http_200_does_not_make_broken_or_truncated_api_protocol_valid(self):
        for raw in (None, {}, {'error': {'message': 'server error'}}, {'choices': []},
                    {'choices': [None]},
                    {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
                    {'choices': [{'finish_reason': 'stop', 'message': None}]},
                    {'choices': [{'finish_reason': 'stop', 'message': {'content': {}}}]}):
            with self.subTest(raw=raw):
                checked = calibration.validate_response(raw, 'extraction')
                self.assertIs(checked.get('api_protocol_valid'), False)
                self.assertIs(checked.get('content_valid'), False)
                self.assertIs(checked['protocol_valid'], False)
                self.assertIn('api_protocol_error', checked)
                self.assertNotIn('content_validation_error', checked)

    def test_api_goodput_selects_level_even_when_every_native_content_check_fails(self):
        slower = level(32, 20, 3, content=100)
        faster = level(64, 10, 2)
        faster['passes'] = [copy.deepcopy(faster), copy.deepcopy(faster)]
        selected = calibration.choose_level([slower, faster])
        self.assertIsNotNone(selected)
        self.assertEqual(selected['concurrency'], 64)

    def test_bad_api_protocol_cannot_win_on_http_success_and_raw_throughput(self):
        healthy = level(32, 20, 3)
        broken = level(64, 5, 1, api=80)
        selected = calibration.choose_level([healthy, broken])
        self.assertIsNotNone(selected)
        self.assertEqual(selected['concurrency'], 32)

    def test_group_transport_failure_is_still_rejected_with_good_api_aggregate(self):
        row = level(64, 10, 2, api=99, transport=1)
        row['groups'] = {
            'dictionary': {'requests': 90, 'protocol_valid': 0, 'content_valid': 0,
                           'api_protocol_valid': 90, 'transport_errors': 0},
            'important': {'requests': 10, 'protocol_valid': 0, 'content_valid': 0,
                          'api_protocol_valid': 9, 'transport_errors': 1}}
        self.assertIsNone(calibration.choose_level([row]))

    def test_combined_passes_preserve_api_and_content_counts_per_group(self):
        combined = calibration.combine_passes([level(32, 10, 2), level(32, 12, 3)])
        row = combined[0]
        self.assertEqual(row.get('api_protocol_valid'), 200)
        self.assertEqual(row.get('content_valid'), 0)
        self.assertEqual(row.get('content_validation_errors'), 200)
        self.assertEqual(row.get('api_protocol_errors'), 0)
        self.assertEqual(row['groups']['important_cleaning'].get('api_protocol_valid'), 200)
        self.assertEqual(row['groups']['important_cleaning'].get('content_validation_errors'), 200)
        self.assertEqual(calibration.choose_level(combined)['concurrency'], 32)

    def test_legacy_or_mixed_reports_do_not_invent_split_validation_counts(self):
        legacy = {'concurrency': 32, 'requests': 100, 'protocol_valid': 100,
                  'transport_errors': 0, 'wall_seconds': 10, 'p95_seconds': 2,
                  'population_sha256': 'same-population', 'groups': {}}
        self.assertEqual(calibration.choose_level([legacy])['concurrency'], 32)
        combined = calibration.combine_passes([legacy, level(32, 10, 2)])[0]
        self.assertNotIn('api_protocol_valid', combined)
        self.assertNotIn('content_valid', combined)
        self.assertNotIn('content_validation_errors', combined)
        self.assertIsNone(calibration.choose_level([combined]))

    def test_replay_receipts_separate_broken_api_from_rejected_content(self):
        replies = {'grounded': cleaning_reply(),
                   'ungrounded': cleaning_reply('数据库事务支持并发控制。'),
                   'broken': {'error': {'message': 'upstream error'}}}
        packets = [cleaning_packet(model) for model in replies]
        with tempfile.TemporaryDirectory() as temp, patch.object(
                calibration, 'build_opener', return_value=ResponseOpener(replies)):
            root = Path(temp) / 'replay'
            summary = calibration.run_level('http://offline.invalid', 'important_cleaning',
                                            packets, 2, root, 3)
            receipts = [json.loads((root / 'requests' / f'{index:04}.json').read_text())
                        for index in range(3)]
            saved = json.loads((root / 'SUMMARY.json').read_text())
        self.assertEqual(summary.get('api_protocol_valid'), 2)
        self.assertEqual(summary.get('content_valid'), 1)
        self.assertEqual(summary['protocol_valid'], 1)
        self.assertEqual(summary.get('api_protocol_errors'), 1)
        self.assertEqual(summary.get('content_validation_errors'), 1)
        self.assertEqual(summary['transport_errors'], 0)
        self.assertEqual(summary['http_statuses'], {'200': 3})
        self.assertEqual(saved, summary)
        self.assertIs(summary['semantic_quality_approved'], False)
        self.assertIn('content_validation_error', receipts[1])
        self.assertNotIn('api_protocol_error', receipts[1])
        self.assertIn('api_protocol_error', receipts[2])
        self.assertNotIn('content_validation_error', receipts[2])

    def test_calibration_and_report_consumer_do_not_hold_for_content_evidence(self):
        from stage_scheduling import ResourceProfile
        from staged_benchmark import calibration_evidence

        class Gateway:
            api_url = 'http://offline.invalid'

            def stats(self):
                return {'phase': 'offline', 'limit': 4, 'routes': {}}

        reply = cleaning_reply('数据库事务支持并发控制。')
        with tempfile.TemporaryDirectory() as temp, patch.object(
                calibration, 'transition_phase'), patch.object(
                calibration, 'build_opener', return_value=ResponseOpener({'ungrounded': reply})):
            root = Path(temp)
            result = calibration.calibrate_stage(Gateway(), 'important_cleaning',
                [cleaning_packet('ungrounded')], [2, 4], root, 3, 4)
            applied = result['selected_limit']
            report = {'schema': 1, 'status': 'completed', 'semantic_quality_approved': False,
                      'front': result, 'extraction': result, 'model_limit': applied,
                      'measurement_scope': ['important_front', 'important_cleaning']}
            report_path = root / 'report.json'
            report_path.write_text(json.dumps(report), encoding='utf-8')
            evidence = calibration_evidence(report_path, ResourceProfile(
                front_limit=applied, extraction_limit=applied, model_limit=applied))
        self.assertIn(applied, (2, 4))
        self.assertEqual(len(result['levels']), 2)
        self.assertIs(result['semantic_quality_approved'], False)
        self.assertIs(evidence['semantic_quality_approved'], False)
        self.assertEqual(evidence['scope'], ['important_front', 'important_cleaning'])
        for row in result['levels']:
            self.assertEqual(row['api_protocol_valid'], 8)
            self.assertEqual(row['protocol_valid'], 0)
            self.assertEqual(row['content_validation_errors'], 8)


if __name__ == '__main__':
    unittest.main()
