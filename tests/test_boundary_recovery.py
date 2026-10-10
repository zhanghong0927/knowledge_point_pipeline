"""Exercise automatic recovery with the real validators and an offline service."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

NATIVE = Path(__file__).resolve().parents[1] / 'modules/mounting/pipeline'
sys.path.insert(0, str(NATIVE))
boundary = importlib.import_module('generate_semantic_boundaries')


def nodes():
    return boundary.normalize({'code': 'r', 'name': 'Transport', 'children': [
        {'code': 'a', 'name': 'Rail', 'children': [
            {'code': 'a1', 'name': 'Vehicles'}, {'code': 'a2', 'name': 'Signals'}]},
        {'code': 'b', 'name': 'Road'}]})


def card(code):
    return {'node_code': code, 'definition': 'Object and functional scope ' + code,
            'boundary': 'Keep original subject scope ' + code,
            'includes': ['Subject objects'], 'excludes': ['Unrelated objects'],
            'sibling_distinctions': [], 'cross_boundary_rule': 'Use object and application context.'}


class Service:
    def __init__(self, broken=(), cross_bad=None, repairable=False):
        self.broken = set(broken)
        self.cross_bad = cross_bad
        self.repairable = repairable
        self.generations = []
        self.cross_reviews = []

    def __call__(self, url, request=None):
        if request is None:
            return {'data': [{'id': 'model'}]}
        payload = json.loads(request['messages'][-1]['content'])
        prompt = request['messages'][0]['content']
        codes = [node['code'] for node in payload['targets']]
        if prompt.startswith(boundary.PAIR_PROMPT):
            code = payload['cards'][0]['node_code']
            self.cross_reviews.append(code)
            bad = code == self.cross_bad and 'repaired' not in payload['cards'][0]['boundary']
            checks = [{'ancestor_code': item['node']['code'],
                       'verdict': 'needs_revision' if bad and item == payload['ancestors'][-1] else 'pass',
                       'reason': 'Incompatible scopes.' if bad else 'Compatible scopes.'}
                      for item in payload['ancestors']]
            issues = []
            if bad:
                ancestor = payload['ancestors'][-1]
                issues = [{'ancestor_code': ancestor['node']['code'], 'descendant_code': code,
                           'conflict_type': 'ancestor_restriction',
                           'ancestor_field': 'excludes', 'ancestor_quote': ancestor['card']['excludes'][0],
                           'descendant_field': 'includes', 'descendant_quote': 'Subject objects',
                           'reason': 'Specific exclusions conflict with the named child scope.'}]
            obj = {'verdict': 'needs_revision' if bad else 'pass', 'checked_codes': [code],
                   'issues': issues, 'ancestor_checks': checks, 'self_issues': [],
                   'self_checks': [{'node_code': code, 'verdict': 'pass', 'reason': 'Internally consistent.'}]}
        elif prompt.startswith(boundary.REVIEW_PROMPT):
            obj = {'verdict': 'pass', 'checked_codes': codes, 'issues': []}
        else:
            self.generations.append((codes, payload))
            if self.broken.intersection(codes):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{invalid'}}]}
            result = [card(code) for code in codes]
            if self.repairable and payload.get('cross_revision_feedback'):
                for value in result:
                    value['boundary'] += ' repaired'
            obj = {'cards': result}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(obj)}}]}


class BoundaryRecoveryTests(unittest.TestCase):
    def test_single_cross_review_does_not_require_missing_sibling_card(self):
        tree = nodes()
        cards = {code: card(code) for code in ['r', 'a', 'a1']}
        payload = boundary.single_cross_payload(tree, 'a1', cards)
        self.assertEqual([value['node_code'] for value in payload['cards']], ['a1'])
        self.assertEqual([value['code'] for value in payload['sibling_context']], ['a1', 'a2'])

    def test_structural_retry_receives_previous_output_as_data(self):
        seen = []
        def service(url, request):
            payload = json.loads(request['messages'][-1]['content'])
            seen.append(payload)
            obj = {'wrong': 1} if len(seen) == 1 else {'good': 1}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(obj)}}]}
        def validate(obj):
            if 'good' not in obj:
                raise ValueError('required field good is missing')
            return obj
        with patch.object(boundary, 'request', side_effect=service), patch.object(boundary.time, 'sleep'):
            result = boundary.call('base', 'model', 'JSON schema', {'targets': []}, validate)
        self.assertEqual(result['result'], {'good': 1})
        self.assertEqual(seen[1]['repair_feedback']['previous_output'], '{"wrong": 1}')
        self.assertIn('good', seen[1]['repair_feedback']['error'])

    def recover(self, directory, service, reuse=None, policy='empty', rewrites=2):
        recovery = importlib.import_module('boundary_recovery')
        with patch.object(boundary, 'request', side_effect=service), patch.object(boundary.time, 'sleep'):
            return recovery.run(nodes(), Path(directory), 'base', 'model', 4, 50000,
                                policy=policy, rewrite_rounds=rewrites, reuse=reuse)

    def test_exhausted_technical_failure_is_empty_not_fake_verified_text(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, Service(broken=['a1']))
            rows = [json.loads(line) for line in (Path(d) / 'runtime_cards.jsonl').read_text().splitlines()]
            failed = [row for row in rows if row.get('provenance') == 'empty_boundary_fallback']
            self.assertTrue(result['runtime_release'])
            self.assertFalse(result['cross_validated_release'])
            self.assertGreater(len(failed), 0)
            for row in failed:
                self.assertEqual(row['definition'], '')
                self.assertEqual(row['boundary'], '')
                self.assertEqual(row['semantic_card'], '')
            self.assertEqual({row['node_code'] for row in rows}, {'r', 'a', 'b', 'a1', 'a2'})
            self.assertEqual((Path(d) / 'cross_validated_cards.jsonl').read_text(), '')

    def test_cross_conflict_is_rewritten_then_rechecked(self):
        service = Service(cross_bad='a1', repairable=True)
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, service)
            self.assertTrue(result['cross_validated_release'])
            self.assertEqual(result['empty_boundary_nodes'], 0)
            self.assertEqual(result['cross_rewrite_rounds'], 1)
            root_calls = [codes for codes, payload in service.generations if codes == ['r']]
            self.assertEqual(len(root_calls), 1)

    def test_unrepairable_cross_conflict_falls_back_without_claiming_pass(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, Service(cross_bad='a1'))
            self.assertTrue(result['runtime_release'])
            self.assertEqual(result['empty_boundary_nodes'], 4)
            self.assertEqual(result['cross_rewrite_rounds'], 2)
            fallbacks = json.loads((Path(d) / 'boundary_fallbacks.json').read_text())
            self.assertEqual({row['node_code'] for row in fallbacks}, {'a', 'b', 'a1', 'a2'})

    def test_strict_policy_does_not_release_unresolved_cards(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, Service(broken=['a1']), policy='strict')
            self.assertFalse(result['runtime_release'])
            self.assertEqual(result['empty_boundary_nodes'], 0)

    def test_reuse_keeps_good_groups_and_only_regenerates_failed_groups(self):
        with tempfile.TemporaryDirectory() as d:
            prior, current = Path(d) / 'prior', Path(d) / 'current'
            self.recover(prior, Service(broken=['a1']))
            service = Service()
            result = self.recover(current, service, reuse=prior)
            self.assertEqual(result['empty_boundary_nodes'], 0)
            self.assertGreater(result['reused_groups'], 0)
            self.assertFalse(any('r' in codes or 'b' in codes for codes, payload in service.generations))

    def test_multi_target_failure_recovers_with_single_targets_and_joint_review(self):
        service = Service()
        def batch_sensitive(url, request=None):
            if request is not None:
                payload = json.loads(request['messages'][-1]['content'])
                prompt = request['messages'][0]['content']
                if 'cards' not in payload and len(payload['targets']) > 1:
                    return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{invalid'}}]}
            return service(url, request)
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, batch_sensitive)
            self.assertTrue(result['cross_validated_release'])
            self.assertEqual(result['empty_boundary_nodes'], 0)
            recovered = [json.loads(path.read_text())['output'] for path in (Path(d) / 'groups').glob('*.json')]
            self.assertEqual(sum(item.get('automatic_recovery') == 'single_target_full_sibling_context' for item in recovered), 2)

    def test_failed_parent_falls_back_descendants_without_inventing_descriptions(self):
        service = Service(broken=['a'])
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, service)
            self.assertTrue(result['runtime_release'])
            self.assertEqual(result['empty_boundary_nodes'], 4)
            self.assertFalse(any('a1' in codes or 'a2' in codes for codes, payload in service.generations))

    def test_changed_reuse_tree_is_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as d:
            prior = Path(d) / 'prior'
            self.recover(prior, Service())
            saved = json.loads((prior / 'normalized_nodes.json').read_text())
            saved[-1]['name_zh'] = 'Different object'
            (prior / 'normalized_nodes.json').write_text(json.dumps(saved))
            service = Service()
            with self.assertRaisesRegex(ValueError, 'taxonomy changed'):
                self.recover(Path(d) / 'new', service, reuse=prior)
            self.assertEqual(service.generations, [])

    def test_old_cross_pass_is_not_reused_after_cross_prompt_changes(self):
        with tempfile.TemporaryDirectory() as d:
            prior = Path(d) / 'prior'
            self.recover(prior, Service())
            (prior / 'cross_prompt.txt').write_text('obsolete review policy')
            service = Service()
            result = self.recover(Path(d) / 'current', service, reuse=prior)
            self.assertTrue(result['runtime_release'])
            self.assertEqual(service.generations, [])
            self.assertEqual(set(service.cross_reviews), {'r', 'a', 'b', 'a1', 'a2'})

    def test_strict_failed_ancestor_rewrite_invalidates_previously_accepted_children(self):
        service = Service(cross_bad='a1')
        def fail_rewrite(url, request=None):
            if request is not None:
                payload = json.loads(request['messages'][-1]['content'])
                if 'cards' not in payload and payload.get('cross_revision_feedback'):
                    service.broken.add('a')
            return service(url, request)
        with tempfile.TemporaryDirectory() as d:
            result = self.recover(d, fail_rewrite, policy='strict')
            self.assertFalse(result['runtime_release'])
            rows = [json.loads(line) for line in (Path(d) / 'semantic_cards.jsonl').read_text().splitlines()]
            self.assertEqual([row['node_code'] for row in rows], ['r'])
            states = json.loads((Path(d) / 'node_status.json').read_text())
            self.assertEqual({row['node_code'] for row in states if row['status'] == 'blocked_by_parent'}, {'a1', 'a2'})


if __name__ == '__main__':
    unittest.main()
