"""Offline, source-preserving content-unit regression tests."""
import copy
import unittest

import clean_boundary_v41 as legacy
import clean_compare1000 as c
import content_units_v46 as v


C149 = (
    "Such sentences do not appear to be truth-evaluable - 'Open the door, Smith!' "
    "or 'Where's the butter?' do not look like the kinds of sentence that can be "
    "true or false."
)
C149_NEXT = (
    "Yet if they lack truth-conditions, it seems puzzling how they can be "
    "accommodated within a truth-theoretic framework."
)
NOTES = (
    '\uff08\u4eba\u6c11\u7f512020\u5e749\u670812\u65e5\uff09',
    '\uff08\u4eba\u6c11\u7f51\uff0c2020-09-12\uff09',
    '(p.198)',
    '(pp. 198-200)',
    '\uff08\u9c81\u8fc5\u300a\u5450\u558a\u300b\uff09',
)


def vote(*ranges):
    return {'ranges': [
        {'first': first, 'last': last, 'language': language}
        for first, last, language in ranges
    ], 'exclude_units': []}


class ContentUnitsTests(unittest.TestCase):
    def assert_units(self, raw, expected, layouts=frozenset()):
        actual = v.units(raw, layouts)
        self.assertEqual([u['text'].strip() for u in actual], expected)
        previous_end = 0
        for index, unit in enumerate(actual):
            self.assertEqual(set(unit), {'unit', 'start', 'end', 'text', 'layout'})
            self.assertEqual(unit['unit'], index)
            self.assertGreaterEqual(unit['start'], previous_end)
            self.assertFalse(raw[previous_end:unit['start']].strip())
            self.assertEqual(unit['text'], raw[unit['start']:unit['end']])
            self.assertGreater(unit['end'], unit['start'])
            previous_end = unit['end']
        self.assertFalse(raw[previous_end:].strip())
        return actual

    def test_c149_real_sentence_is_not_cut_at_quoted_exclamation(self):
        self.assert_units(C149 + ' ' + C149_NEXT, [C149, C149_NEXT])

    def test_open_quotes_protect_internal_terminal_punctuation(self):
        for first, last in [('"', '"'), ("'", "'"), ('\u201c', '\u201d'),
                            ('\u2018', '\u2019'), ('\u300c', '\u300d'),
                            ('\u300e', '\u300f')]:
            with self.subTest(quote=first):
                sentence = 'He said ' + first + 'Stop! Wait? Go.' + last
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_closed_sentence_end_quotes_allow_normal_boundary(self):
        for sentence in ['He said "Go!"', "He said 'Go!'", 'He said \u201cGo!\u201d']:
            with self.subTest(sentence=sentence):
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_quote_followed_by_or_and_or_lowercase_is_continuation(self):
        for continuation in ['or "Stay?" is possible.', 'and "Stay?" is possible.',
                             'Or "Stay?" is possible.', 'And "Stay?" is possible.',
                             'is only an example.']:
            with self.subTest(continuation=continuation):
                sentence = 'The command "Go!" ' + continuation
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_quote_continuation_survives_newline(self):
        sentence = "The command 'Go!'\n or 'Stay?' is only an example."
        self.assert_units(sentence + '\nNext sentence.', [sentence, 'Next sentence.'])

    def test_apostrophes_in_contractions_and_names_are_not_quotes(self):
        for sentence in ["Don't stop.", "Where's the butter?", "O'Brien doesn't stop.",
                         'Don\u2019t stop.', "The speakers' words matter."]:
            with self.subTest(sentence=sentence):
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_apostrophe_inside_single_quoted_passage_does_not_close_it(self):
        for first, last in [("'", "'"), ('\u2018', '\u2019')]:
            with self.subTest(quote=first):
                sentence = 'He asked ' + first + "Where's the butter? Wait!" + last
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_nested_quotes_preserve_the_outer_sentence(self):
        sentence = 'He said "The word \'Stop!\' is a command."'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_escaped_quote_does_not_close_the_outer_quote(self):
        sentence = 'He said "Use \\"Stop!\\" carefully."'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_unclosed_quote_does_not_create_a_new_outer_sentence(self):
        raw = 'He said "Stop! Next sentence.'
        self.assert_units(raw, [raw])

    def test_parenthesis_internal_punctuation_does_not_split_outer_sentence(self):
        for first, last in [('(', ')'), ('\uff08', '\uff09'), ('[', ']'),
                            ('\u3010', '\u3011')]:
            with self.subTest(bracket=first):
                sentence = 'A statement ' + first + 'Really! Why? Yes.' + last + ' remains true.'
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_closed_parenthesis_internal_terminal_is_not_an_outer_terminal(self):
        sentence = 'The claim (Really!) remains true.'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_sentence_initial_parenthesis_followed_by_continuation_does_not_split(self):
        sentence = '(Really!) this remains true.'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_plural_possessive_inside_single_quote_does_not_close_quote(self):
        sentence = "He said 'The speakers' words! Don't stop.'"
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_quoted_word_ending_in_s_does_not_absorb_a_later_quote(self):
        self.assert_units("The word 'dogs' is plural. He said 'Go!'",
                          ["The word 'dogs' is plural.", "He said 'Go!'"])

    def test_nested_brackets_and_quotes_preserve_the_outer_sentence(self):
        sentence = 'The claim (see [the command "Go!"]) remains true.'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_chinese_parentheses_and_quotes_protect_internal_punctuation(self):
        sentence = '\u8fd9\u662f\uff08\u771f\u7684\uff01\u662f\u5417\uff1f\uff09\u6b63\u6587\u3002'
        next_sentence = '\u4e0b\u4e00\u53e5\u3002'
        self.assert_units(sentence + next_sentence, [sentence, next_sentence])

    def test_dictionary_abbreviations_and_initials_are_preserved(self):
        for sentence in ['Dr. Smith studied Sk. and Tibetan Bsm. in India.',
                         'Mr. J. Smith used e.g. the U.S. example.',
                         'This is vol. 2, pp. 198-200.']:
            with self.subTest(sentence=sentence):
                self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_dictionary_abbreviation_at_real_sentence_end_still_splits(self):
        self.assert_units('He studied Pali Bsm. Then studied Sk. and Tibetan Bsm. in India.',
                          ['He studied Pali Bsm.', 'Then studied Sk. and Tibetan Bsm. in India.'])

    def test_plain_lowercase_next_sentence_keeps_legacy_boundary(self):
        self.assert_units('This is noise. paper money backed by a decree.',
                          ['This is noise.', 'paper money backed by a decree.'])

    def test_ellipsis_is_not_a_sentence_boundary(self):
        self.assert_units('He paused... then spoke. Next sentence.',
                          ['He paused... then spoke.', 'Next sentence.'])

    def test_empty_and_whitespace_inputs_have_no_units(self):
        for raw in ['', ' \t\r\n']:
            with self.subTest(raw=raw):
                self.assert_units(raw, [])

    def test_offsets_preserve_crlf_and_unicode_exactly(self):
        raw = ' \t' + C149 + '\r\n(p.198)\r\n' + C149_NEXT + '  '
        self.assert_units(raw, [C149, '(p.198)', C149_NEXT])

    def test_high_confidence_standalone_sources_are_nonlayout_units(self):
        for note in NOTES:
            with self.subTest(note=note):
                actual = self.assert_units('First sentence.\n' + note + '\nNext sentence.',
                                           ['First sentence.', note, 'Next sentence.'])
                self.assertFalse(any(u['layout'] for u in actual))

    def test_inline_post_sentence_source_is_an_independent_unit(self):
        self.assert_units('First sentence. (p.198) Next sentence.',
                          ['First sentence.', '(p.198)', 'Next sentence.'])

    def test_embedded_page_reference_is_not_an_isolated_source(self):
        sentence = 'The discussion (p. 198) continues here.'
        self.assert_units(sentence + ' Next sentence.', [sentence, 'Next sentence.'])

    def test_general_bracket_explanation_and_name_are_not_layout(self):
        raw = 'First sentence.\n\uff08\u8865\u5145\u8bf4\u660e\uff09'
        self.assertFalse(any(u['layout'] for u in v.units(raw)))
        self.assertFalse(any(u['layout'] for u in v.units('\uff08\u5f20\u4e09\uff09')))

    def test_layout_is_only_the_callers_explicit_whole_line_evidence(self):
        raw = 'First sentence.\r\n Smith \r\nNext sentence.'
        actual = self.assert_units(raw, ['First sentence.', 'Smith', 'Next sentence.'], {'Smith'})
        self.assertEqual([u['layout'] for u in actual], [False, True, False])
        self.assertFalse(any(u['layout'] for u in v.units('Smith is an author.')))
        self.assertFalse(any(u['layout'] for u in v.units('## A heading')))


