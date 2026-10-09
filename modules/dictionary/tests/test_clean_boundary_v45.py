import unittest

import clean_boundary_v45 as v45


def row(head, head_line, before='', body='Example text.', line=10):
    return {
        'id': 'test', 'head': head, 'raw_content': body,
        'source': {'head_line': line},
        'source_context': {'head_context': [
            {'line': line - 2, 'text': before},
            {'line': line, 'text': head_line},
            {'line': line + 2, 'text': body},
        ]},
    }


class BoundaryV45Tests(unittest.TestCase):
    def test_drop_does_not_require_definition_evidence(self):
        vote = {'record_decision': 'drop', 'fields': {
            'definition': 'drop', 'en_definition': 'drop',
            'description': 'drop', 'en_description': 'drop',
        }}
        packet = {'text_fields': {'definition': 'A disputed definition.'}}
        result, edits = v45.alignment_vote(vote, packet)
        self.assertEqual(result, vote)
        self.assertEqual(edits, [])

    def test_invalid_definition_evidence_clears_only_definition(self):
        vote = {'record_decision': 'keep', 'fields': {
            'definition': 'keep', 'en_definition': 'drop',
            'description': 'keep', 'en_description': 'drop',
        }, 'definition_checks': {}}
        packet = {'text_fields': {
            'definition': 'An exact sentence.', 'description': 'An exact sentence.',
        }}
        result, edits = v45.alignment_vote(vote, packet)
        self.assertEqual(result['fields']['definition'], 'drop')
        self.assertEqual(result['fields']['description'], 'keep')
        self.assertEqual(len(edits), 1)

    def test_reject_numbered_passage(self):
        sample = row('XXXIII', 'XXXIII. Also, whereas the commons complain',
                     'XXXII. Also, whereas another matter', 'Also, whereas the commons complain')
        self.assertEqual(v45.structural_reject_reason(sample), 'numbered_source_passage')

    def test_reject_index_see_only(self):
        sample = row('NCPG', 'NCPG. See National Council on Problem Gaming',
                     'NCAAD. See National Council on Alcoholism',
                     'See National Council on Problem Gaming\n\nThe Need for FDA Regulation, 369-71')
        self.assertEqual(v45.structural_reject_reason(sample), 'index_see_only')

    def test_ambiguous_heading_is_not_auto_rejected(self):
        sample = row('Precedents', '## Precedents',
                     'A long prior discussion of civil liberties and political repression. ' * 3,
                     'The repression during World War I was not without some precedent.')
        self.assertIsNone(v45.structural_reject_reason(sample))
        self.assertEqual(v45.structural_review_reason(sample), 'possible_dependent_subheading')

    def test_keep_independent_heading_and_valid_see_entry(self):
        self.assertIsNone(v45.structural_reject_reason(
            row('Cognitive Learning', '## Cognitive Learning', '',
                'Cognitive Learning is a theory of education.')))
        self.assertIsNone(v45.structural_reject_reason(
            row('NCPG', 'NCPG. See National Council on Problem Gaming', '',
                'NCPG is a national nonprofit organization.')))


if __name__ == '__main__':
    unittest.main()
