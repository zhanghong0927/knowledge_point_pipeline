import json
import unittest

from test_dictionary_md_audit import AUDIT, add_knowledge_value_labels, base_result


def qualitative_result(initial='PASS'):
    result = add_knowledge_value_labels(base_result(), discipline=1, nonknowledge=30)
    result.update(
        dictionary_structure='clear', subject_relevance='high',
        entry_definition_alignment='high', continuous_prose_dominant=False,
        decision=initial,
        knowledge_value_assessment={
            'usability': 'high', 'dominant_content': 'disciplinary_entries',
            'examples': [
                {'sample_no': 1, 'headword': 'Painting', 'anchor': 'Paint is applied'},
                {'sample_no': 2, 'headword': 'Sculpture', 'anchor': 'Three dimensional art'},
            ],
        },
    )
    for flag in result['sample_knowledge_value_flags']:
        flag.update(content_role='body', issue_basis='none')
    return result


class QualitativePolicyTests(unittest.TestCase):
    def parse(self, result, **kwargs):
        return AUDIT.parse_json_content(json.dumps(result), expected_sample_count=12,
                                        knowledge_value_mode=True, **kwargs)

    def test_counts_do_not_downgrade_useful_entries(self):
        result = self.parse(qualitative_result(), max_nonknowledge_headword_ratio=0.0,
                            drop_nonknowledge_headword_ratio=0.0)
        self.assertEqual(result['decision'], 'PASS')

    def test_low_evidence_does_not_turn_review_into_drop(self):
        result = qualitative_result('REVIEW')
        result['sample_headword_type_counts'][0]['general_language'] = 2
        result['knowledge_value_assessment']['usability'] = 'unclear'
        self.assertEqual(self.parse(result)['decision'], 'REVIEW')

    def test_general_examination_practice_remains_unusable(self):
        result = qualitative_result()
        result['knowledge_value_assessment'].update(
            usability='low', dominant_content='general_language_practice', examples=[])
        result['problems'] = [{'sample_no': 1, 'anchor': 'remember this word',
                              'problem_type': '学科不相关', 'reason': 'Only general vocabulary drills',
                              'severity': 'major', 'rule_cleanable': False}]
        self.assertEqual(self.parse(result)['decision'], 'DROP')

    def test_author_biography_does_not_count_as_body_damage(self):
        result = qualitative_result()
        for index in (9, 10, 11):
            result['sample_structure_labels'][index]['structure'] = 'long_article'
            result['sample_knowledge_value_flags'][index].update(
                content_role='non_body', entry_extraction_status='unusable', issue_basis='non_entry_content')
        self.assertEqual(self.parse(result)['decision'], 'PASS')

    def test_cut_mid_entry_does_not_prove_source_damage(self):
        result = qualitative_result()
        for index in range(6):
            result['sample_structure_labels'][index]['structure'] = 'long_article'
            result['sample_knowledge_value_flags'][index].update(
                entry_extraction_status='unusable', issue_basis='sample_truncation')
        self.assertEqual(self.parse(result)['decision'], 'PASS')

    def test_real_multilingual_entry_still_drops(self):
        result = qualitative_result()
        result['sample_knowledge_value_flags'][1]['languages'] = ['en', 'de']
        self.assertEqual(self.parse(result)['decision'], 'DROP')

    def test_unverified_anchor_is_not_counted_as_evidence(self):
        result = qualitative_result()
        samples = [{'sample_no': i, 'text': 'Painting. Paint is applied.' if i == 1
                    else 'Sculpture. This is a different definition.'} for i in range(1, 13)]
        parsed = self.parse(result, samples=samples)
        self.assertEqual(parsed['decision'], 'REVIEW')
        self.assertEqual(parsed['knowledge_value_evidence_count'], 1)
        self.assertEqual(len(parsed['unverified_knowledge_examples']), 1)

    def test_non_body_example_is_rejected_without_failing_the_book(self):
        result = qualitative_result()
        result['sample_knowledge_value_flags'][1]['content_role'] = 'non_body'
        parsed = self.parse(result)
        self.assertEqual(parsed['decision'], 'REVIEW')
        self.assertEqual(parsed['knowledge_value_evidence_count'], 1)
        self.assertEqual(parsed['unverified_knowledge_examples'][0]['rejection_reason'], 'non_body_sample')

    def test_prompt_removes_fixed_ratios_and_type_whitelist(self):
        prompt = AUDIT.build_system_prompt(knowledge_value_mode=True)
        self.assertNotIn('15%', prompt)
        self.assertNotIn('50%', prompt)
        self.assertNotIn('默认只有 discipline_concept', prompt)
        self.assertIn('knowledge_value_assessment', prompt)

    def test_sampler_skips_author_tail_and_preserves_context(self):
        body = '\n\n'.join(f'## Term {i}\n' + 'Definition and explanation. ' * 100 for i in range(20))
        text = body + '\n\n## About the Author and Illustrator\n' + 'Author biography. ' * 300
        samples = AUDIT.hybrid_entry_samples(text, sample_count=16, chunk_chars=500)
        self.assertEqual(len(samples), 16)
        self.assertTrue(all('Author biography' not in s['text'] for s in samples))
        self.assertTrue(any(s.get('context_before') for s in samples))


if __name__ == '__main__':
    unittest.main()