class ProjectionTests(unittest.TestCase):
    def project(self, raw, selection, layouts=frozenset()):
        mapped = c.s.MappedText.from_raw(raw, 100)
        before = (mapped.text, mapped.positions[:], copy.deepcopy(selection))
        result, edits = v.project(mapped, selection, layouts)
        self.assertEqual((mapped.text, mapped.positions, selection), before)
        for output in result.values():
            for char, position in zip(output.text, output.positions):
                if position is not None:
                    self.assertEqual(char, raw[position - 100])
        return result, edits

    def test_c149_projection_keeps_the_whole_original_sentence(self):
        result, edits = self.project(C149 + ' ' + C149_NEXT, vote((0, 0, 'en')))
        self.assertEqual(result['en'].text, C149)
        self.assertEqual(result['en'].positions, list(range(100, 100 + len(C149))))
        self.assertEqual(edits, [])

    def test_source_retained_with_selected_previous_body(self):
        for note in NOTES:
            with self.subTest(note=note):
                raw = 'First sentence.\n' + note + '\nNext sentence.'
                result, edits = self.project(raw, vote((0, 1, 'en')))
                self.assertEqual(result['en'].text, 'First sentence.\n' + note)
                self.assertEqual(edits, [])

    def test_orphan_source_is_removed_without_blocking_next_body(self):
        for note in NOTES:
            with self.subTest(note=note):
                result, edits = self.project('First sentence.\n' + note + '\nNext sentence.',
                                             vote((1, 2, 'en')))
                self.assertEqual(result['en'].text, 'Next sentence.')
                self.assertEqual([(e['unit'], e['reason']) for e in edits],
                                 [(1, 'orphan_source_note')])

    def test_source_only_selection_without_parent_returns_no_language(self):
        result, edits = self.project('First sentence.\n(p.198)', vote((1, 1, 'en')))
        self.assertEqual(result, {})
        self.assertEqual(edits[0]['reason'], 'orphan_source_note')

    def test_leading_source_has_no_parent(self):
        result, edits = self.project('(p.198)\nFirst sentence.', vote((0, 1, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.')
        self.assertEqual(edits[0]['reason'], 'orphan_source_note')

    def test_source_can_attach_across_adjacent_selected_ranges(self):
        result, edits = self.project('First sentence.\n(p.198)',
                                     vote((0, 0, 'en'), (1, 1, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.\n(p.198)')
        self.assertEqual(edits, [])

    def test_source_does_not_attach_to_an_earlier_selected_nonparent(self):
        raw = 'First sentence. Second sentence.\n(p.198)'
        result, edits = self.project(raw, vote((0, 0, 'en'), (2, 2, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.')
        self.assertEqual(edits[0]['reason'], 'orphan_source_note')

    def test_source_requires_actual_retained_parent_not_just_a_vote(self):
        result, edits = self.project('without the missing start.\n(p.198)',
                                     vote((0, 0, 'en'), (1, 1, 'en')))
        self.assertEqual(result, {})
        self.assertEqual([e['reason'] for e in edits], ['incomplete_sentence', 'orphan_source_note'])

    def test_source_cannot_attach_to_body_in_another_language(self):
        result, edits = self.project('First sentence.\n(p.198)',
                                     vote((0, 0, 'zh'), (1, 1, 'en')))
        self.assertEqual(set(result), {'zh'})
        self.assertEqual(edits[0]['reason'], 'orphan_source_note')

    def test_multiple_source_notes_share_previous_content_parent(self):
        raw = 'First sentence.\n(p.198)\n' + NOTES[-1]
        result, edits = self.project(raw, vote((0, 2, 'en')))
        self.assertEqual(result['en'].text, raw)
        self.assertEqual(edits, [])

    def test_unselected_source_is_not_added_implicitly(self):
        result, edits = self.project('First sentence.\n(p.198)', vote((0, 0, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.')
        self.assertEqual(edits, [])

    def test_general_parenthetical_explanations_are_not_removed_as_sources(self):
        for sentence in ['The claim (not a source!) remains true.',
                         '(This is an ordinary explanation.)',
                         '(Reading a book explains this.)',
                         '\uff08\u8fd9\u662f\u4e00\u822c\u89e3\u91ca\u3002\uff09']:
            with self.subTest(sentence=sentence):
                result, edits = self.project(sentence, vote((0, 0, 'en')))
                self.assertEqual(result['en'].text, sentence)
                self.assertEqual(edits, [])

    def test_unclosed_quote_is_not_projected_as_complete(self):
        result, edits = self.project('He said "Stop! Next sentence.', vote((0, 0, 'en')))
        self.assertEqual(result, {})
        self.assertEqual(edits[0]['reason'], 'incomplete_sentence')

    def test_layout_removal_does_not_delete_person_names_in_body(self):
        raw = 'Smith is an author.\nSmith\nNext sentence.'
        result, edits = self.project(raw, vote((0, 2, 'en')), {'Smith'})
        self.assertEqual(result['en'].text, 'Smith is an author.\n\nNext sentence.')
        self.assertEqual([(e['unit'], e['reason']) for e in edits], [(1, 'layout')])

    def test_explicit_layout_overrides_source_note_retention(self):
        raw = 'First sentence.\n(p.198)'
        result, edits = self.project(raw, vote((0, 1, 'en')), {'(p.198)'})
        self.assertEqual(result['en'].text, 'First sentence.')
        self.assertEqual(edits[0]['reason'], 'layout')

    def test_incomplete_interior_keeps_prefix_without_bridging(self):
        raw = 'First sentence. without the missing start. Last sentence.'
        result, edits = self.project(raw, vote((0, 2, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.')
        self.assertEqual([e['reason'] for e in edits],
                         ['incomplete_sentence', 'avoid_bridge_after_incomplete'])

    def test_separate_ranges_preserve_mapping_and_legacy_joiners(self):
        raw = 'First sentence. Omitted sentence. Last sentence.'
        result, edits = self.project(raw, vote((0, 0, 'en'), (2, 2, 'en')))
        self.assertEqual(result['en'].text, 'First sentence.\n\nLast sentence.')
        spans, joins = c.s.source_trace(result['en'])
        self.assertEqual(c.s.render_trace(' ' * 100 + raw, spans, joins), result['en'].text)
        self.assertEqual(edits, [])

    def test_empty_selection_returns_empty_result(self):
        self.assertEqual(self.project('First sentence.', vote()), ({}, []))

    def test_invalid_votes_are_rejected(self):
        for invalid in [None, {}, {'ranges': [], 'exclude_units': [0]},
                        {'ranges': 'all', 'exclude_units': []}, vote((True, 0, 'en')),
                        vote((0, 9, 'en')), vote((0, 0, 'fr')),
                        vote((0, 0, 'en'), (0, 0, 'en')),
                        {'ranges': [None], 'exclude_units': []}]:
            with self.subTest(vote=invalid), self.assertRaises(ValueError):
                v.project(c.s.MappedText.from_raw('First sentence.', 0), invalid)

    def test_legacy_units_and_project_remain_unchanged(self):
        original_units = legacy.units
        original_project = legacy.project
        before = legacy.units(C149)
        self.project(C149, vote((0, 0, 'en')))
        self.assertIs(legacy.units, original_units)
        self.assertIs(legacy.project, original_project)
        self.assertEqual(legacy.units(C149), before)
        result, _ = legacy.project(c.s.MappedText.from_raw(C149, 0), vote((0, 0, 'en')))
        self.assertEqual(result['en'].text,
                         "Such sentences do not appear to be truth-evaluable - 'Open the door, Smith!'")


if __name__ == '__main__':
    unittest.main()
