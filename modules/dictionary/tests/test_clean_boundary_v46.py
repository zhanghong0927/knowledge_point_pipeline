import unittest
try:
    import clean_boundary_v46 as m
except ImportError:
    m = None


class Gates(unittest.TestCase):
    def vote(self):
        return {'record_decision': 'keep', 'fields': {f: 'keep' for f in ('definition','en_definition','description','en_description')},
                'entry_check': {'kind': 'independent', 'name_complete': True, 'evidence': '## Alpha'},
                'definition_checks': {'en_definition': {'role': 'definition', 'needs_prior': False,
                     'identifies_subject': True, 'evidence': 'Alpha is a category.'}}}

    def packet(self):
        return {'subject': 'Alpha', 'text_fields': {'en_definition': 'Alpha is a category.', 'en_description': 'Alpha is a category.'},
                'source_context': {'head_context': [{'line':1,'text':'## Alpha'}]}, 'raw_content': 'Alpha is a category.'}

    def test_good_definition_unchanged(self):
        self.assertIsNotNone(m)
        vote=self.vote(); got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['record_decision'],'keep')
        self.assertEqual(got['fields']['en_definition'],'keep')

    def test_background_clears_definition_only(self):
        self.assertIsNotNone(m)
        vote=self.vote();vote['definition_checks']['en_definition']['identifies_subject']=False
        got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['fields']['en_definition'],'drop')
        self.assertEqual(got['fields']['en_description'],'keep')

    def test_verified_subsection_rejected(self):
        self.assertIsNotNone(m)
        vote=self.vote();vote['entry_check']['kind']='subsection'
        got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['record_decision'],'drop')

    def test_unverified_entry_is_review_not_content_drop(self):
        self.assertIsNotNone(m)
        vote=self.vote();vote['entry_check']['evidence']='not present'
        got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['record_decision'],'review')

    def test_missing_name_part_not_rewritten(self):
        self.assertIsNotNone(m)
        vote=self.vote();vote['entry_check']['name_complete']=False
        got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['record_decision'],'drop')
        self.assertEqual(self.packet()['subject'],'Alpha')

    def test_literal_missing_definition_evidence_rejected(self):
        self.assertIsNotNone(m)
        vote=self.vote();vote['definition_checks']['en_definition']['evidence']='invented'
        got,_=m.strict_vote(vote,self.packet())
        self.assertEqual(got['fields']['en_definition'],'drop')

    def test_enrichment_preserves_body_and_exposes_caption(self):
        text='## Parent\n\n## Alpha\n\nOpening text.\n\n![](fig.jpg)\n\nShort caption\n\nMore text.'
        a=text.index('Alpha');b=text.index('Opening')
        row={'raw_content':text[b:], 'source':{'head_spans':[[a,a+5]],'body_spans':[[b,len(text)]]}}
        result=m.enrich_source(row,text)
        self.assertEqual(result['raw_content'],row['raw_content'])
        self.assertEqual(result['source'],row['source'])
        self.assertNotIn('source_context',row)
        self.assertIn('Short caption',result['source_context']['image_caption_candidates'])
        self.assertTrue(any(h['text']=='## Parent' for h in result['source_context']['heading_history']))

    def test_paragraph_after_image_not_blanket_removed(self):
        text='## Alpha\n\n![](fig.jpg)\n\nThis is a complete explanatory paragraph.'
        row={'raw_content':text,'source':{'head_spans':[[3,8]],'body_spans':[[0,len(text)]]}}
        result=m.enrich_source(row,text)
        self.assertEqual(result['source_context']['image_caption_candidates'],[])


if __name__=='__main__': unittest.main()
