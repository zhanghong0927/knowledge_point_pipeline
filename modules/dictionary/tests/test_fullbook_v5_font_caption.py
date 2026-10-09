import unittest
import fullbook_llm_v2 as v2
import fullbook_v5_structure as structure
from test_fullbook_v5_structure import record


class FontCaptionTests(unittest.TestCase):
    def setup_case(self, distinct=True, image=True, continued=True):
        text='## ALPHA\n\nThe machines differed only'+('' if continued else '.')+'\n\n'
        if image:text+='![](figure.png)\n\n'
        text+='One of the early machines.\n\nin feature sets and price.\n'
        units=structure.annotate(v2.structural_units(text))
        for row in units:
            if row['kind']=='paragraph' and not row['text'].startswith('!['):
                font='CaptionSans' if distinct and row['text'].startswith('One of') else 'BodySerif'
                row['pdf_format']=[{'page':1,'spans':[{'text':row['text'],'font':font,'size_ratio':1.0}]}]
        entry=record(text,'ALPHA','The machines')
        structure.cleanup_body(entry,units,text)
        return entry,text

    def test_distinct_font_caption_inside_continuing_sentence_is_removed(self):
        entry,text=self.setup_case()
        self.assertNotIn('One of the early machines.',entry['raw_content'])
        self.assertIn('in feature sets and price.',entry['raw_content'])
        self.assertEqual(entry['raw_content'],'\n\n'.join(text[a:b] for a,b in entry['source']['body_spans']))

    def test_font_alone_or_image_alone_is_not_deletion_evidence(self):
        for opts in [dict(distinct=False),dict(image=False),dict(continued=False)]:
            with self.subTest(opts=opts):
                entry,_=self.setup_case(**opts)
                self.assertIn('One of the early machines.',entry['raw_content'])


if __name__=='__main__':unittest.main()
