"""Offline contract tests for generation without model review."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

NATIVE = Path(__file__).resolve().parents[1] / 'modules/mounting/pipeline'
sys.path.insert(0, str(NATIVE))
b = importlib.import_module('generate_semantic_boundaries')


def nodes():
    return b.normalize({'code': 'r', 'name': 'Transport', 'children': [
        {'code': 'a', 'name': 'Rail', 'children': [{'code': 'a1', 'name': 'Vehicles'}]},
        {'code': 'b', 'name': 'Road'}]})


def card(code):
    return dict(node_code=code, definition='Object and scope ' + code,
                boundary='Original subject scope ' + code, includes=['Subject objects'],
                excludes=[], sibling_distinctions=[], cross_boundary_rule='')


class Service:
    def __init__(self, broken=()):
        self.broken = set(broken)
        self.calls = []

    def __call__(self, url, request):
        prompt = request['messages'][0]['content']
        payload = json.loads(request['messages'][-1]['content'])
        codes = [n['code'] for n in payload['targets']]
        self.calls.append((prompt, codes, payload))
        content = '{invalid' if self.broken.intersection(codes) else json.dumps({'cards': [card(c) for c in codes]})
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}


class UnreviewedTests(unittest.TestCase):
    def run_boundary(self, out, service, policy='strict', reuse=None, max_bytes=50000):
        module = importlib.import_module('boundary_unreviewed')
        with patch.object(b, 'request', side_effect=service), patch.object(b.time, 'sleep'):
            return module.run(nodes(), out, 'base', 'model', 3, max_bytes, policy=policy, reuse=reuse)

    def test_full_tree_generates_only_and_releases_unreviewed_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            service = Service()
            summary = self.run_boundary(Path(d), service)
            self.assertEqual([codes for _, codes, _ in service.calls], [['r'], ['a', 'b'], ['a1']])
            self.assertTrue(all(prompt.startswith(b.PROMPT) for prompt, _, _ in service.calls))
            self.assertTrue(summary['runtime_release'])
            self.assertEqual(summary['unreviewed_candidate_cards'], 4)
            self.assertEqual(summary['accepted_candidate_cards'], 0)
            self.assertFalse(summary['model_reviewed'])
            self.assertFalse(summary['cross_validated_release'])
            self.assertEqual(summary['review_mode'], 'off')
            self.assertEqual(summary['cross_review'], 'off')
            self.assertEqual((Path(d) / 'cross_validated_cards.jsonl').read_text(), '')
            self.assertFalse((Path(d) / 'cross_reviews').exists())
            cards = [json.loads(s) for s in (Path(d) / 'runtime_cards.jsonl').read_text().splitlines()]
            self.assertEqual([c['node_code'] for c in cards], ['r', 'a', 'a1', 'b'])
            self.assertTrue(all(c['provenance'] == 'llm_candidate' for c in cards))
            self.assertEqual(json.loads((Path(d) / 'knowledge_tree.json').read_text())['code'], 'r')
            for path in (Path(d) / 'groups').glob('*.json'):
                saved = json.loads(path.read_text())
                self.assertEqual(saved['output']['status'], 'unreviewed_candidate')
                self.assertTrue(saved['output']['rounds'][0]['generation']['attempts'])
                self.assertEqual(saved['output']['cards'], saved['output']['rounds'][0]['generation']['result'])

    def test_strict_technical_failure_is_not_drop_or_release(self):
        with tempfile.TemporaryDirectory() as d:
            summary = self.run_boundary(Path(d), Service(broken={'a'}))
            self.assertFalse(summary['runtime_release'])
            self.assertEqual(summary['status_counts']['technical_failure'], 2)
            self.assertEqual(summary['status_counts']['blocked_by_parent'], 1)
            self.assertNotIn('DROP', summary['status_counts'])
            self.assertEqual((Path(d) / 'runtime_cards.jsonl').read_text(), '')
            failed = [json.loads(p.read_text())['output'] for p in (Path(d) / 'groups').glob('*.json')
                      if json.loads(p.read_text())['output']['status'] == 'technical_failure']
            self.assertEqual(len(failed[0]['rounds'][0]['generation']['attempts']), 3)

    def test_context_budget_is_technical_and_makes_no_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            service = Service()
            summary = self.run_boundary(Path(d), service, max_bytes=1)
            self.assertEqual(service.calls, [])
            self.assertFalse(summary['runtime_release'])
            self.assertEqual(summary['status_counts']['technical_failure'], 1)
            saved = next((Path(d) / 'groups').glob('*.json'))
            self.assertEqual(json.loads(saved.read_text())['output']['reason'], 'context_byte_budget_exceeded')

    def test_extra_response_fields_are_retried_as_technical_failure(self):
        module = importlib.import_module('boundary_unreviewed')
        service = Service()
        def extra_response(url, request):
            response = service(url, request)
            response['choices'][0]['message']['content'] = json.dumps(
                {'cards': [card('r')], 'review': {'verdict': 'pass'}})
            return response
        payload = b.planned_groups(nodes(), None, {})[0]
        with patch.object(b, 'request', side_effect=extra_response), patch.object(b.time, 'sleep'):
            result = module.generate_group('base', 'model', payload, 50000)
        self.assertEqual(result['status'], 'technical_failure')
        self.assertEqual(len(service.calls), 3)

    def test_empty_fallback_marks_technical_failure_and_descendants(self):
        with tempfile.TemporaryDirectory() as d:
            summary = self.run_boundary(Path(d), Service(broken={'r'}), policy='empty')
            self.assertTrue(summary['runtime_release'])
            self.assertEqual(summary['empty_boundary_nodes'], 4)
            fallback = json.loads((Path(d) / 'boundary_fallbacks.json').read_text())
            self.assertEqual(fallback[0]['reason'], 'generation_technical_failure')
            self.assertTrue(all(item['reason'] == 'parent_empty_boundary' for item in fallback[1:]))
            self.assertEqual(summary['unreviewed_candidate_cards'], 0)

    def test_validator_rejects_identity_tamper_and_review_evidence(self):
        module = importlib.import_module('boundary_unreviewed')
        payload = b.planned_groups(nodes(), None, {})[0]
        cards = b.validate_cards({'cards': [card('r')]}, payload['targets'], payload['sibling_context'])
        raw = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'cards': [card('r')]})}}]}
        output = {'status': 'unreviewed_candidate', 'cards': cards,
                  'rounds': [{'generation': {'result': cards, 'attempts': [{'raw': raw}]}}]}
        self.assertEqual(module.validate_group(payload, output), cards)
        tampered = json.loads(json.dumps(output))
        tampered['cards'][0]['node_path'] = 'forged'
        with self.assertRaises(ValueError):
            module.validate_group(payload, tampered)
        reviewed = json.loads(json.dumps(output))
        reviewed['rounds'][0]['review'] = {'result': {'verdict': 'pass'}}
        with self.assertRaises(ValueError):
            module.validate_group(payload, reviewed)
        forged = json.loads(json.dumps(output))
        forged['rounds'][0]['generation']['attempts'][-1]['raw']['choices'][0]['message']['content'] = json.dumps({'cards': [card('a')]})
        with self.assertRaises(ValueError):
            module.validate_group(payload, forged)
        raw_review = json.loads(json.dumps(output))
        raw_review['rounds'][0]['generation']['attempts'][-1]['raw']['choices'][0]['message']['content'] = json.dumps(
            {'cards': [card('r')], 'review': {'verdict': 'pass'}})
        with self.assertRaises(ValueError):
            module.validate_group(payload, raw_review)
        output['status'] = 'accepted_candidate'
        with self.assertRaises(ValueError):
            module.validate_group(payload, output)

    def test_resume_only_reuses_identical_payload_and_generation(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'run'
            self.run_boundary(out, Service())
            service = Service()
            summary = self.run_boundary(out, service)
            self.assertEqual(summary['reused_groups'], 3)
            self.assertEqual(service.calls, [])
            root = next(p for p in (out / 'groups').glob('*.json')
                        if json.loads(p.read_text())['payload']['targets'][0]['code'] == 'r')
            saved = json.loads(root.read_text())
            saved['payload']['targets'][0]['name_zh'] = 'Changed context'
            root.write_text(json.dumps(saved))
            service = Service()
            summary = self.run_boundary(out, service)
            self.assertEqual([codes for _, codes, _ in service.calls], [['r']])
            self.assertEqual(summary['reused_groups'], 2)

    def test_reuse_does_not_import_reviewed_groups(self):
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / 'old', Path(d) / 'new'
            self.run_boundary(old, Service())
            for path in (old / 'groups').glob('*.json'):
                saved = json.loads(path.read_text())
                saved['output']['status'] = 'accepted_candidate'
                path.write_text(json.dumps(saved))
            service = Service()
            summary = self.run_boundary(new, service, reuse=old)
            self.assertEqual(summary['reused_groups'], 0)
            self.assertEqual(len(service.calls), 3)


if __name__ == '__main__':
    unittest.main()
