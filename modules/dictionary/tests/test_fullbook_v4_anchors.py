import copy
import hashlib
import unittest

try:
    import fullbook_v4_anchors as anchors
except ModuleNotFoundError as exc:
    if exc.name != 'fullbook_v4_anchors':
        raise
    anchors = None


BOOK = {'identifier': 'test-book', 'title': 'Test',
        'md_path': 'test.md', 'md_sha256': 'abc'}
CHINESE = '\u8d1d\u5854'


def selection(unit, quote, **extra):
    return {'unit': unit, 'quote': quote, **extra}


def entry(unit, quote, **extra):
    return {'head': [selection(unit, quote)],
            'knowledge_point': [selection(unit, quote)], 'name': [],
            'body': [], 'body_complete': True, **extra}


def source(*blocks):
    rows = []
    offset = 0
    for unit, block in enumerate(blocks):
        kind, text = block if isinstance(block, tuple) else ('paragraph', block)
        rows.append({'unit': unit, 'offset': offset, 'text': text,
                     'kind': kind, 'line': 1 + sum(r['text'].count('\n') for r in rows)})
        offset += len(text)
    return ''.join(r['text'] for r in rows), {'lo': 0, 'hi': len(rows), 'units': rows}


class ModuleTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(anchors, 'The independent v4 anchor module is missing')

    def validate(self, item, text, part):
        return anchors.validate_entry(item, part, text, BOOK)


