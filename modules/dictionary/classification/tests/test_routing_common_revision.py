import unittest

from classification_routing import project
from test_classification_routing import sample


class CommonRevision(unittest.TestCase):
    def test_plain_complete_head_does_not_depend_on_window_label(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            window['lines'][0]['text'] = record['entries'][0]['head']['quote']
            record['md_position'] = 'both'
        self.assertEqual(project(value, windows)['head_position'], 'standalone')

    def test_markdown_does_not_hide_unresolved_inline_text(self):
        value, windows = sample()
        for window in windows:
            window['lines'][0]['text'] += ' Born in 1931. An author.'
        result = project(value, windows)
        self.assertEqual(result['head_position'], 'unknown')
        self.assertEqual(result['routing_status'], 'review')

    def test_markdown_inline_with_actual_body_anchor(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            window['lines'][0]['text'] += ' Definition and discussion.'
            record['entries'][0]['body']['line_id'] = window['lines'][0]['id']
        self.assertEqual(project(value, windows)['head_position'], 'inline')

    def test_markdown_redaction_is_not_main_head(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            window['lines'][0]['text'] = '## [XXXXXXXX]'
            record['entries'][0]['head']['quote'] = '## [XXXXXXXX]'
        result = project(value, windows)
        self.assertEqual(result['support'], [])
        self.assertEqual(result['routing_status'], 'review')

    def test_supplement_excluded_even_when_region_says_entry_body(self):
        value, windows = sample()
        for record in value['windows']:
            record['body_scope'] = 'supplement'
        result = project(value, windows, require_scope=True)
        self.assertEqual(result['support'], [])

    def test_unknown_scope_cannot_count_as_primary(self):
        value, windows = sample()
        for record in value['windows']:
            record['body_scope'] = 'unknown'
        self.assertEqual(project(value, windows, require_scope=True)['routing_status'], 'review')

    def test_missing_scope_in_new_run_is_schema_error(self):
        value, windows = sample()
        with self.assertRaisesRegex(ValueError, 'body_scope'):
            project(value, windows, require_scope=True)

    def test_o4_without_commentary_cannot_pass_strict_validation(self):
        value, windows = sample()
        for record in value['windows']:
            record['organization'] = 'O4'
        result = project(value, windows, require_commentary=True)
        self.assertEqual(result['routing_status'], 'review')
        self.assertEqual(result['support'], [])

    def test_o4_separate_commentary_anchor_can_pass(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            record['organization'] = 'O4'
            anchor = {'line_id': window['lines'][1]['id'] + '-comment',
                      'quote': 'The image contrasts silence with movement.'}
            record['entries'][0]['commentary'] = anchor
            window['lines'].append({'id': anchor['line_id'], 'text': anchor['quote']})
        self.assertEqual(project(value, windows, require_commentary=True)['routing_status'], 'sample_supported')

    def test_commentary_cannot_repeat_original_or_head(self):
        value, windows = sample()
        for record in value['windows']:
            record['organization'] = 'O4'
            record['entries'][0]['commentary'] = dict(record['entries'][0]['body'])
        self.assertEqual(project(value, windows, require_commentary=True)['routing_status'], 'review')

    def test_commentary_cannot_repeat_original_on_another_line(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            record['organization'] = 'O4'
            anchor = dict(record['entries'][0]['body'])
            anchor['line_id'] += '-copy'
            record['entries'][0]['commentary'] = anchor
            window['lines'].append({'id': anchor['line_id'], 'text': anchor['quote']})
        self.assertEqual(project(value, windows, require_commentary=True)['routing_status'], 'review')

    def test_unknown_scope_not_removed_from_other_denominator(self):
        import copy
        value, windows = sample()
        for record in value['windows']:
            record['body_scope'] = 'primary'
            record['boundary_contract'] = {'status': 'unsupported', 'reason': 'External relations.',
                                           'evidence': [record['entries'][0]['head']]}
        value['applicability'] = {'decision': 'other', 'reason': 'External relations.',
            'evidence': [{'window_id': r['window_id'], **r['entries'][0]['head']} for r in value['windows']]}
        for record, window in list(zip(value['windows'], windows)):
            r, w = copy.deepcopy(record), copy.deepcopy(window)
            r['window_id'] = w['window_id'] = 'unknown-' + r['window_id']
            r['body_scope'] = 'unknown'
            r['boundary_contract']['status'] = 'supported'
            value['windows'].append(r)
            windows.append(w)
        result = project(value, windows, require_boundary=True, require_scope=True)
        self.assertEqual(result['routing_status'], 'review')

    def test_long_complete_head_still_standalone(self):
        value, windows = sample()
        for record, window in zip(value['windows'], windows):
            head = 'A long but complete descriptive entry name ' * 5
            record['entries'][0]['head']['quote'] = head.strip()
            window['lines'][0]['text'] = '## ' + head.strip()
        self.assertEqual(project(value, windows)['head_position'], 'standalone')

    def test_partial_head_with_unresolved_suffix_is_not_assumed_standalone(self):
        value, windows = sample()
        for window in windows:
            window['lines'][0]['text'] += ' unquoted suffix'
        self.assertEqual(project(value, windows)['head_position'], 'unknown')


if __name__ == '__main__':
    unittest.main()
