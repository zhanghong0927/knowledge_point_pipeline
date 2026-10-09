import json
import unittest
from unittest.mock import patch

import recover_boundary_failures as recover


class BoundaryRecoveryTest(unittest.TestCase):
    def test_recovers_single_target_wrong_code_only_after_independent_review(self):
        target = {'code': 'expected', 'name_zh': '胜论派与正理派', 'path': '哲学/胜论派与正理派', 'depth': 2}
        card = {'node_code': 'invented', 'definition': '讨论胜论派与正理派的逻辑和范畴思想。',
                'boundary': '仅覆盖这两派的理论，其他印度哲学派别不作主挂载。',
                'includes': ['胜论派范畴论'], 'excludes': ['数论派思想'],
                'sibling_distinctions': [], 'cross_boundary_rule': '相关内容以两派理论为主时挂本节点。'}
        payload = {'targets': [target], 'sibling_context': [target]}
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'cards': [card]})}}]}
        saved = {'payload': payload, 'output': {'status': 'technical_failure', 'rounds': [
            {'generation': {'attempts': [{'raw': raw, 'error': 'foreign/duplicate card code'}]}}]}}
        reviewed = {'result': {'verdict': 'pass', 'checked_codes': ['expected'], 'issues': []},
                    'attempts': [{'raw': {'choices': [{'finish_reason': 'stop'}]}}]}
        with patch.object(recover.boundary, 'call', return_value=reviewed), \
             patch.object(recover.boundary, 'process_group', side_effect=AssertionError('unneeded regeneration')):
            result = recover.recover_group(saved, 'endpoint', 'model', 1000)
        self.assertTrue(result['recovered'])
        self.assertEqual(result['output']['cards'][0]['node_code'], 'expected')
        self.assertEqual(result['output']['automatic_recovery'], 'single_target_code_correction_reviewed')

    def test_splits_targets_but_retains_all_siblings(self):
        payload = {'targets': [{'code': 'a'}, {'code': 'b'}],
                   'sibling_context': [{'code': 'a'}, {'code': 'b'}, {'code': 'c'}],
                   'parent_card': {'node_code': 'p'}}
        one = recover.split_payload(payload, payload['targets'][0])
        self.assertEqual([x['code'] for x in one['targets']], ['a'])
        self.assertEqual(one['sibling_context'], payload['sibling_context'])
        self.assertEqual(one['parent_card'], payload['parent_card'])

    def test_does_not_recover_partial_group(self):
        payload = {'targets': [{'code': 'a'}, {'code': 'b'}], 'sibling_context': []}
        saved = {'payload': payload, 'output': {'status': 'technical_failure'}}
        parts = [
            {'status': 'accepted_candidate', 'cards': [{'node_code': 'a'}]},
            {'status': 'technical_failure'},
        ]
        with patch.object(recover.boundary, 'process_group', side_effect=parts):
            result = recover.recover_group(saved, 'endpoint', 'model', 1000)
        self.assertFalse(result['recovered'])


if __name__ == '__main__':
    unittest.main()
