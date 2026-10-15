import importlib.util
import unittest


class CalibrationPacketTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('calibration_packets'),
                             'native replay packet construction is required')
        import calibration_packets
        return calibration_packets

    def test_native_extraction_messages_keep_ownership_and_output_budget(self):
        module = self.module()
        packet = module.extraction_packet('p', [{'unit': 0, 'text': 'term means definition',
                                                'offset': 0}], 0, 1, 'fake', 8192)
        self.assertEqual(packet['payload']['max_tokens'], 8192)
        self.assertFalse(packet['payload']['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(packet['payload']['response_format'], {'type': 'json_object'})
        self.assertEqual(packet['kind'], 'extraction')
        import json
        part = json.loads(packet['payload']['messages'][1]['content'])
        self.assertEqual((part['lo'], part['hi']), (0, 1))
        self.assertIn('OWNED', packet['payload']['messages'][0]['content'])

    def test_invalid_native_range_cannot_create_a_model_request(self):
        module = self.module()
        for lo, hi in ((-1, 1), (1, 0), (0, 2)):
            with self.assertRaises(ValueError):
                module.extraction_packet('p', [{'text': 'term'}], lo, hi, 'fake', 8192)

    def test_native_validation_rejects_invented_anchors_and_empty_positive_control(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        import concurrency_calibration
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'book.md'
            source.write_text('term means definition', encoding='utf-8')
            packet = module.extraction_packet('p', [{'unit': 0, 'text': 'term means definition',
                'kind': 'paragraph', 'offset': 0}], 0, 1, 'fake')
            packet.update(validation_book={'identifier': 'p', 'title': 'p', 'md_path': str(source),
                'md_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}, require_nonempty_entries=True)
            for entries in ([], [{'head': [{'unit': 0, 'quote': 'invented head'}], 'body': []}]):
                raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                    {'scanned_all': True, 'entries': entries})}}]}
                self.assertFalse(concurrency_calibration.validate_response(raw, 'extraction', packet)['protocol_valid'])

    def test_context_filter_rejects_oversize_payload_without_chat_submission(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        module = self.module()
        packets = [{'id': 'fit', 'payload': {'model': 'fake', 'max_tokens': 8192,
                    'messages': [{'role': 'user', 'content': 'a'}]}},
                   {'id': 'large', 'payload': {'model': 'fake', 'max_tokens': 8192,
                    'messages': [{'role': 'user', 'content': 'b'}]}}]
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(module, 'post_json', create=True,
                             side_effect=[{'count': 16000}, {'count': 30000}]) as post:
            selected = module.context_checked_packets('http://127.0.0.1:12345', packets,
                {'model': 'fake', 'configured_context': 32768}, Path(temp) / 'CHECK.json')
        self.assertEqual([value['id'] for value in selected], ['fit'])
        self.assertTrue(all(call.args[0].endswith('/tokenize') for call in post.call_args_list))

    def test_invalid_token_count_or_model_is_a_hard_preflight_failure(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        module = self.module()
        packet = {'id': 'p', 'payload': {'model': 'fake', 'max_tokens': 8192, 'messages': []}}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(module, 'post_json', create=True, return_value={'count': True}):
            with self.assertRaises(ValueError):
                module.context_checked_packets('http://127.0.0.1:12345', [packet],
                    {'model': 'fake', 'configured_context': 32768}, Path(temp) / 'CHECK.json')

    def test_important_native_payload_capture_cannot_make_a_network_call(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'inputs').mkdir()
            md = root / 'book.md'
            md.write_text('# 第一章 系统\n\n系统由若干部件组成。\n\n## 第二节 流程\n\n流程描述操作顺序。\n')
            rows = [{'identifier': f'b{number}', 'title': f'book{number}',
                     'md_path': str(md), 'pdf_path': None} for number in range(4)]
            (root / 'inputs/important_candidates_verified.json').write_text(json.dumps(rows))
            with patch('socket.create_connection', side_effect=AssertionError('capture egress')):
                packets = module.important_front_packets(root, root / 'output',
                    {'model': 'fake', 'configured_context': 32768}, maximum=4)
            self.assertEqual(len(packets), 4)
            self.assertTrue(all(packet['native_front_track'] == 'important' for packet in packets))
            self.assertTrue(all(packet['payload']['max_tokens'] == 8192 for packet in packets))
            self.assertTrue(all(not packet['payload']['chat_template_kwargs']['enable_thinking'] for packet in packets))
            self.assertEqual(json.loads((root / 'output/IMPORTANT_PACKET_PREPARATION.json').read_text())['real_model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
