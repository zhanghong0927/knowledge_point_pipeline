"""Boundary policy keeps unsupported exclusions empty and ambiguity nonblocking."""

import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import tempfile

NATIVE = Path(__file__).resolve().parents[1] / 'modules/mounting/pipeline'
sys.path.insert(0, str(NATIVE))
boundary = importlib.import_module('generate_semantic_boundaries')


def card(code):
    return dict(node_code=code, definition='Objects of ' + code,
                boundary='Scope of ' + code, includes=['Objects'],
                excludes=['Unrelated objects'], sibling_distinctions=[],
                cross_boundary_rule='Use the object and application context.')


def payload():
    tree = boundary.normalize(dict(code='p', name='Parent', children=[
        dict(code='c', name='Child')]))
    parent = boundary.validate_cards({'cards': [card('p')]}, [tree[0]])[0]
    child = boundary.validate_cards({'cards': [card('c')]}, [tree[1]])[0]
    return boundary.single_cross_payload(tree, 'c', {'p': parent, 'c': child})


def finding():
    return dict(ancestor_code='p', descendant_code='c',
                ancestor_field='boundary', ancestor_quote='Scope of p',
                descendant_field='boundary', descendant_quote='Scope of c',
                reason='The wording is ambiguous, not a demonstrated contradiction.')


def self_checks(verdict='pass'):
    return [dict(node_code='c', verdict=verdict, reason='Check the card itself.')]


