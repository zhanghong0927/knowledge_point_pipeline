"""Source-only V5 contract tests; ANCHORS_MODULE can replay the V4 baseline."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import unittest


anchors = importlib.import_module(os.environ.get('ANCHORS_MODULE', 'fullbook_v5_anchors'))
BOOK = {'identifier': 'test-book', 'md_sha256': 'abc', 'md_path': 'test.md'}
CN = '\u8d1d\u5854'


def sel(unit, quote, **extra):
    return {'unit': unit, 'quote': quote, **extra}


def entry(unit=0, quote='Beta', **extra):
    return {'head': [sel(unit, quote)], 'knowledge_point': [sel(unit, quote)],
            'name': [], 'body': [], 'body_complete': True, **extra}


def source(*blocks):
    rows, offset = [], 0
    for unit, block in enumerate(blocks):
        kind, value = block if isinstance(block, tuple) else ('paragraph', block)
        rows.append({'unit': unit, 'offset': offset, 'text': value, 'kind': kind})
        offset += len(value)
    return ''.join(r['text'] for r in rows), {'lo': 0, 'hi': len(rows), 'units': rows}


def body(start_unit, start_quote, end_unit, end_quote):
    return [{'start': sel(start_unit, start_quote), 'end': sel(end_unit, end_quote)}]


class ValidationTests(unittest.TestCase):
    def valid(self, item, text, part):
        return anchors.validate_entry(item, part, text, BOOK)

    def test_same_line_chinese_name_extends_english_only_head(self):
        text, part = source('Beta (' + CN + ') is defined.\n')
        item = entry(name=[sel(0, CN)], body=body(0, 'is defined.', 0, 'is defined.'))
        before = copy.deepcopy((item, part))
        built = self.valid(item, text, part)
        self.assertEqual(built['head'], 'Beta ' + CN)
        self.assertEqual(built['source']['head_spans'], [(0, 4), (6, 8)])
        self.assertEqual(built['name'], CN)
        self.assertEqual(built['raw_content'], 'is defined.')
        self.assertEqual((item, part), before)

    def test_chinese_first_same_line_expands_before_english_head(self):
        text, part = source(CN + ' Beta means a test.\n')
        built = self.valid(entry(name=[sel(0, CN)]), text, part)
        self.assertEqual(built['source']['head_spans'], [(0, 2), (3, 7)])
        self.assertEqual(built['head'], CN + ' Beta')

    def test_same_line_expansion_with_no_space_between_scripts(self):
        text, part = source('Beta' + CN + '\n')
        built = self.valid(entry(name=[sel(0, CN)]), text, part)
        self.assertEqual(built['source']['head_spans'], [(0, 4), (4, 6)])

    def test_adjacent_bilingual_line_in_same_paragraph_can_extend(self):
        text, part = source('Beta\n' + CN + '\nDefinition.\n')
        built = self.valid(entry(name=[sel(0, CN)]), text, part)
        self.assertEqual(built['source']['head_spans'], [(0, 4), (5, 7)])

    def test_cross_unit_bilingual_head_preserves_crlf(self):
        text, part = source(('heading', '## Beta\r\n'), CN + '\r\n', 'Body.\r\n')
        item = entry(head=[sel(0, 'Beta'), sel(1, CN)], name=[sel(1, CN)],
                     body=body(2, 'Body.', 2, 'Body.'))
        built = self.valid(item, text, part)
        self.assertEqual(built['source']['head_spans'], [(3, 7), (9, 11)])
        self.assertEqual(built['source']['body_spans'], [(13, 18)])

    def test_normal_multiline_same_language_title_is_allowed(self):
        text, part = source(('heading', '## PURE FOOD AND\n'), 'DRUG ACT\n', 'Definition.\n')
        title = [sel(0, 'PURE FOOD AND'), sel(1, 'DRUG ACT')]
        built = self.valid(entry(quote='PURE FOOD AND', head=title, knowledge_point=title), text, part)
        self.assertEqual(built['knowledge_point'], 'PURE FOOD AND DRUG ACT')

    def test_cannot_extend_over_definition_or_paragraph_or_next_head(self):
        for value in ('Beta means ' + CN + '.\n', 'Beta\n\n' + CN + '\n',
                      'Beta. ' + CN + '\n', 'Beta\n## ' + CN + '\n',
                      'Beta ' + CN + '\u662f\u4e00\u79cd\u5de5\u5177\u3002\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                text, part = source(value)
                self.valid(entry(name=[sel(0, CN)]), text, part)

    def test_cannot_extend_into_separate_paragraph_unit(self):
        text, part = source('Beta\n', 'Only ' + CN + ' occurs here.\n')
        with self.assertRaises(ValueError):
            self.valid(entry(name=[sel(1, CN)]), text, part)

    def test_repeat_translation_in_body_uses_unique_adjacent_name(self):
        text, part = source('Beta (' + CN + ') defines ' + CN + '.\n')
        built = self.valid(entry(name=[sel(0, CN)]), text, part)
        self.assertEqual(built['name'], CN)
        self.assertEqual(built['source']['head_spans'], [(0, 4), (6, 8)])

    def test_occurrence_indexes_all_matches_before_name_filtering(self):
        text, part = source(CN + ' mention.\nBeta (' + CN + ')\n')
        item = entry(name=[sel(0, CN, occurrence=1)])
        self.assertEqual(self.valid(item, text, part)['name'], CN)
        item['name'][0]['occurrence'] = 0
        with self.assertRaises(ValueError):
            self.valid(item, text, part)

    def test_repeated_translation_on_both_sides_is_ambiguous(self):
        text, part = source(CN + ' Beta ' + CN + '\n')
        with self.assertRaises(ValueError):
            self.valid(entry(name=[sel(0, CN)]), text, part)

    def test_no_translation_or_ocr_correction_is_synthesized(self):
        text, part = source('Bcta\nDefinition.\n')
        self.assertEqual(self.valid(entry(quote='Bcta'), text, part)['knowledge_point'], 'Bcta')
        for item in (entry(quote='Beta'), entry(quote='Bcta', name=[sel(0, CN)])):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.valid(item, text, part)

    def test_body_end_before_head_is_filtered_not_first_or_last(self):
        text, part = source('end.\nBeta\nStart end.\n## Gamma \u4f3d\u9a6c\nOther end.\n')
        built = self.valid(entry(body=body(0, 'Start', 0, 'end.')), text, part)
        self.assertEqual(built['raw_content'], 'Start end.')
        self.assertEqual(built['source']['body_spans'], [(10, 20)])

    def test_body_start_and_end_scoped_to_current_clear_next_head(self):
        text, part = source('Beta\nStart end.\n## Gamma \u4f3d\u9a6c\nStart end.\n')
        built = self.valid(entry(body=body(0, 'Start', 0, 'end.')), text, part)
        self.assertEqual(built['raw_content'], 'Start end.')
        self.assertEqual(built['source']['body_spans'], [(5, 15)])

    def test_body_end_uses_only_unique_candidate_after_start(self):
        text, part = source('Beta\nend Start end\n')
        self.assertEqual(self.valid(entry(body=body(0, 'Start', 0, 'end')), text, part)
                         ['raw_content'], 'Start end')

    def test_two_body_candidates_in_current_entry_are_rejected(self):
        text, part = source('Beta\nStart end then end.\n## Gamma\nend\n')
        with self.assertRaises(ValueError):
            self.valid(entry(body=body(0, 'Start', 0, 'end')), text, part)
        item = entry(body=body(0, 'Start', 0, 'end'))
        item['body'][0]['end']['occurrence'] = 1
        self.assertEqual(self.valid(item, text, part)['raw_content'], 'Start end then end')

    def test_explicit_occurrence_cannot_select_next_entry(self):
        text, part = source('Beta\nStart end.\n## Gamma \u4f3d\u9a6c\nStart end.\n')
        item = entry(body=body(0, 'Start', 0, 'end.'))
        item['body'][0]['end']['occurrence'] = 1
        with self.assertRaises(ValueError):
            self.valid(item, text, part)

    def test_next_head_in_another_unit_bounds_body(self):
        text, part = source(('heading', '## Beta\n'), 'Start.\n',
                            ('heading', '## Gamma \u4f3d\u9a6c\n'), 'Wrong.\n')
        with self.assertRaises(ValueError):
            self.valid(entry(body=body(1, 'Start.', 3, 'Wrong.')), text, part)

    def test_internal_subheading_is_not_a_next_entry_boundary(self):
        text, part = source(('heading', '## Beta\n'), 'Start.\n',
                            ('heading', '### Details\n'), 'Final.\n')
        part['units'][2]['head_role'] = 'internal_after_bilingual_main'
        built = self.valid(entry(body=body(1, 'Start.', 3, 'Final.')), text, part)
        self.assertEqual(built['raw_content'], 'Start.\n### Details\nFinal.')

    def test_flattened_same_level_internal_heading_keeps_long_body(self):
        text, part = source(('heading', '## Beta\n'), 'Start.\n',
                            ('heading', '## History\n'), 'Final.\n')
        built = self.valid(entry(body=body(1, 'Start.', 3, 'Final.')), text, part)
        self.assertEqual(built['raw_content'], 'Start.\n## History\nFinal.')

    def test_flattened_internal_heading_cannot_pick_duplicate_end(self):
        text, part = source('## Beta\nStart end\n## Intellectual work\nMore end\n')
        item = entry(body=body(0, 'Start', 0, 'end'))
        with self.assertRaises(ValueError):
            self.valid(item, text, part)
        item['body'][0]['end']['occurrence'] = 1
        self.assertEqual(self.valid(item, text, part)['raw_content'],
                         'Start end\n## Intellectual work\nMore end')

    def test_body_fragment_boundaries_cannot_cut_inside_words(self):
        text, part = source('Beta\nStarted endword\n')
        for ranges in (body(0, 'arted', 0, 'endword'), body(0, 'Started', 0, 'end')):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                self.valid(entry(body=ranges), text, part)

    def test_explicit_multiline_title_with_format_blankline_is_allowed(self):
        text, part = source(('heading', '## PURE FOOD AND\n'), ('gap', '\n'), 'DRUG ACT\n')
        title = [sel(0, 'PURE FOOD AND'), sel(2, 'DRUG ACT')]
        built = self.valid(entry(quote='PURE FOOD AND', head=title, knowledge_point=title), text, part)
        self.assertEqual(built['knowledge_point'], 'PURE FOOD AND DRUG ACT')

    def test_explicit_bilingual_title_with_format_blankline_is_allowed(self):
        text, part = source(('heading', '## ' + CN + '\n'), ('gap', '\n'), 'Beta\n')
        item = entry(quote=CN, head=[sel(0, CN), sel(2, 'Beta')],
                     knowledge_point=[sel(2, 'Beta')], name=[sel(0, CN)])
        self.assertEqual(self.valid(item, text, part)['head'], CN + ' Beta')

    def test_numbered_dictionary_names_are_not_arbitrary_suffixes(self):
        for prefix in ('04.124 ', '\u246f ', '\u247d '):
            title = prefix + CN + ' Beta'
            text, part = source(title + '\n')
            item = entry(quote=title, knowledge_point=[sel(0, 'Beta')], name=[sel(0, CN)])
            self.assertEqual(self.valid(item, text, part)['name'], CN)

    def test_optional_pinyin_pronunciation_and_pos_are_not_name_omissions(self):
        for title, key, chosen in ((CN + ' | b\u00e8i t\u01ce', 'name', CN),
                                   ('preamble n.', 'knowledge_point', 'preamble'),
                                   ('COP8 (cop eight)', 'knowledge_point', 'COP8'),
                                   ('Beta (' + CN + ')', 'knowledge_point', 'Beta')):
            text, part = source(title + '\n')
            item = entry(quote=title, knowledge_point=[], name=[])
            item[key] = [sel(0, chosen)]
            built = self.valid(item, text, part)
            self.assertEqual(built[key], chosen)

    def test_source_heading_translations_need_not_be_final_english_fields(self):
        text, part = source(('heading', '## MIXED-MEANS PERFORMANCE\n'), ('gap', '\n'),
                            ('heading', '## SPECTACLE AUX TECHNIQUES MIXTES\n'))
        item = entry(quote='MIXED-MEANS PERFORMANCE',
                     head=[sel(0, 'MIXED-MEANS PERFORMANCE'), sel(2, 'SPECTACLE AUX TECHNIQUES MIXTES')])
        self.assertEqual(self.valid(item, text, part)['knowledge_point'], 'MIXED-MEANS PERFORMANCE')

    def test_disjoint_original_name_senses_can_skip_other_language_expansions(self):
        title = 'AC (=address counter) \u5730\u5740\u8ba1\u6570\u5668 (=adjacent channel) \u76f8\u90bb\u4fe1\u9053'
        text, part = source(title + '\n')
        item = entry(quote=title, knowledge_point=[sel(0, 'AC')],
                     name=[sel(0, '\u5730\u5740\u8ba1\u6570\u5668'), sel(0, '\u76f8\u90bb\u4fe1\u9053')])
        self.assertEqual(self.valid(item, text, part)['name'], '\u5730\u5740\u8ba1\u6570\u5668 \u76f8\u90bb\u4fe1\u9053')

    def test_caps_section_is_not_a_lexical_next_head_even_with_main_hint(self):
        text, part = source(('heading', '## JEPHTHAH.\n'), 'Start.\n',
                            ('heading', '## SECTION II\n'), 'Final.\n')
        part['units'][0]['head_role'] = part['units'][2]['head_role'] = 'main_style'
        built = self.valid(entry(quote='JEPHTHAH.', body=body(1, 'Start.', 3, 'Final.')), text, part)
        self.assertEqual(built['raw_content'], 'Start.\n## SECTION II\nFinal.')

    def test_same_language_slash_alias_requires_occurrence_but_is_a_name(self):
        text, part = source('## Beta / Beta\n')
        item = entry(quote='Beta / Beta', knowledge_point=[sel(0, 'Beta')])
        with self.assertRaises(ValueError):
            self.valid(item, text, part)
        item['knowledge_point'][0]['occurrence'] = 1
        self.assertEqual(self.valid(item, text, part)['knowledge_point'], 'Beta')

    def test_pdf_bold_or_main_style_alone_does_not_disambiguate(self):
        text, part = source('Beta\nStart end\n', 'Discussion\nend\n')
        part['units'][1].update(head_role='main_style', pdf_format=[{
            'spans': [{'text': 'Discussion', 'bold': True}]}])
        # Both matches are in unit 0 when units are combined; metadata alone cannot cut it.
        part['units'][0]['text'] = text
        part['units'] = part['units'][:1]
        part['units'][0]['pdf_format'] = [{'spans': [{'text': 'Discussion', 'bold': True}]}]
        with self.assertRaises(ValueError):
            self.valid(entry(body=body(0, 'Start', 0, 'end')), text, part)

    def test_body_does_not_cross_excluded_region(self):
        text, part = source('Beta\n', 'Start.\n', 'SECRET\n', 'Final.\n')
        part['units'][2]['excluded_zone'] = 'index'
        with self.assertRaises(ValueError):
            self.valid(entry(body=body(1, 'Start.', 3, 'Final.')), text, part)

    def test_audit_a053_a074_profession_fields_are_rejected(self):
        cases = [
            ('Lewis, Meade \u201cLux\u201d (b. Chicago, 1905; d. Minneapolis, 1964). '
             'American popular pianist and composer, known for his popularization and '
             'development of the boo-gie-woogie style.',
             'American popular pianist and composer, known for his popularization and '
             'development of the boo-gie-woogie style.'),
            ('Levy, Ernst (b. Basel, Switzerland, 1895; d. Morges, Switzerland, 1981). '
             'Swiss pianist, conductor, and theorist.', 'Swiss pianist, conductor, and theorist.')]
        for head, introduction in cases:
            for key in ('knowledge_point', 'name'):
                text, part = source(head + '\nBiography.\n')
                item = entry(quote=head, knowledge_point=[], name=[])
                item[key] = [sel(0, introduction)]
                with self.subTest(key=key, head=head), self.assertRaises(ValueError):
                    self.valid(item, text, part)

    def test_audit_a039_person_plus_profession_is_rejected(self):
        head = 'LEWY, JULIUS (1895\u20131963), Semitic philologist and Assyriologist.'
        text, part = source(head + ' Born in Berlin.\n')
        with self.assertRaises(ValueError):
            self.valid(entry(quote=head), text, part)

    def test_true_person_name_with_dates_is_not_overrestricted(self):
        for quote in ('LEWY, JULIUS', 'LEWY, JULIUS (1895\u20131963)',
                      'Levy, Ernst (b. Basel, Switzerland, 1895; d. Morges, Switzerland, 1981).',
                      'Lewis, Meade \u201cLux\u201d', 'Pollini, Maurizio'):
            text, part = source(quote + ' Swiss pianist, conductor, and theorist.\n')
            self.assertEqual(self.valid(entry(quote=quote), text, part)['knowledge_point'], quote)

    def test_pdf_hint_does_not_require_all_titles_to_be_bold(self):
        text, part = source('Beta (' + CN + ')\n')
        part['units'][0]['pdf_format'] = [{'spans': [
            {'text': 'Beta', 'bold': True}, {'text': ' (' + CN + ')', 'bold': False}]}]
        built = self.valid(entry(quote='Beta (' + CN + ')', knowledge_point=[sel(0, 'Beta')],
                                 name=[sel(0, CN)]), text, part)
        self.assertEqual(built['name'], CN)

    def test_introduction_field_cannot_hide_behind_true_name_in_head(self):
        head = 'Beta is a tool used for testing.'
        text, part = source(head + '\n')
        with self.assertRaises(ValueError):
            self.valid(entry(quote=head, knowledge_point=[sel(0, 'a tool used for testing.')]), text, part)

    def test_disjoint_head_cannot_skip_definition_to_borrow_name(self):
        text, part = source('Beta\nDefinition.\n' + CN + '\n')
        with self.assertRaises(ValueError):
            self.valid(entry(head=[sel(0, 'Beta'), sel(0, CN)], name=[sel(0, CN)]), text, part)

    def test_final_name_does_not_cut_inside_original_word(self):
        for title, chosen in (('John Smith', 'mith'), ('John Smith', 'Joh'),
                              ('Beta Gamma', 'amma'), ('Beta Gamma', 'Bet')):
            text, part = source(title + '\n')
            with self.subTest(title=title, chosen=chosen), self.assertRaises(ValueError):
                self.valid(entry(quote=title, knowledge_point=[sel(0, chosen)]), text, part)

    def test_ordinary_inline_terms_aliases_and_multilingual_names(self):
        for title in ('pianist', 'Swiss cheese', 'The Art of Fugue', 'See-saw',
                      'absolute worst case(AWC)', 'J. S. Bach', 'Poems 1918\u201321'):
            text, part = source(title + ' Definition follows.\n')
            self.assertEqual(self.valid(entry(quote=title), text, part)['knowledge_point'], title)

    def test_subwords_and_midparagraph_heads_are_rejected(self):
        for value in ('Betamax mentions Beta.\n', 'Earlier Beta then Beta.\n'):
            text, part = source(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.valid(entry(), text, part)

    def test_clipped_unit_does_not_become_line_start(self):
        text, part = source('Earlier ', 'Beta.\n')
        part['units'] = part['units'][1:]
        part['lo'] = 1
        with self.assertRaises(ValueError):
            self.valid(entry(unit=1), text, part)

    def test_repeated_head_never_defaults_to_first_or_last(self):
        text, part = source('Beta first.\nBeta second.\n')
        with self.assertRaises(ValueError):
            self.valid(entry(), text, part)
        built = self.valid(entry(head=[sel(0, 'Beta', occurrence=1)]), text, part)
        self.assertEqual(built['source']['head_spans'], [(12, 16)])

    def test_schema_offset_identity_and_ownership_contract(self):
        text, part = source('Beta\n')
        cases = [(entry(body_complete=1), part, BOOK),
                 (entry(knowledge_point=[]), part, BOOK),
                 (entry(), {**part, 'lo': 1}, BOOK),
                 (entry(source={'identifier': 'foreign'}), part, BOOK),
                 (entry(), part, {'identifier': 'test-book'}),
                 (entry(head=[sel(True, 'Beta')]), part, BOOK),
                 (entry(), {**part, 'units': [{**part['units'][0], 'offset': 1}]}, BOOK)]
        for item, packet, book in cases:
            with self.subTest(item=item, packet=packet, book=book), self.assertRaises(ValueError):
                anchors.validate_entry(item, packet, text, book)

    def test_output_contract_position_id_context_and_no_mutation(self):
        text, part = source('x' * 750 + '\n', 'Beta\n', 'Definition.\n', 'y' * 1400)
        item = entry(1, body=body(2, 'Definition.', 2, 'Definition.'))
        before = copy.deepcopy((item, part))
        built = self.valid(item, text, part)
        self.assertEqual(set(built), {'id', 'knowledge_point', 'name', 'head', 'raw_content',
                                     'body_complete', 'leading_context', 'trailing_context',
                                     'source', 'ready_for_delivery'})
        self.assertEqual(built['id'], hashlib.sha256(b'test-book:abc:751').hexdigest()[:24])
        self.assertEqual(built['source'], {**BOOK, 'head_spans': [(751, 755)],
                                         'body_spans': [(756, 767)]})
        self.assertEqual(len(built['leading_context']), 600)
        self.assertEqual(len(built['trailing_context']), 1200)
        self.assertFalse(built['ready_for_delivery'])
        self.assertEqual((item, part), before)

    def test_live_audit_fixtures_reject_three_known_name_errors(self):
        root = os.environ.get('AUDIT300_DIR')
        if not root:
            self.skipTest('Set AUDIT300_DIR for frozen audit300 fixture verification')
        samples = json.loads((Path(root) / 'samples.json').read_text(encoding='utf-8-sig'))
        cases = {x['sample_id']: x for x in samples if x['sample_id'] in {'A039', 'A053', 'A074'}}
        self.assertEqual(set(cases), {'A039', 'A053', 'A074'})
        for sample_id, sample in cases.items():
            row = sample['pdf_hints'][0]
            text, part = source(row['text'])
            part['units'][0]['pdf_format'] = row.get('pdf_format', [])
            e = sample['entry']
            item = entry(quote=e['head'], knowledge_point=[sel(0, e['knowledge_point'])],
                         name=[sel(0, e['name'])] if e['name'] else [])
            with self.subTest(sample_id=sample_id), self.assertRaises(ValueError):
                self.valid(item, text, part)

    def test_real300_preliminary_healthy_rejections_are_not_systematic(self):
        path = os.environ.get('REPLAY300_PATH')
        root = os.environ.get('AUDIT300_DIR')
        if not path or not root:
            self.skipTest('Set REPLAY300_PATH and AUDIT300_DIR for original-unit regression')
        replay = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        manifest = json.loads((Path(root) / 'MANIFEST.json').read_text(encoding='utf-8-sig'))
        folders = {b['book']: Path(b['folder']) for b in manifest['books']}
        grouped = {}
        for sample in replay:
            if sample.get('review', {}).get('judgment') == 'ok' and sample.get('rejected'):
                grouped.setdefault(sample['book'], []).append(sample)
        self.assertGreater(len(grouped), 5)
        for book_id, samples in grouped.items():
            units = json.loads((folders[book_id] / 'units.json').read_text(encoding='utf-8-sig'))
            text = Path(samples[0]['old']['source']['md_path']).read_bytes().decode('utf-8-sig')
            by_unit = {r['unit']: r for r in units}
            for sample in samples:
                raw = sample['rejected'][0]['entry']
                selected = [s['unit'] for s in raw['head'] + raw['knowledge_point'] + raw['name']]
                selected += [s[k]['unit'] for s in raw['body'] for k in ('start', 'end')]
                lo, hi = min(selected), max(selected) + 1
                part = {'lo': lo, 'hi': hi,
                        'units': [by_unit[u] for u in range(max(0, lo - 12), min(len(units), hi + 12))]}
                with self.subTest(sample_id=sample['sample_id']):
                    built = anchors.validate_entry(raw, part, text, sample['old']['source'])
                    self.assertEqual(built['knowledge_point'], sample['old']['knowledge_point'])
                    self.assertEqual(built['name'], sample['old']['name'])
                    self.assertEqual(built['raw_content'], sample['old']['raw_content'])

    def test_explicit_crossline_translation_cannot_be_definition_prefix(self):
        text, part = source('Beta\n', CN + ' is a discussion.\n')
        item = entry(head=[sel(0, 'Beta'), sel(1, CN)], name=[sel(1, CN)])
        with self.assertRaises(ValueError):
            self.valid(item, text, part)


class RepairTests(unittest.TestCase):
    def test_crop_see_guide_keeps_same_literal_head_identity(self):
        text, part = source('Beta See Gamma.\n')
        self.assertTrue(anchors.repair_matches(entry(quote='Beta See'), entry(), part))
        self.assertEqual(anchors.validate_entry(entry(), part, text, BOOK)['knowledge_point'], 'Beta')

    def test_crop_leading_see_guide_keeps_target_identity(self):
        text, part = source(('heading', '## See Beta\n'))
        fixed = entry()
        self.assertTrue(anchors.repair_matches(entry(quote='See Beta'), fixed, part))
        self.assertEqual(anchors.validate_entry(fixed, part, text, BOOK)['knowledge_point'], 'Beta')

    def test_guide_cropping_cannot_jump_to_referenced_entry(self):
        _, part = source('Beta See Gamma.\n')
        self.assertFalse(anchors.repair_matches(entry(quote='Beta See'), entry(quote='Gamma'), part))

    def test_inline_adjacent_translation_extension_is_identity_safe(self):
        _, part = source('Beta (' + CN + ') is a tool.\n')
        fixed = entry(head=[sel(0, 'Beta'), sel(0, CN)], name=[sel(0, CN)])
        self.assertTrue(anchors.repair_matches(entry(), fixed, part))

    def test_contiguous_bilingual_extension_preserves_v4_repair_contract(self):
        _, part = source('Beta (' + CN + ') is a tool.\n')
        fixed = entry(quote='Beta (' + CN + ')', knowledge_point=[sel(0, 'Beta')], name=[sel(0, CN)])
        self.assertTrue(anchors.repair_matches(entry(), fixed, part))
        self.assertTrue(anchors.repair_matches(entry(quote=CN), fixed, part))

    def test_translation_extension_and_guide_crop_can_combine(self):
        _, part = source('Beta (' + CN + ') See Gamma.\n')
        original = entry(quote='Beta (' + CN + ') See')
        fixed = entry(head=[sel(0, 'Beta'), sel(0, CN)], name=[sel(0, CN)])
        self.assertTrue(anchors.repair_matches(original, fixed, part))

    def test_biography_crop_cannot_change_the_person(self):
        head = 'LEWY, JULIUS (1895\u20131963), Semitic philologist and Assyriologist.'
        _, part = source(head + '\n', 'LEWY, YOHANAN\n')
        self.assertTrue(anchors.repair_matches(entry(quote=head), entry(quote='LEWY, JULIUS'), part))
        self.assertFalse(anchors.repair_matches(entry(quote=head), entry(1, 'LEWY, YOHANAN'), part))

    def test_extension_into_definition_paragraph_or_other_head_is_rejected(self):
        for blocks in (('Beta means ' + CN + '.\n',), ('Beta\n\n' + CN + '\n',),
                       ('Beta\n', ('heading', '## ' + CN + '\n'))):
            _, part = source(*blocks)
            unit = len(blocks) - 1
            fixed = entry(head=[sel(0, 'Beta'), sel(unit, CN)], name=[sel(unit, CN)])
            with self.subTest(blocks=blocks):
                self.assertFalse(anchors.repair_matches(entry(), fixed, part))

    def test_arbitrary_same_language_crop_or_extension_is_rejected(self):
        _, part = source('Beta Gamma\n')
        self.assertFalse(anchors.repair_matches(entry(), entry(quote='Beta Gamma'), part))
        self.assertFalse(anchors.repair_matches(entry(quote='Beta Gamma'), entry(), part))

    def test_missing_evidence_or_relocated_duplicate_cannot_repair(self):
        _, part = source('Beta ' + 'word ' * 100 + 'Beta\n')
        self.assertFalse(anchors.repair_matches(entry(quote='Bcta'), entry(), part))
        self.assertFalse(anchors.repair_matches(entry(head=[sel(0, 'Beta', occurrence=1)]), entry(), part))

    def test_format_equivalent_splits_and_cross_book_guard(self):
        _, part = source(('heading', '## **Beta ' + CN + '**\n'))
        original = entry(quote='Beta ' + CN)
        fixed = entry(head=[sel(0, 'Beta'), sel(0, CN)], name=[sel(0, CN)])
        before = copy.deepcopy((original, fixed, part))
        self.assertTrue(anchors.repair_matches(original, fixed, part))
        self.assertEqual((original, fixed, part), before)
        self.assertFalse(anchors.repair_matches({**original, 'source': BOOK},
                                               {**fixed, 'source': {**BOOK, 'md_sha256': 'foreign'}}, part))

    def test_v4_same_name_body_mention_recovery_is_preserved(self):
        _, part = source(('heading', '## BETA\n'), ('gap', '\n'), 'Discussion of beta.\n')
        self.assertTrue(anchors.repair_matches(entry(2, 'beta'), entry(0, 'BETA'), part))
        _, part = source(('heading', '## BETA\n'), ('heading', '## GAMMA\n'), 'Discussion of beta.\n')
        self.assertFalse(anchors.repair_matches(entry(2, 'beta'), entry(0, 'BETA'), part))


if __name__ == '__main__':
    unittest.main()