class AnchorsTests(ModuleTestCase):
    def test_repeated_head_uses_only_unique_line_prefix(self):
        text, part = source('Beta is defined; Beta is mentioned.\n')
        built = self.validate(entry(0, 'Beta'), text, part)
        self.assertEqual(built['source']['head_spans'], [(0, 4)])
        self.assertEqual(built['knowledge_point'], 'Beta')

    def test_repeated_midparagraph_head_is_not_guessed(self):
        text, part = source('We mention Beta, then Beta again.\n')
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta'), text, part)

    def test_two_line_prefix_heads_require_occurrence(self):
        text, part = source('Beta first.\nBeta second.\n')
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta'), text, part)
        item = entry(0, 'Beta', head=[selection(0, 'Beta', occurrence=1)])
        built = self.validate(item, text, part)
        self.assertEqual(built['source']['head_spans'], [(12, 16)])

    def test_markdown_prefix_is_head_position(self):
        text, part = source(('heading', '## **Beta** and later Beta\n'))
        self.assertEqual(self.validate(entry(0, 'Beta'), text, part)
                         ['source']['head_spans'], [(5, 9)])

    def test_chunk_start_inside_a_line_is_not_head_position(self):
        text, part = source('Earlier ', 'Beta and Beta.\n')
        part['units'][1]['line'] = 1
        with self.assertRaises(ValueError):
            self.validate(entry(1, 'Beta'), text, part)

    def test_clipped_packet_start_is_not_an_original_line_start(self):
        text, part = source('Earlier ', 'Beta and Beta.\n')
        part['units'] = part['units'][1:]
        part['lo'] = 1
        with self.assertRaises(ValueError):
            self.validate(entry(1, 'Beta'), text, part)

    def test_repeated_subword_at_prefix_is_not_a_headword(self):
        text, part = source('Betamax mentions Beta and Beta.\n')
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta'), text, part)

    def test_name_repetition_is_scoped_to_established_head(self):
        text, part = source('Beta (' + CHINESE + ') defines Beta and ' + CHINESE + '.\n')
        item = entry(0, 'Beta (' + CHINESE + ')',
                     knowledge_point=[selection(0, 'Beta')],
                     name=[selection(0, CHINESE)])
        built = self.validate(item, text, part)
        self.assertEqual(built['knowledge_point'], 'Beta')
        self.assertEqual(built['name'], CHINESE)

    def test_occurrence_is_global_before_head_scope_filtering(self):
        text, part = source('Beta mention.\nBeta (' + CHINESE + ')\n')
        item = entry(0, 'Beta (' + CHINESE + ')',
                     knowledge_point=[selection(0, 'Beta', occurrence=1)])
        self.assertEqual(self.validate(item, text, part)['knowledge_point'], 'Beta')
        item['knowledge_point'][0]['occurrence'] = 0
        with self.assertRaises(ValueError):
            self.validate(item, text, part)

    def test_multiple_names_inside_head_still_need_occurrence(self):
        text, part = source(('heading', '## Beta / Beta\n'))
        item = entry(0, 'Beta / Beta', knowledge_point=[selection(0, 'Beta')])
        with self.assertRaises(ValueError):
            self.validate(item, text, part)
        item['knowledge_point'][0]['occurrence'] = 1
        self.assertEqual(self.validate(item, text, part)['knowledge_point'], 'Beta')

    def test_language_field_cannot_borrow_from_body(self):
        text, part = source('Beta\n', 'Only ' + CHINESE + ' occurs here.\n')
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta', name=[selection(1, CHINESE)]), text, part)

    def test_repeated_body_start_needs_occurrence(self):
        text, part = source('Beta\n', 'echo first echo last.\n')
        item = entry(0, 'Beta', body=[{'start': selection(1, 'echo'),
                                      'end': selection(1, 'last.')}])
        with self.assertRaises(ValueError):
            self.validate(item, text, part)
        item['body'][0]['start']['occurrence'] = 1
        built = self.validate(item, text, part)
        self.assertEqual(built['raw_content'], 'echo last.')
        self.assertEqual(built['source']['body_spans'], [(16, 26)])

    def test_body_start_is_scoped_after_head_not_inside_head(self):
        text, part = source('Beta echo\necho final.\n')
        item = entry(0, 'Beta echo', body=[{'start': selection(0, 'echo'),
                                          'end': selection(0, 'final.')}])
        self.assertEqual(self.validate(item, text, part)['raw_content'], 'echo final.')
        item['body'][0]['start']['occurrence'] = 0
        with self.assertRaises(ValueError):
            self.validate(item, text, part)

    def test_repeated_body_end_needs_occurrence(self):
        text, part = source('Beta\n', 'Start end then end.\n')
        item = entry(0, 'Beta', body=[{'start': selection(1, 'Start'),
                                      'end': selection(1, 'end')}])
        with self.assertRaises(ValueError):
            self.validate(item, text, part)
        item['body'][0]['end']['occurrence'] = 1
        self.assertEqual(self.validate(item, text, part)['raw_content'], 'Start end then end')

    def test_end_order_does_not_disambiguate_repeated_quote(self):
        text, part = source('Beta\n', 'end Start end\n')
        item = entry(0, 'Beta', body=[{'start': selection(1, 'Start'),
                                      'end': selection(1, 'end')}])
        with self.assertRaises(ValueError):
            self.validate(item, text, part)

    def test_reversed_and_overlapping_body_ranges_rejected(self):
        text, part = source('Beta\n', 'First. Last.\n')
        for body in ([{'start': selection(1, 'Last.'), 'end': selection(1, 'First.')}],
                     [{'start': selection(1, 'First.'), 'end': selection(1, 'Last.')},
                      {'start': selection(1, 'Last.'), 'end': selection(1, 'Last.')}],
                     [{'start': selection(0, 'Beta'), 'end': selection(1, 'Last.')}]):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.validate(entry(0, 'Beta', body=body), text, part)

    def test_bilingual_head_across_lines_preserves_literal_spans(self):
        text, part = source(('heading', '## Beta\r\n'), CHINESE + '\r\n', 'Body.\r\n')
        item = entry(0, 'Beta', head=[selection(0, 'Beta'), selection(1, CHINESE)],
                     name=[selection(1, CHINESE)],
                     body=[{'start': selection(2, 'Body.'), 'end': selection(2, 'Body.')}])
        built = self.validate(item, text, part)
        self.assertEqual(built['head'], 'Beta ' + CHINESE)
        self.assertEqual(built['source']['head_spans'], [(3, 7), (9, 11)])
        self.assertEqual(built['source']['body_spans'], [(13, 18)])
        self.assertEqual(built['raw_content'], 'Body.')

    def test_output_equals_v1_for_unambiguous_entry(self):
        import fullbook_llm_extract as v1
        text, part = source('Beta\n', 'Definition.\n')
        item = entry(0, 'Beta', body=[{'start': selection(1, 'Definition.'),
                                      'end': selection(1, 'Definition.')}])
        expected = v1.validate({'scanned_all': True, 'entries': [item]}, part, text, BOOK)[0]
        self.assertEqual(self.validate(item, text, part), expected)

    def test_context_limits_and_position_based_id(self):
        text, part = source('x' * 750 + '\n', 'Beta\n', 'y' * 1400)
        built = self.validate(entry(1, 'Beta'), text, part)
        self.assertEqual(len(built['leading_context']), 600)
        self.assertEqual(len(built['trailing_context']), 1200)
        expected_id = hashlib.sha256(b'test-book:abc:751').hexdigest()[:24]
        self.assertEqual(built['id'], expected_id)
        self.assertIs(built['ready_for_delivery'], False)

    def test_offset_mismatch_is_rejected(self):
        text, part = source('Beta\n')
        part['units'][0]['offset'] = 1
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta'), text, part)

    def test_unicode_crlf_and_disjoint_body_are_exact_original_slices(self):
        text, part = source(('heading', '## Beta ' + CHINESE + '\r\n'),
                            '\U0001f600 First.\r\n', 'noise\r\n', 'Second.\r\n')
        item = entry(0, 'Beta ' + CHINESE, knowledge_point=[selection(0, 'Beta')],
                     name=[selection(0, CHINESE)], body=[
                         {'start': selection(1, '\U0001f600'), 'end': selection(1, 'First.')},
                         {'start': selection(3, 'Second.'), 'end': selection(3, 'Second.')}])
        built = self.validate(item, text, part)
        self.assertEqual(built['head'], 'Beta ' + CHINESE)
        self.assertEqual(built['raw_content'], '\U0001f600 First.\n\nSecond.')
        for start, end in built['source']['head_spans'] + built['source']['body_spans']:
            self.assertEqual(text[start:end], next(
                r['text'][start-r['offset']:end-r['offset']] for r in part['units']
                if r['offset'] <= start and end <= r['offset'] + len(r['text'])))

    def test_bad_schema_and_occurrence_values_rejected(self):
        text, part = source('Beta and Beta\n')
        for occurrence in (-1, 2, True, 0.0, '0', None):
            with self.subTest(occurrence=occurrence), self.assertRaises(ValueError):
                anchors.resolve_selection(selection(0, 'Beta', occurrence=occurrence), part)
        for chosen in (selection(True, 'Beta'), selection(0, ''), selection(0, 1),
                       selection(99, 'Beta'), selection(0, 'beta')):
            with self.subTest(chosen=chosen), self.assertRaises(ValueError):
                anchors.resolve_selection(chosen, part)

    def test_overlapping_literal_occurrences_are_counted(self):
        text, part = source('aaa\n')
        self.assertEqual(anchors.resolve_selection(selection(0, 'aa', occurrence=1), part), (1, 3))

    def test_missing_names_unordered_heads_and_nonboolean_complete_rejected(self):
        text, part = source('Beta\n', CHINESE + '\n')
        for item in (entry(0, 'Beta', knowledge_point=[]),
                     entry(0, 'Beta', head=[selection(1, CHINESE), selection(0, 'Beta')]),
                     entry(0, 'Beta', body_complete=1)):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.validate(item, text, part)

    def test_head_outside_ownership_rejected(self):
        text, part = source('Beta\n')
        part['lo'] = 1
        with self.assertRaises(ValueError):
            self.validate(entry(0, 'Beta'), text, part)

    def test_validation_does_not_mutate_source_or_selections(self):
        text, part = source('Beta and Beta\n')
        item = entry(0, 'Beta')
        old = copy.deepcopy((item, part))
        self.validate(item, text, part)
        self.assertEqual((item, part), old)


