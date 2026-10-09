"""Source-only V5 regressions; remote audit fixtures are read, never rewritten."""
import copy
import concurrent.futures
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

import fullbook_llm_v2 as v2

# Before V5 exists, exercise real V4 behavior to demonstrate the regressions.
MODULE = os.environ.get('FULLBOOK_STRUCTURE_MODULE', 'fullbook_v5_structure')
g = importlib.import_module(MODULE if importlib.util.find_spec(MODULE)
                            else 'fullbook_v4_structure')
cleanup = getattr(g, 'cleanup_body', None)
if cleanup is None:
    cleanup = importlib.import_module('fullbook_llm_v4').cleanup_body


def record(text, head, body, stop=None, head_start=None):
    a = text.index(head) if head_start is None else head_start
    spans = [] if body is None else [(text.index(body), len(text) if stop is None else stop)]
    return {'head': head, 'knowledge_point': head, 'name': '',
            'raw_content': '\n\n'.join(text[x:y] for x, y in spans),
            'body_complete': True,
            'source': {'head_spans': [(a, a + len(head))], 'body_spans': spans}}


class BodyTests(unittest.TestCase):
    def clean(self, body, selected=None):
        text = '## ALPHA\n\n' + body
        entry = record(text, 'ALPHA', body, None if selected is None else 10 + len(selected))
        cleanup(entry, g.annotate(v2.structural_units(text)), text)
        self.assert_exact(entry, text)
        return entry

    def assert_exact(self, entry, text):
        self.assertEqual(entry['raw_content'], '\n\n'.join(
            text[a:b] for a, b in entry['source']['body_spans']))
        for removal in entry.get('body_cleanup_removed', []):
            if 'text' in removal:
                self.assertEqual(removal['text'], text[removal['start']:removal['end']])

    def test_retains_complete_sentence_before_broken_word(self):
        e = self.clean('A valid sentence. An unfinished parame-')
        self.assertEqual(e['raw_content'], 'A valid sentence.')
        self.assertFalse(e['body_complete'])
        self.assertTrue(e['body_cleanup_removed'])

    def test_no_complete_sentence_becomes_empty(self):
        e = self.clean('n. An unfinished parame-')
        self.assertEqual(e['raw_content'], '')
        self.assertEqual(e['source']['body_spans'], [])

    def test_dangling_colon_is_trimmed(self):
        self.assertEqual(self.clean('A valid sentence. The examples are:')['raw_content'],
                         'A valid sentence.')

    def test_only_dangling_colon_becomes_empty(self):
        self.assertEqual(self.clean('The examples are:')['raw_content'], '')

    def test_cut_inside_chinese_phrase_is_trimmed(self):
        self.assertEqual(self.clean('\u5b8c\u6574\u53e5\u3002\u7f57\u9a6c\u5929\u4e3b\u6559\u89c2\u70b9\u3002',
                                    '\u5b8c\u6574\u53e5\u3002\u7f57\u9a6c\u5929')['raw_content'],
                         '\u5b8c\u6574\u53e5\u3002')

    def test_unpunctuated_complete_gloss_is_not_deleted(self):
        body = 'Self-evident assent without investigation; opposed to nazari'
        self.assertEqual(self.clean(body)['raw_content'], body)

    def test_omitted_source_period_does_not_make_gloss_incomplete(self):
        text = '## ALPHA\n\nSelf-evident assent.\n\n## BETA\n'
        e = record(text, 'ALPHA', 'Self-evident', text.index('.'))
        e['body_complete'] = False
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], 'Self-evident assent')

    def test_editorial_note_is_not_positive_continuation(self):
        body = 'Conditional proposition [al-shartiyah](q.v.)'
        text = '## ALPHA\n\n' + body + '\n\n[ed. A separate editorial note.]\n\n## BETA\n'
        e = record(text, 'ALPHA', body, text.index('\n\n[ed.'))
        e['body_complete'] = False
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], body)

    def test_complete_abbreviations_are_preserved(self):
        body = 'n. A U.S. device, e.g. a D/A converter. It runs at 1.5 V.'
        self.assertEqual(self.clean(body)['raw_content'], body)

    def test_complete_sentence_can_end_with_uppercase_abbreviation(self):
        self.assertEqual(self.clean('It is used in the U.S.')['raw_content'],
                         'It is used in the U.S.')

    def test_sentence_ending_in_decade_keeps_single_letter_suffix(self):
        text = '## ALPHA\n\nIt changed in the 1920s.\n\n(See also another article.)'
        e = record(text, 'ALPHA', 'It changed', text.index('\n\n(See'))
        e['body_complete'] = False
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], 'It changed in the 1920s.')

    def test_omitted_chinese_period_before_color_code_is_not_truncation(self):
        text = '## ALPHA\n\n\u5b8c\u6574\u53e5\u3002\u989c\u8272\u53eb\u82e5\u7af9\u8272\u3002\n\nNo. 011\n\nC 55/M 30/Y 65/K 35'
        e = record(text, 'ALPHA', '\u5b8c\u6574', text.index('\u3002', text.index('\u989c\u8272')))
        e['body_complete'] = False
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], '\u5b8c\u6574\u53e5\u3002\u989c\u8272\u53eb\u82e5\u7af9\u8272')

    def test_abbreviation_period_is_not_a_complete_definition(self):
        self.assertEqual(self.clean('abbr. Used for an incom-')['raw_content'], '')

    def test_decimal_is_not_sentence_boundary(self):
        self.assertEqual(self.clean('Known. The value is 1.5 and ris-')['raw_content'], 'Known.')

    def test_initials_are_not_sentence_boundary(self):
        self.assertEqual(self.clean('Known. Written by J. W. Smith and incom-')['raw_content'],
                         'Known.')

    def test_quoted_complete_sentence_is_preserved(self):
        body = 'He said "Done."'
        self.assertEqual(self.clean(body)['raw_content'], body)

    def test_does_not_extend_into_foreign_entry(self):
        body = 'A valid sentence. An unfinished parame-\n\n## BETA\n\nA stranger definition.'
        text = '## ALPHA\n\n' + body
        e = record(text, 'ALPHA', 'A valid', text.index('\n\n## BETA'))
        original = copy.deepcopy(e['source']['body_spans'])
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], 'A valid sentence.')
        self.assertEqual(e['source']['original_body_spans'], original)
        self.assertNotIn('stranger', e['raw_content'])
        self.assertIn('BETA', e['body_context_after']['text'])
        self.assert_exact(e, text)

    def test_partial_unit_never_deletes_unselected_text(self):
        text = '## ALPHA\n\nA valid sentence. Unselected tail.'
        e = record(text, 'ALPHA', 'A valid', text.index(' Unselected'))
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], 'A valid sentence.')
        self.assertFalse(e.get('body_cleanup_removed'))

    def test_empty_body_is_allowed_and_raw_is_source_derived(self):
        text = '## ALPHA\n\n## BETA\n'
        e = record(text, 'ALPHA', None)
        e['raw_content'] = 'stale model output'
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['raw_content'], '')
        self.assertTrue(e['eligible_for_name_screening'])

    def test_ocr_is_not_corrected(self):
        body = 'A boo-gie-woogie player used air-look devices.'
        self.assertEqual(self.clean(body)['raw_content'], body)

    def test_disjoint_source_spans_and_existing_log_are_preserved(self):
        text = '## ALPHA\n\nFirst.\n\n![](figure/a.png)\n\nSecond. A tail-'
        image = (text.index('![]'), text.index('Second.'))
        e = record(text, 'ALPHA', 'First.')
        e['source']['body_spans'] = [(text.index('First.'), image[0]), (image[1], len(text))]
        e['body_cleanup_removed'] = [{'start':image[0], 'end':image[1], 'reason':'image_markup'}]
        cleanup(e, g.annotate(v2.structural_units(text)), text)
        self.assertIn('First.', e['raw_content'])
        self.assertTrue(e['raw_content'].endswith('Second.'))
        self.assertNotIn('tail-', e['raw_content'])
        self.assertEqual(e['body_cleanup_removed'][0]['reason'], 'image_markup')
        self.assert_exact(e, text)

    def test_cleanup_is_idempotent(self):
        text = '## ALPHA\n\nKnown. An unfinished word-'
        rows = g.annotate(v2.structural_units(text))
        e = record(text, 'ALPHA', 'Known.')
        cleanup(e, rows, text)
        before = copy.deepcopy(e)
        cleanup(e, rows, text)
        self.assertEqual(e, before)

    def test_concurrent_book_cleanup_does_not_mutate_units(self):
        text = '## ALPHA\n\nKnown. Unfinished word-\n\n' + ('## BETA\n\nOther definition.\n\n' * 40)
        fixtures = [g.annotate(v2.structural_units(text)) for _ in range(96)]
        fingerprints = [hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                        for rows in fixtures]
        stop = text.index('\n\n## BETA')
        def run(index):
            e = record(text, 'ALPHA', 'Known.', stop)
            cleanup(e, fixtures[index % len(fixtures)], text)
            g.guard_entries([e], fixtures[index % len(fixtures)], text)
            self.assertEqual(e['raw_content'], 'Known.')
        interval = sys.getswitchinterval()
        try:
            sys.setswitchinterval(0.000001)
            with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
                list(pool.map(run, range(1024)))
        finally:
            sys.setswitchinterval(interval)
        self.assertEqual(fingerprints,
                         [hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                          for rows in fixtures])

    def test_same_sentence_caption_requires_source_evidence(self):
        text = ('## ALPHA\n\nThe text discusses emotional\n\n![](figure/a.png)\n\n'
                'Urban graffiti.\n\nPhoto J.W.Love.\n\nrepression and desire.')
        rows = g.annotate(v2.structural_units(text))
        for row in rows:
            if row['text'].strip() in ('Urban graffiti.', 'Photo J.W.Love.'):
                row['pdf_format'] = [{'spans':[{'text':row['text'], 'italic':True,
                                               'size_ratio':1.2, 'font':'CaptionItalic'}]}]
            elif 'repression' in row['text']:
                row['pdf_format'] = [{'spans':[{'text':row['text'], 'italic':False,
                                               'size_ratio':1.0, 'font':'Body'}]}]
        e = record(text, 'ALPHA', 'The text')
        cleanup(e, rows, text)
        self.assertNotIn('Urban graffiti.', e['raw_content'])
        self.assertNotIn('Photo J.W.Love.', e['raw_content'])
        self.assertIn('emotional', e['raw_content'])
        self.assertIn('repression and desire.', e['raw_content'])
        self.assertTrue(any(r['reason']=='evidenced_inline_caption'
                            for r in e['body_cleanup_removed']))
        self.assert_exact(e, text)

    def test_caption_words_without_image_or_pdf_are_not_deleted(self):
        text = ('## ALPHA\n\nThe text discusses emotional\n\nUrban graffiti.\n\n'
                'Photo J.W.Love.\n\nrepression and desire.')
        rows = g.annotate(v2.structural_units(text))
        e = record(text, 'ALPHA', 'The text')
        cleanup(e, rows, text)
        self.assertIn('Photo J.W.Love.', e['raw_content'])
        self.assertTrue(e.get('structural_review_required'))
        self.assertFalse(e.get('eligible_for_name_screening', True))

    def test_image_alone_does_not_prove_following_prose_is_caption(self):
        body = 'First.\n\n![](figure/a.png)\n\nA normal sentence about a photo.'
        e = self.clean(body)
        self.assertIn('A normal sentence about a photo.', e['raw_content'])
        self.assertNotIn('![]', e['raw_content'])

    def test_intact_formula_at_end_is_not_a_broken_sentence(self):
        body = 'The rule is defined.\n\n$$\nA = B + C\n$$\n'
        self.assertEqual(self.clean(body)['raw_content'], body)


