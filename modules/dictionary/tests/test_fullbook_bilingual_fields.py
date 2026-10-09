import copy
import importlib.util
import unittest

if importlib.util.find_spec('fullbook_bilingual_fields'):
    from fullbook_bilingual_fields import repair_bilingual_fields as repair
else:
    def repair(entry, text):
        return False


def fixture(zh='\u6e29\u5ea6\u4f20\u611f\u5668', alternating=False):
    pairs = [('pressure sensor', '\u538b\u529b\u4f20\u611f\u5668'),
             ('light sensor', '\u5149\u4f20\u611f\u5668'), ('temperature sensor', zh),
             ('current sensor', '\u7535\u6d41\u4f20\u611f\u5668'),
             ('sound sensor', '\u58f0\u4f20\u611f\u5668')]
    sep = '\n\n' if alternating else ' '
    text = '\n\n'.join(en + sep + value for en, value in pairs) + '\n'
    en = 'temperature sensor'
    a = text.index(en)
    b = text.index(zh, a + len(en))
    entry = {'id': 'stable', 'knowledge_point': en, 'name': '', 'head': en,
             'raw_content': zh, 'body_complete': True,
             'source': {'head_spans': [[a, a + len(en)]], 'body_spans': [[b, b + len(zh)]]},
             'name_evidence': [[a, a + len(en)]], 'body_evidence': [[b, b + len(zh)]]}
    return entry, text


class BilingualTests(unittest.TestCase):
    def test_inline_move_and_provenance(self):
        e, t = fixture()
        old = copy.deepcopy(e)
        self.assertTrue(repair(e, t))
        self.assertEqual(e['name'], old['raw_content'])
        self.assertEqual(e['raw_content'], '')
        self.assertEqual(e['source']['body_spans'], [])
        self.assertEqual(e['source']['original_body_spans'], old['source']['body_spans'])
        self.assertEqual(e['id'], old['id'])
        self.assertEqual(e['knowledge_point'], old['knowledge_point'])
        self.assertEqual(e['body_evidence'], [])
        self.assertEqual(e['name_evidence'], e['source']['head_spans'])

    def test_alternating(self):
        e, t = fixture(alternating=True)
        self.assertTrue(repair(e, t))

    def test_numbered_synonyms_and_mixed_acronym(self):
        for value in ['(1)\u7fa4\u7a7f\u5b54(2)\u590d\u7a7f\u5b54',
                      'CD-ROM \u76d8\u5e93', '(\u5de5\u7a0b\u7684)\u5de5\u4f5c\u59d4\u6258',
                      '\u52a0\u6c22[\u4f5c\u7528], \u6c22\u5316[\u4f5c\u7528]']:
            with self.subTest(value=value):
                e, t = fixture(value)
                self.assertTrue(repair(e, t))
                self.assertEqual(e['name'], value)

    def test_idempotence(self):
        e, t = fixture()
        repair(e, t)
        old = copy.deepcopy(e)
        self.assertFalse(repair(e, t))
        self.assertEqual(e, old)

    def test_do_not_move_prose(self):
        for value in ['\u4e00\u79cd\u6d4b\u91cf\u6e29\u5ea6\u7684\u4eea\u5668',
                      '\u7528\u4e8e\u6e29\u5ea6\u6d4b\u91cf', '\u5c06\u6e29\u5ea6\u8f6c\u6362\u6210\u7535\u4fe1\u53f7',
                      '\u6e29\u5ea6\u4f20\u611f\u5668\u3002', 'Temperature measuring device.',
                      '\u6e29\u5ea6\u4f20\u611f\u5668\n\n\u7528\u4e8e\u6d4b\u91cf']:
            with self.subTest(value=value):
                e, t = fixture(value)
                old = copy.deepcopy(e)
                self.assertFalse(repair(e, t))
                self.assertEqual(e, old)

    def test_existing_name_untouched(self):
        e, t = fixture()
        e['name'] = '\u6e29\u5ea6\u4f20\u611f\u5668'
        old = copy.deepcopy(e)
        self.assertFalse(repair(e, t))
        self.assertEqual(e, old)

    def test_context_required(self):
        t = 'temperature sensor \u6e29\u5ea6\u4f20\u611f\u5668'
        e = {'knowledge_point': 'temperature sensor', 'name': '', 'raw_content': t[19:],
             'source': {'head_spans': [[0, 18]], 'body_spans': [[19, len(t)]]}}
        old = copy.deepcopy(e)
        self.assertFalse(repair(e, t))
        self.assertEqual(e, old)

    def test_conflicting_duplicate_not_guessed(self):
        e, t = fixture('\u6e29\u5ea6')
        t += '\ntemperature sensor \u6e29\u5ea6\u4f20\u611f\u5668\n'
        old = copy.deepcopy(e)
        self.assertFalse(repair(e, t))
        self.assertEqual(e, old)

    def test_body_must_end_at_line_boundary(self):
        e, t = fixture('\u6e29\u5ea6\u4f20\u611f\u5668')
        e['raw_content'] = '\u6e29\u5ea6'
        e['source']['body_spans'][0][1] = e['source']['body_spans'][0][0] + 2
        self.assertFalse(repair(e, t))

    def test_source_mismatch_rejected(self):
        e, t = fixture()
        e['raw_content'] = '\u4f2a\u9020\u8bd1\u540d'
        self.assertFalse(repair(e, t))


if __name__ == '__main__':
    unittest.main()