class RepairTests(ModuleTestCase):
    def test_unrelated_head_repair_rejected(self):
        _, part = source(('heading', '## Beta\n'), ('heading', '## Gamma\n'))
        self.assertFalse(anchors.repair_matches(entry(0, 'Beta'), entry(1, 'Gamma'), part))

    def test_no_fallback_when_original_quote_has_no_evidence(self):
        _, part = source(('heading', '## Gamma\n'))
        self.assertFalse(anchors.repair_matches(entry(0, 'Beta'), entry(0, 'Gamma'), part))

    def test_evidenced_bilingual_extension_accepted(self):
        _, part = source(('heading', '## Beta (' + CHINESE + ')\n'))
        original = entry(0, 'Beta')
        fixed = entry(0, 'Beta (' + CHINESE + ')',
                      knowledge_point=[selection(0, 'Beta')], name=[selection(0, CHINESE)])
        self.assertTrue(anchors.repair_matches(original, fixed, part))
        self.assertTrue(anchors.repair_matches(
            entry(0, CHINESE, knowledge_point=[], name=[selection(0, CHINESE)]), fixed, part))

    def test_evidenced_adjacent_crossline_bilingual_extension_accepted(self):
        _, part = source(('heading', '## Beta\n'), ('gap', '\n'), CHINESE + '\n')
        fixed = entry(0, 'Beta', head=[selection(0, 'Beta'), selection(2, CHINESE)],
                      name=[selection(2, CHINESE)])
        self.assertTrue(anchors.repair_matches(entry(0, 'Beta'), fixed, part))

    def test_extension_into_definition_or_other_head_rejected(self):
        _, part = source(('heading', '## Beta\n'), CHINESE + ' is a discussion.\n',
                         ('heading', '## Gamma\n'))
        for fixed in (entry(0, 'Beta', head=[selection(0, 'Beta'), selection(1, CHINESE)],
                            name=[selection(1, CHINESE)]),
                      entry(0, 'Beta', head=[selection(0, 'Beta'), selection(2, 'Gamma')])):
            with self.subTest(fixed=fixed):
                self.assertFalse(anchors.repair_matches(entry(0, 'Beta'), fixed, part))

    def test_same_language_lexical_extension_rejected(self):
        _, part = source(('heading', '## Beta Gamma\n'))
        self.assertFalse(anchors.repair_matches(entry(0, 'Beta'), entry(0, 'Beta Gamma'), part))

    def test_format_equivalent_split_head_does_not_need_equal_raw_list(self):
        _, part = source(('heading', '## **Beta ' + CHINESE + '**\n'))
        original = entry(0, 'Beta ' + CHINESE)
        fixed = entry(0, 'Beta', head=[selection(0, 'Beta', occurrence=0), selection(0, CHINESE)],
                      name=[selection(0, CHINESE)])
        self.assertTrue(anchors.repair_matches(original, fixed, part))

    def test_case_insensitive_body_mention_returns_to_real_heading(self):
        _, part = source(('heading', '## BETA\n'), ('gap', '\n'),
                         'Discussion of beta follows.\n')
        self.assertTrue(anchors.repair_matches(entry(2, 'beta'), entry(0, 'BETA'), part))

    def test_body_mention_heading_recovery_at_three_unit_limit(self):
        _, part = source(('heading', '## BETA\n'), ('gap', '\n'), 'First.\n',
                         'We discuss beta.\n')
        self.assertTrue(anchors.repair_matches(entry(3, 'beta'), entry(0, 'BETA'), part))

    def test_far_or_later_or_nonheading_recovery_rejected(self):
        for blocks, old_unit, new_unit in (
                ((('heading', '## BETA\n'), '\n', 'First.\n', '\n', 'We mention beta.\n'), 4, 0),
                (('We mention beta.\n', ('heading', '## BETA\n')), 0, 1),
                (('BETA\n', '\n', 'We mention beta.\n'), 2, 0)):
            with self.subTest(blocks=blocks):
                _, part = source(*blocks)
                self.assertFalse(anchors.repair_matches(entry(old_unit, 'beta'), entry(new_unit, 'BETA'), part))

    def test_same_unit_far_duplicate_cannot_replace_original(self):
        _, part = source('Beta ' + 'word ' * 100 + 'Beta\n')
        original = entry(0, 'Beta', head=[selection(0, 'Beta', occurrence=1)])
        self.assertFalse(anchors.repair_matches(original, entry(0, 'Beta'), part))

    def test_recovery_cannot_cross_another_heading(self):
        _, part = source(('heading', '## BETA\n'), ('heading', '## GAMMA\n'),
                         'We mention beta.\n')
        self.assertFalse(anchors.repair_matches(entry(2, 'beta'), entry(0, 'BETA'), part))

    def test_wrong_name_even_nearby_heading_is_rejected(self):
        _, part = source(('heading', '## GAMMA\n'), 'We mention beta.\n')
        self.assertFalse(anchors.repair_matches(entry(1, 'beta'), entry(0, 'GAMMA'), part))

    def test_cross_book_repair_rejected_even_at_same_location(self):
        _, part = source(('heading', '## Beta\n'))
        original = entry(0, 'Beta', source=BOOK.copy())
        for foreign in ({**BOOK, 'identifier': 'other-book'}, {**BOOK, 'md_sha256': 'other-hash'}):
            fixed = entry(0, 'Beta', source=foreign)
            with self.subTest(foreign=foreign):
                self.assertFalse(anchors.repair_matches(original, fixed, part))

    def test_repair_does_not_mutate_candidates(self):
        _, part = source(('heading', '## Beta (' + CHINESE + ')\n'))
        original = entry(0, 'Beta')
        fixed = entry(0, 'Beta (' + CHINESE + ')',
                      knowledge_point=[selection(0, 'Beta')], name=[selection(0, CHINESE)])
        before = copy.deepcopy((original, fixed, part))
        self.assertTrue(anchors.repair_matches(original, fixed, part))
        self.assertEqual((original, fixed, part), before)


if __name__ == '__main__':
    unittest.main()
