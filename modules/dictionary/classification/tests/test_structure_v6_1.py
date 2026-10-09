import copy
import unittest
import structure_v6_1 as core


def fixture():
    windows=[]; records=[]
    for i in range(2):
        head={'line_id':f'md:{i*10+1}','quote':'MAIN TERM'}
        body={'line_id':f'md:{i*10+2}','quote':'A concept with a direct definition.'}
        windows.append({'window_id':f'w{i}','zone':i,'kind':'targeted',
                        'lines':[{'id':head['line_id'],'text':'## MAIN TERM'},
                                 {'id':body['line_id'],'text':body['quote']}]})
        records.append({'window_id':f'w{i}','region':'entry_body','organization':'O1',
                        'md_position':'standalone','md_usable':True,
                        'entries':[{'head':head,'body':body,'role':'main_entry',
                                    'role_reason':'Independent subject, not a subsection.',
                                    'role_evidence':[head]}],
                        'body_assessment':{'pattern':'direct_definition','evidence':[body],
                                           'reason':'Direct meaning only.'},
                        'integrity':{'status':'usable','evidence':[],'reason':'No structural damage.'},
                        'structure_tags':[],'quality_issues':[]})
    return {'windows':records}, windows


class Regression(unittest.TestCase):
    def test_valid_main_entries_remain_supported(self):
        v,w=fixture(); self.assertEqual(core.validate(v,w)['status'],'sample_classified')

    def test_internal_head_cannot_support_book_type(self):
        v,w=fixture()
        for r in v['windows']: r['entries'][0]['role']='internal_heading'
        self.assertEqual(core.validate(v,w)['status'],'review')

    def test_redaction_is_not_a_head(self):
        v,w=fixture()
        for r,win in zip(v['windows'],w):
            r['entries'][0]['head']['quote']='[XXXXXXXXXXXX]'
            win['lines'][0]['text']='## [XXXXXXXXXXXX]'
        self.assertEqual(core.validate(v,w)['status'],'review')

    def test_damaged_body_overrides_model_usable(self):
        v,w=fixture()
        v['windows'][0]['integrity']={'status':'damaged','reason':'Cross-entry body splice.',
                                      'evidence':[v['windows'][0]['entries'][0]['body']]}
        self.assertEqual(core.validate(v,w)['reason'],'md_structure_not_reliably_preserved')

    def test_uncertain_integrity_without_damage_does_not_veto(self):
        v,w=fixture();v['windows'][0]['integrity']['status']='uncertain'
        self.assertEqual(core.validate(v,w)['status'],'sample_classified')

    def test_type_and_quality_are_separate(self):
        v,w=fixture()
        extra=copy.deepcopy(v['windows'][0]);extra['window_id']='w2'
        extra['integrity']={'status':'damaged','evidence':[extra['entries'][0]['body']], 'reason':'Spliced body.'}
        win=copy.deepcopy(w[0]);win.update(window_id='w2',zone=2)
        v['windows'].append(extra);w.append(win)
        r=core.validate(v,w)
        self.assertEqual(r['classification_status'],'sample_classified')
        self.assertEqual(r['md_quality_status'],'review')

    def test_background_exposition_does_not_pass_as_o1(self):
        v,w=fixture()
        for r in v['windows']: r['body_assessment']['pattern']='topic_development'
        self.assertEqual(core.validate(v,w)['status'],'review')

    def test_internal_brackets_cannot_be_head_tag(self):
        v,w=fixture()
        for r,win in zip(v['windows'],w):
            win['lines'].append({'id':f"md:{r['window_id'][1:]}99",'text':'[note]'})
            r['structure_tags']=[{'tag':'bracket_headwords','evidence':[
                {'line_id':win['lines'][-1]['id'],'quote':'[note]'}],'reason':'Brackets.'}]
        self.assertNotIn('bracket_headwords',core.validate(v,w)['structure_labels'])

    def test_context_contains_earlier_appendix_hint(self):
        rows=['## Appendix 1']+['body']*50+['## INTERNAL','text']
        w=[{'window_id':'w0','lines':[{'id':'md:52','text':rows[51]}]}]
        result=core.add_context(w,rows)
        self.assertEqual(result[0]['section_hint']['id'],'md:1')
        self.assertEqual(result[0]['lines'],w[0]['lines'])

    def test_invalid_new_evidence_is_technical_failure(self):
        v,w=fixture();v['windows'][0]['entries'][0]['role_evidence'][0]['quote']='invented'
        with self.assertRaises(ValueError):core.validate(v,w)


if __name__=='__main__':unittest.main()