class BoundaryPolicyTests(unittest.TestCase):
    def test_empty_exclusions_do_not_force_invented_scope_limits(self):
        value = card('c')
        value['excludes'] = []
        result = boundary.validate_cards({'cards': [value]}, [dict(code='c', path='P/C', name_zh='C', depth=1)])
        self.assertEqual(result[0]['excludes'], [])
        self.assertNotIn('\u6392\u9664\uff1a', result[0]['semantic_card'])

    def test_empty_cross_rule_is_valid_when_no_cross_case_is_known(self):
        value = card('c')
        value['cross_boundary_rule'] = ''
        result = boundary.validate_cards({'cards': [value]}, [dict(code='c', path='P/C', name_zh='C', depth=1)])
        self.assertEqual(result[0]['cross_boundary_rule'], '')
        self.assertNotIn('\u4e3b\u6302\u8f7d\uff1a', result[0]['semantic_card'])

    def test_null_exclusions_are_still_invalid(self):
        value = card('c')
        value['excludes'] = None
        with self.assertRaisesRegex(ValueError, 'invalid excludes'):
            boundary.validate_cards({'cards': [value]}, [dict(code='c', path='P/C', name_zh='C', depth=1)])

    def test_unsupported_warning_quote_is_not_accepted_as_evidence(self):
        warning = finding()
        warning['ancestor_quote'] = 'Not in the original'
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], self_issues=[], warnings=[warning])
        with self.assertRaisesRegex(ValueError, 'quote not in original card'):
            boundary.validate_cross_review(obj, payload())

    def test_warning_references_must_use_real_ancestor_and_descendant(self):
        warning = finding()
        warning['ancestor_code'] = 'invented'
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], self_issues=[], warnings=[warning])
        with self.assertRaisesRegex(ValueError, 'invalid ancestor-descendant pair'):
            boundary.validate_cross_review(obj, payload())

    def test_warning_container_must_be_an_array(self):
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], self_issues=[], warnings='ambiguous')
        with self.assertRaisesRegex(ValueError, 'cross warnings'):
            boundary.validate_cross_review(obj, payload())

    def test_evidenced_warning_preserves_pass_and_full_ancestor_coverage(self):
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], warnings=[finding()],
                   ancestor_checks=[dict(ancestor_code='p', verdict='pass', reason='No explicit conflict.')],
                   self_checks=self_checks(), self_issues=[])
        result = boundary.validate_pair_review(obj, payload())
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(result['warnings'], [finding()])

    def test_blocking_conflict_cannot_be_hidden_in_pass(self):
        obj = dict(verdict='pass', checked_codes=['c'], issues=[finding()], self_issues=[], warnings=[])
        with self.assertRaisesRegex(ValueError, 'cross issues/verdict mismatch'):
            boundary.validate_cross_review(obj, payload())

    def test_schema_two_requires_card_self_check_not_only_ancestor_checks(self):
        data = dict(payload(), review_schema=2)
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], self_issues=[], warnings=[],
                   ancestor_checks=[dict(ancestor_code='p', verdict='pass', reason='Compatible.')])
        with self.assertRaisesRegex(ValueError, 'self check coverage'):
            boundary.validate_pair_review(obj, data)

    def test_parent_child_inclusion_overlap_cannot_be_a_blocker(self):
        data = dict(payload(), review_schema=2)
        issue = dict(finding(), conflict_type='ancestor_restriction',
                     ancestor_field='includes', ancestor_quote='Objects',
                     descendant_field='includes', descendant_quote='Objects')
        obj = dict(verdict='needs_revision', checked_codes=['c'], issues=[issue], warnings=[],
                   self_checks=self_checks(), self_issues=[])
        with self.assertRaisesRegex(ValueError, 'not a restrictive field'):
            boundary.validate_cross_review(obj, data)

    def test_parent_include_child_exclude_is_normal_not_a_blocker(self):
        data = dict(payload(), review_schema=2)
        issue = dict(finding(), conflict_type='ancestor_restriction',
                     ancestor_field='includes', ancestor_quote='Objects',
                     descendant_field='excludes', descendant_quote='Unrelated objects')
        obj = dict(verdict='needs_revision', checked_codes=['c'], issues=[issue], warnings=[],
                   self_checks=self_checks(), self_issues=[])
        with self.assertRaisesRegex(ValueError, 'not a restrictive field'):
            boundary.validate_cross_review(obj, data)

    def test_self_conflict_does_not_falsely_require_rewriting_an_ancestor(self):
        data = dict(payload(), review_schema=2)
        issue = dict(node_code='c', field_a='includes', quote_a='Objects',
                     field_b='excludes', quote_b='Objects',
                     reason='A contradiction in the child card itself.')
        data['cards'][0]['excludes'] = ['Objects']
        obj = dict(verdict='needs_revision', checked_codes=['c'], issues=[], warnings=[],
                   self_issues=[issue], self_checks=self_checks('needs_revision'),
                   ancestor_checks=[dict(ancestor_code='p', verdict='pass', reason='Compatible.')])
        self.assertEqual(boundary.validate_pair_review(obj, data)['self_issues'], [issue])

    def test_self_conflict_requires_exact_original_evidence(self):
        data = dict(payload(), review_schema=2)
        issue = dict(node_code='c', field_a='includes', quote_a='Invented quote',
                     field_b='excludes', quote_b='Unrelated objects', reason='Unsupported.')
        obj = dict(verdict='needs_revision', checked_codes=['c'], issues=[], warnings=[],
                   self_issues=[issue], self_checks=self_checks('needs_revision'))
        with self.assertRaisesRegex(ValueError, 'self quote not in original card'):
            boundary.validate_cross_review(obj, data)

    def test_schema_two_requires_explicit_self_issues_array(self):
        obj = dict(verdict='pass', checked_codes=['c'], issues=[], warnings=[],
                   self_checks=self_checks(),
                   ancestor_checks=[dict(ancestor_code='p', verdict='pass', reason='Compatible.')])
        with self.assertRaisesRegex(ValueError, 'missing self issues'):
            boundary.validate_pair_review(obj, payload())

    def test_root_card_receives_self_review_even_without_ancestors(self):
        tree = boundary.normalize(dict(code='p', name='Parent'))
        parent = boundary.validate_cards({'cards': [card('p')]}, tree)[0]
        seen = []

        def fake_call(base, model, prompt, data, validator):
            seen.append(data)
            obj = dict(verdict='pass', checked_codes=['p'], issues=[], self_issues=[], warnings=[],
                       ancestor_checks=[], self_checks=[dict(node_code='p', verdict='pass',
                                                            reason='Internally consistent.')])
            return dict(result=validator(obj), attempts=[])

        with tempfile.TemporaryDirectory() as directory, patch.object(boundary, 'call', fake_call):
            codes, reviews = boundary.cross_audit(tree, {'p': parent}, 'unused', 'unused', 1,
                                                  10000, Path(directory))
        self.assertEqual(codes, ['p'])
        self.assertEqual(seen[0]['ancestors'], [])
        self.assertEqual(reviews[0]['result']['self_checks'][0]['node_code'], 'p')


if __name__ == '__main__':
    unittest.main()