class StructureTests(unittest.TestCase):
    def test_context_heads_do_not_contaminate_selected_body(self):
        text = '## ALPHA\n\nOwn definition.\n\n## BETA\n\nForeign definition.'
        rows = g.annotate(v2.structural_units(text))
        a = record(text, 'ALPHA', 'Own', text.index('\n\n## BETA'))
        b = record(text, 'BETA', 'Foreign')
        a['trailing_context'] = text[text.index('## BETA'):]
        a['body_coverage_warning'] = 'coverage_gap'
        a['structural_review_required'] = True
        g.guard_entries([a,b], rows, text)
        self.assertTrue(a['eligible_for_name_screening'])
        self.assertEqual(a['raw_content'], 'Own definition.')

    def test_inline_subject_candidate_does_not_quarantine_real_entry(self):
        text = '## XOR\n\nA Boolean operator.\n\nXOR is useful in circuits.'
        rows = g.annotate(v2.structural_units(text))
        real = record(text, 'XOR', 'A Boolean')
        fake = record(text, 'XOR', None, head_start=text.index('XOR is'))
        g.guard_entries([real,fake], rows, text)
        self.assertTrue(real['eligible_for_name_screening'])
        self.assertEqual(real['raw_content'], text[text.index('A Boolean'):])

    def test_full_body_sentence_candidate_does_not_quarantine_real_entry(self):
        text = '## NAME\n\nA complete explanation. More detail.'
        rows = g.annotate(v2.structural_units(text))
        real = record(text, 'NAME', 'A complete')
        fake = record(text, 'A complete explanation.', None)
        g.guard_entries([real,fake], rows, text)
        self.assertTrue(real['eligible_for_name_screening'])
        self.assertEqual(real['raw_content'], 'A complete explanation. More detail.')

    def test_weak_inline_head_evidence_is_not_quarantine_proof(self):
        text = 'Simon, Abbey (b. New York City, 1922). American pianist.\n'
        e = record(text, 'Simon, Abbey (b. New York City, 1922).', 'American pianist.')
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(e['eligible_for_name_screening'])

    def test_duplicate_candidates_do_not_create_a_foreign_head(self):
        text = '## ALPHA\n\nA real definition.'
        e = record(text, 'ALPHA', 'A real')
        g.guard_entries([e,copy.deepcopy(e)], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(e['eligible_for_name_screening'])

    def test_merged_bad_head_does_not_quarantine_correct_translation(self):
        text = 'AADC (= advanced computer) Translation\n\nAADEOS (= advanced sensor) Translation\n'
        good = record(text, 'AADEOS (= advanced sensor) Translation', None)
        bad = record(text, 'AADC (= advanced computer) Translation', None)
        bad['source']['head_spans'].extend(good['source']['head_spans'])
        g.guard_entries([bad,good], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(good['eligible_for_name_screening'])
        self.assertFalse(bad['eligible_for_name_screening'])

    def test_numbered_acronym_senses_are_not_foreign_heads(self):
        text = 'AC \u2460(=address counter) Translation\n\n\u2461(=adjacent channel) Translation\n'
        e = record(text, 'AC \u2460(=address counter) Translation', None)
        a = text.index('\u2461')
        e['source']['head_spans'].append((a, len(text)-1))
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(e['eligible_for_name_screening'])

    def test_body_overlap_alone_is_not_quarantine(self):
        text = '## ALPHA\n\nShared definition.\n\n## BETA\n'
        a = record(text, 'ALPHA', 'Shared', text.index('\n\n## BETA'))
        b = record(text, 'BETA', 'Shared', text.index('\n\n## BETA'))
        a['structural_review_required'] = b['structural_review_required'] = True
        g.guard_entries([a,b], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(a['eligible_for_name_screening'])
        self.assertTrue(b['eligible_for_name_screening'])

    def test_foreign_markdown_head_in_selected_body_is_quarantined(self):
        text = '## ALPHA\n\nOwn text.\n\n## BETA\n\nForeign text.'
        a = record(text, 'ALPHA', 'Own')
        b = record(text, 'BETA', 'Foreign')
        g.guard_entries([a,b], g.annotate(v2.structural_units(text)), text)
        self.assertIn('other_entry_head_in_body', a['extraction_risks'])
        self.assertFalse(a['eligible_for_name_screening'])
        self.assertEqual(a['raw_content'], text[text.index('Own'):])

    def test_numbered_bilingual_foreign_inline_entry_is_quarantined(self):
        text = ('## PROBE\n\n\u7535\u5b50\u675f\u76f4\u5f84\u4e3a\n\n'
                '04.379 \u663e\u5fae\u955c microscope \u72ec\u7acb\u5b9a\u4e49\u3002\n\n'
                '\u7eb3\u7c73\u7ea7\u3002')
        a = record(text, 'PROBE', '\u7535\u5b50\u675f')
        g.guard_entries([a], g.annotate(v2.structural_units(text)), text)
        self.assertFalse(a['eligible_for_name_screening'])

    def test_inline_dictionary_foreign_entry_is_quarantined(self):
        text = ('## 8-bit\n\n1. adj. A byte bus.\n\n'
                '68k (sixty-eight kay) abbr. See 680x0.\n\n2. adj. A byte circuit.')
        e = record(text, '8-bit', '1. adj.')
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertFalse(e['eligible_for_name_screening'])

    def test_selected_definition_near_interleaving_is_not_isolated(self):
        text = ('Broken para-\n\n## FOREIGN\n\ngraph continues.\n\n') * 3
        text += '## CLEAN\n\nA correct complete definition.'
        e = record(text, 'CLEAN', 'A correct')
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertTrue(e['eligible_for_name_screening'])

    def test_selected_interruption_is_isolated(self):
        text = '## ALPHA\n\nBroken para-\n\n## FOREIGN\n\ngraph continues.'
        e = record(text, 'ALPHA', 'Broken')
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertFalse(e['eligible_for_name_screening'])

    def test_three_unrelated_adjacent_pairs_are_not_subtitle_evidence(self):
        text = ''.join('## \u679c\u7269%d (fruit%d)\n\n## \u90bb\u6761%d\n\n\u5b9a\u4e49\u3002\n\n'
                       % (i,i,i) for i in range(3))
        rows = g.annotate(v2.structural_units(text))
        self.assertFalse(any(r.get('head_role')=='internal_after_bilingual_main' for r in rows))

    def test_deeper_subtitle_has_hierarchy_evidence(self):
        text = '## \u4e66\u7c4d (Books)\n\n### \u5386\u53f2\n\n\u5b9a\u4e49\u3002'
        rows = g.annotate(v2.structural_units(text))
        e = record(text, '\u5386\u53f2', '\u5b9a\u4e49')
        g.guard_entries([e], rows, text)
        self.assertFalse(e['eligible_for_name_screening'])

    def test_dictionary_chapter_and_category_do_not_make_entries_subtitles(self):
        for parent in ('Dictionary', 'Chapter 2', 'Plant category',
                       '\u690d\u7269\u7c7b (Plant category)'):
            with self.subTest(parent=parent):
                text = '# ' + parent + '\n\n## \u82f9\u679c (apple)\n\nA fruit.'
                rows = g.annotate(v2.structural_units(text))
                e = record(text, '\u82f9\u679c (apple)', 'A fruit')
                g.guard_entries([e], rows, text)
                self.assertTrue(e['eligible_for_name_screening'])

    def test_internal_subtitle_candidate_does_not_poison_parent(self):
        text = '## \u4e66\u7c4d (Books)\n\nFirst.\n\n### \u5386\u53f2\n\nSecond.'
        rows = g.annotate(v2.structural_units(text))
        parent = record(text, '\u4e66\u7c4d (Books)', 'First.')
        child = record(text, '\u5386\u53f2', 'Second.')
        g.guard_entries([parent,child], rows, text)
        self.assertTrue(parent['eligible_for_name_screening'])
        self.assertFalse(child['eligible_for_name_screening'])

    def test_repeated_same_subtitle_under_distinct_mains_is_evidence(self):
        text = ''.join('## \u4e3b\u9898%d (Topic%d)\n\n## \u6982\u8ff0\n\n\u5b9a\u4e49\u3002\n\n'
                       % (i,i) for i in range(3))
        rows = g.annotate(v2.structural_units(text))
        self.assertEqual(sum(r.get('head_role')=='internal_after_bilingual_main' for r in rows), 3)

    def test_unknown_existing_risk_is_preserved(self):
        text = '## ALPHA\n\nA definition.'
        e = record(text, 'ALPHA', 'A definition')
        e['extraction_risks'] = ['unresolved_source_ownership']
        g.guard_entries([e], g.annotate(v2.structural_units(text)), text)
        self.assertIn('unresolved_source_ownership', e['extraction_risks'])
        self.assertFalse(e['eligible_for_name_screening'])

    def test_split_name_is_exact_and_logged(self):
        text = ('## epoxy powder \u5851\n\n\u5c01\u7528\u73af\u6c27\u6811\u8102\u7c89\n\n'
                '\u4e00\u79cd\u6750\u6599\u3002\n\n\u7d22\u5f15\uff1a\u5851\u5c01\u7528\u73af\u6c27\u6811\u8102\u7c89\n')
        e = record(text, 'epoxy powder \u5851', '\u5c01\u7528', text.index('\u7d22\u5f15'))
        e.update(name='\u5851', knowledge_point='epoxy powder')
        g.repair_split_name(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['name'], '\u5851\u5c01\u7528\u73af\u6c27\u6811\u8102\u7c89')
        self.assertTrue(e.get('body_cleanup_removed'))
        self.assertEqual(e['raw_content'], '\n\n'.join(text[a:b] for a,b in e['source']['body_spans']))

    def test_short_chinese_body_is_not_repaired_into_name(self):
        text = '## water \u6c34\n\n\u65e0\u8272\u900f\u660e\n'
        e = record(text, 'water \u6c34', '\u65e0\u8272')
        e.update(name='\u6c34', knowledge_point='water')
        g.repair_split_name(e, g.annotate(v2.structural_units(text)), text)
        self.assertEqual(e['name'], '\u6c34')

    def test_packet_retains_forward_context_separately(self):
        text = '## ALPHA\n\n\u7f57\u9a6c\u5929\n\n\u4e3b\u6559\u7684\u89c2\u70b9\u3002\n\n## BETA\n'
        rows = g.annotate(v2.structural_units(text))
        hi = next(r['unit'] for r in rows if '\u4e3b\u6559' in r['text'])
        part = g.packet(rows, 0, hi, 4)
        self.assertTrue(any('\u4e3b\u6559' in r['text'] for r in part['units']))
        self.assertEqual(part['hi'], hi)

    def test_body_mention_points_to_real_heading(self):
        rows = g.annotate(v2.structural_units('## LOGIC GATE\n\nA logic gate is a device.\n'))
        self.assertEqual(g.preceding_same_heading({'unit':2, 'quote':'logic gate'}, rows), 0)

    def test_index_letters_remain_excluded(self):
        rows = g.annotate(v2.structural_units(
            '## \u6c49\u82f1\u672f\u8bed\u7d22\u5f15\n\n## A\n\nalpha 1.2\n\n## B\n\nbeta 2.3\n\n## \u6b63\u6587\n\nA definition.'))
        self.assertTrue(next(r for r in rows if 'beta' in r['text']).get('excluded_zone'))
        self.assertFalse(next(r for r in rows if 'A definition' in r['text']).get('excluded_zone'))


class Audit300Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        audit = Path(os.environ.get('FULLBOOK_AUDIT_ROOT',
                     str(Path(__file__).parent / 'audit300_171514')))
        if not (audit / 'samples.json').is_file():
            raise unittest.SkipTest('Audit fixtures unavailable; set FULLBOOK_AUDIT_ROOT')
        cls.samples = {s['sample_id']:s for s in json.loads((audit/'samples.json').read_text(encoding='utf-8'))}
        cls.reviews = {r['sample_id']:r for path in audit.glob('review*.json')
                       for r in json.loads(path.read_text(encoding='utf-8'))}
        cls.books = {}
        for sample in cls.samples.values():
            if sample['book'] in cls.books:
                continue
            source = sample['entry']['source']
            path = Path(source['md_path'])
            if not path.is_file():
                raise unittest.SkipTest('Original NAS Markdown unavailable')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != source['md_sha256']:
                raise AssertionError('Audit Markdown hash changed: ' + sample['book'])
            text = raw.decode('utf-8-sig')
            folder = next((p.parent for p in (audit.parent/sample['phase']).glob('*/SUMMARY.json')
                           if json.loads(p.read_text())['identifier']==sample['book']), None)
            if folder is None:
                raise AssertionError('Missing original audit book folder')
            rows = g.annotate(json.loads((folder/'units.json').read_text()))
            entries = json.loads((folder/'entries.json').read_text())
            cls.books[sample['book']] = text, rows, entries

    def replay(self, sample_id):
        sample = self.samples[sample_id]
        text, rows, originals = self.books[sample['book']]
        entries = copy.deepcopy(originals)
        target = copy.deepcopy(sample['entry'])
        entries = [e for e in entries if e.get('id') != target['id']] + [target]
        cleanup(target, rows, text)
        g.guard_entries(entries, rows, text)
        self.assertEqual(target['raw_content'], '\n\n'.join(text[a:b] for a,b in target['source']['body_spans']))
        return target

    def test_A201_A224_actual_foreign_entries_remain_quarantined(self):
        for sid in ('A201','A224'):
            with self.subTest(sample=sid):
                e = self.replay(sid)
                self.assertFalse(e['eligible_for_name_screening'])
                self.assertIn('other_entry_head_in_body', e['extraction_risks'])

    def test_all_300_source_invariants_with_all_frozen_neighbors(self):
        for book, (text, rows, originals) in self.books.items():
            samples = [s for s in self.samples.values() if s['book']==book]
            ids = {s['entry']['id'] for s in samples}
            entries = [copy.deepcopy(e) for e in originals if e['id'] not in ids]
            targets = [(s, copy.deepcopy(s['entry'])) for s in samples]
            units_hash = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            for _, entry in targets:
                cleanup(entry, rows, text)
                entries.append(entry)
            g.guard_entries(entries, rows, text)
            self.assertEqual(units_hash, hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
            for sample, entry in targets:
                sid = sample['sample_id']
                with self.subTest(sample=sid):
                    spans = entry['source']['body_spans']
                    self.assertEqual(entry['raw_content'], '\n\n'.join(text[a:b] for a,b in spans))
                    self.assertTrue(all(any(c<=a and b<=d for c,d in sample['entry']['source']['body_spans'])
                                        for a,b in spans))
                    for removal in entry.get('body_cleanup_removed', []):
                        if 'text' in removal:
                            self.assertEqual(removal['text'], text[removal['start']:removal['end']])
                    for key in ('body_context_before','body_context_after'):
                        self.assertEqual(entry[key]['text'], ''.join(text[a:b] for a,b in entry[key]['spans']))
                    if self.reviews[sid]['judgment']=='ok':
                        self.assertTrue(entry['eligible_for_name_screening'], entry.get('extraction_risks'))
                        self.assertEqual(entry['raw_content'], sample['entry']['raw_content'])

    def test_A203_A208_A209_A213_A221_A225_false_positives(self):
        for sid in ('A203','A208','A209','A213','A221','A225'):
            with self.subTest(sample=sid):
                e = self.replay(sid)
                self.assertTrue(e['eligible_for_name_screening'], e.get('extraction_risks'))
                self.assertEqual(e['raw_content'], self.samples[sid]['entry']['raw_content'])

    def test_healthy_preliminary_replay_quarantines_are_not_perpetuated(self):
        for sid in ('A018','A032','A081','A085','A095','A106','A116','A165',
                    'A190','A237','A269','A300','A009','A030','A051','A072',
                    'A093','A114','A135','A156','A177','A198','A256','A277','A298',
                    'A108','A171','A192','A271','A031','A048'):
            with self.subTest(sample=sid):
                e = self.replay(sid)
                self.assertTrue(e['eligible_for_name_screening'], e.get('extraction_risks'))
                self.assertEqual(e['raw_content'], self.samples[sid]['entry']['raw_content'])

    def test_confirmed_truncated_bodies_end_on_complete_sentences(self):
        for sid in ('A050','A053','A077','A105','A197','A285'):
            with self.subTest(sample=sid):
                e = self.replay(sid)
                self.assertNotEqual(e['raw_content'], self.samples[sid]['entry']['raw_content'])
                self.assertFalse(e['body_complete'])
                self.assertTrue(e['body_cleanup_removed'])

    def test_A104_A267_A288_evidenced_same_sentence_captions(self):
        for sid, caption in (('A104','Urban graffiti.'), ('A267','Photo Jonas Dovydenas.'),
                             ('A288','Ambrose Roanhorse, Navajo')):
            with self.subTest(sample=sid):
                e = self.replay(sid)
                self.assertFalse(caption in e['raw_content'], sid + ': caption retained')
                self.assertTrue(any(r['reason']=='evidenced_inline_caption'
                                    for r in e['body_cleanup_removed']))


if __name__ == '__main__':
    unittest.main()
