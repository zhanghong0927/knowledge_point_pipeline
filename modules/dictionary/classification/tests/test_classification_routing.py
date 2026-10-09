import copy
import unittest
from classification_routing import project


def sample():
    ws=[];rs=[]
    for n in [1,101]:
        h={'line_id':f'md:{n}','quote':f'TERM{n}'}
        b={'line_id':f'md:{n+1}','quote':'Definition and discussion.'}
        ws.append({'window_id':str(n),'zone':n//100,'kind':'targeted',
                   'lines':[{'id':h['line_id'],'text':'## '+h['quote']},
                            {'id':b['line_id'],'text':b['quote']}]})
        rs.append({'window_id':str(n),'region':'entry_body','organization':'O1',
                   'entries':[{'head':h,'body':b,'role':'main_entry'}],
                   'structure_tags':[],'md_usable':True})
    return {'windows':rs},ws


class Routing(unittest.TestCase):
    def test_identical_head_body_cannot_supply_structure_support(self):
        v,w=sample()
        v['windows'][0]['entries'][0]['body']=dict(v['windows'][0]['entries'][0]['head'])
        result=project(v,w)
        self.assertEqual(result['routing_status'],'review')
        self.assertEqual(len(result['support']),1)
        self.assertIn('duplicate_head_body',[x['reason'] for x in result['warnings']])

    def test_definition_repeating_head_with_more_text_is_valid(self):
        v,w=sample()
        w[0]['lines'][1]['text']='TERM1 is a defined concept.'
        v['windows'][0]['entries'][0]['body']['quote']='TERM1 is a defined concept.'
        self.assertEqual(project(v,w)['routing_status'],'sample_supported')

    def test_bad_boundary_quote_excludes_window_not_entire_book(self):
        v,w=sample()
        for r in v['windows']:
            r['boundary_contract']={'status':'supported','reason':'Text block.',
                'evidence':[dict(r['entries'][0]['head'])]}
        v['windows'][0]['boundary_contract']['evidence'][0]['quote']='not in source'
        result=project(v,w,require_boundary=True)
        self.assertEqual(result['routing_status'],'review')
        self.assertEqual(len(result['support']),1)
        self.assertIn('invalid_boundary_evidence',[x['reason'] for x in result['warnings']])

    def test_not_body_internal_sections_excluded_from_denominator(self):
        v,w=sample()
        for r in v['windows']:
            r['boundary_contract']={'status':'unsupported','reason':'Unsupported units.',
                'evidence':[r['entries'][0]['head']]}
        v['applicability']={'decision':'other','reason':'Unsupported main body.',
            'evidence':[{'window_id':win['window_id'],'line_id':win['lines'][0]['id'],
                         'quote':win['lines'][0]['text']} for win in w]}
        for n in range(2):
            win=copy.deepcopy(w[0]);win['window_id']='preface'+str(n);w.append(win)
            v['windows'].append({'window_id':win['window_id'],'region':'internal_section',
                'boundary_contract':{'status':'not_body','reason':'Preface.','evidence':[]}})
        self.assertEqual(project(v,w,require_boundary=True)['routing_status'],'other')

    def test_evidenced_unsupported_body_becomes_other(self):
        v,w=sample()
        for r in v['windows']:
            r['boundary_contract']={'status':'unsupported','reason':'Relations outside text blocks.',
                'evidence':[r['entries'][0]['head']]}
        v['applicability']={'decision':'other','reason':'Unsupported dominant body units.',
            'evidence':[{'window_id':win['window_id'],'line_id':win['lines'][0]['id'],
                         'quote':win['lines'][0]['text']} for win in w]}
        self.assertEqual(project(v,w,require_boundary=True)['routing_status'],'other')

    def test_single_unsupported_window_does_not_force_other(self):
        v,w=sample()
        for i,r in enumerate(v['windows']):
            r['boundary_contract']={'status':'unsupported' if i==0 else 'supported',
                'reason':'Mixed units.','evidence':[r['entries'][0]['head']]}
        v['applicability']={'decision':'other','reason':'Mixed units.',
            'evidence':[{'window_id':win['window_id'],'line_id':win['lines'][0]['id'],
                         'quote':win['lines'][0]['text']} for win in w]}
        self.assertEqual(project(v,w,require_boundary=True)['routing_status'],'review')

    def test_boundary_contract_required_for_new_run(self):
        v,w=sample()
        with self.assertRaisesRegex(ValueError,'boundary_contract'):
            project(v,w,require_boundary=True)

    def test_unsupported_units_cannot_support_normal_family(self):
        v,w=sample()
        for r in v['windows']:
            r['boundary_contract']={'status':'unsupported','reason':'External relationships required.',
                'evidence':[r['entries'][0]['head']]}
        v['applicability']={'decision':'compatible'}
        result=project(v,w,require_boundary=True)
        self.assertEqual(result['routing_status'],'review')
        self.assertEqual(result['support'],[])

    def test_other_requires_main_body_incompatibility(self):
        v,w=sample()
        for r in v['windows']:
            r['region']='index'
            r['boundary_contract']={'status':'not_body','reason':'Index.','evidence':[]}
        v['applicability']={'decision':'other','reason':'Index alone.',
            'evidence':[{'window_id':win['window_id'],'line_id':win['lines'][0]['id'],
                         'quote':win['lines'][0]['text']} for win in w]}
        self.assertEqual(project(v,w,require_boundary=True)['routing_status'],'review')

    def test_supported_contract_preserves_classification(self):
        v,w=sample()
        for r in v['windows']:
            r['boundary_contract']={'status':'supported','reason':'Head then text.',
                'evidence':[r['entries'][0]['head']]}
        self.assertEqual(project(v,w,require_boundary=True)['routing_status'],'sample_supported')

    def test_large_italic_is_not_bold(self):
        v,w=sample()
        w[0]['lines'][0]['pdf_format']=[{'spans':[{'text':'TERM1','bold':False,'italic':True,'size_ratio':3}]}]
        v['windows'][0]['structure_tags']=[{'tag':'bold_heads','evidence':[{'line_id':'md:1','quote':'TERM1'}]}]
        self.assertEqual(project(v,w)['validated_optional_annotations'],[])

    def test_pdf_bold_head_retained(self):
        v,w=sample()
        w[0]['lines'][0]['pdf_format']=[{'spans':[{'text':'TERM1','bold':True}]}]
        v['windows'][0]['structure_tags']=[{'tag':'bold_heads','evidence':[{'line_id':'md:1','quote':'TERM1'}]}]
        self.assertEqual(len(project(v,w)['validated_optional_annotations']),1)

    def test_head_tag_cannot_cite_internal_field(self):
        v,w=sample()
        w[0]['lines'][1]['text']='Definition and discussion. 【释义】'
        v['windows'][0]['structure_tags']=[{'tag':'bracket_headwords','evidence':[{'line_id':'md:2','quote':'【释义】'}]}]
        r=project(v,w)
        self.assertEqual(r['validated_optional_annotations'],[])
        self.assertEqual(r['routing_status'],'sample_supported')

    def test_head_tag_cannot_cite_body_on_same_line(self):
        v,w=sample()
        w[0]['lines'][0]['text']='TERM1 Definition and discussion. 【释义】'
        v['windows'][0]['entries'][0]['body']['line_id']='md:1'
        v['windows'][0]['structure_tags']=[{'tag':'bracket_headwords','evidence':[{'line_id':'md:1','quote':'【释义】'}]}]
        self.assertEqual(project(v,w)['validated_optional_annotations'],[])

    def test_actual_bracket_head_is_retained(self):
        v,w=sample();w[0]['lines'][0]['text']='## 【TERM1】'
        v['windows'][0]['structure_tags']=[{'tag':'bracket_headwords','evidence':[{'line_id':'md:1','quote':'【TERM1】'}]}]
        self.assertEqual(len(project(v,w)['validated_optional_annotations']),1)

    def test_number_tag_needs_actual_head_number(self):
        v,w=sample()
        v['windows'][0]['structure_tags']=[{'tag':'numbered_heads','evidence':[{'line_id':'md:1','quote':'TERM1'}]}]
        self.assertEqual(project(v,w)['validated_optional_annotations'],[])

    def test_normal(self):
        v,w=sample();r=project(v,w)
        self.assertEqual(r['routing_status'],'sample_supported')
        self.assertEqual(r['head_position'],'standalone')

    def test_optional_tag_is_local_error(self):
        v,w=sample();v['windows'][0]['structure_tags']=[{'tag':'illegal'}]
        r=project(v,w);self.assertEqual(r['routing_status'],'sample_supported')
        self.assertTrue(r['warnings'])

    def test_invalid_head_does_not_count(self):
        v,w=sample();v['windows'][0]['entries'][0]['head']['quote']='invented'
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_internal_heading_does_not_count(self):
        v,w=sample();v['windows'][0]['entries'][0]['role']='internal_heading'
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_same_zone_insufficient(self):
        v,w=sample();w[1]['zone']=w[0]['zone']
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_duplicate_anchor_insufficient(self):
        v,w=sample();w[1]['lines']=copy.deepcopy(w[0]['lines'])
        v['windows'][1]['entries']=copy.deepcopy(v['windows'][0]['entries'])
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_o1_o2_same_boundary_family_not_same_subtype(self):
        v,w=sample();v['windows'][1]['organization']='O2'
        r=project(v,w)
        self.assertEqual(r['routing_status'],'sample_supported')
        self.assertEqual(r['organization_status'],'review')
        self.assertEqual(r['family'],'entry_prose')

    def test_damage_separate_from_type(self):
        v,w=sample();v['windows'][0]['md_usable']=False
        r=project(v,w)
        self.assertEqual(r['routing_status'],'sample_supported')
        self.assertEqual(r['md_quality_status'],'review')
        self.assertFalse(r['ready_for_extraction'])

    def test_legacy_roles_not_verified(self):
        v,w=sample()
        for r in v['windows']:r['entries'][0].pop('role')
        self.assertEqual(project(v,w)['routing_status'],'legacy_candidate_unverified')

    def test_mixed_family_not_silently_merged(self):
        v,w=sample();v['windows'][1]['organization']='O4'
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_window_mismatch_is_technical(self):
        v,w=sample();v['windows'].pop()
        with self.assertRaises(ValueError):project(v,w)

    def test_input_unchanged(self):
        v,w=sample();before=copy.deepcopy((v,w));project(v,w)
        self.assertEqual((v,w),before)

    def test_minority_family_head_position_is_not_lost(self):
        v,w=sample();v['windows'][1]['organization']='O3'
        e=v['windows'][0]['entries'][0]
        e['body']['line_id']=e['head']['line_id']
        w[0]['lines'][0]['text']='TERM1 Definition and discussion.'
        r=project(v,w)
        self.assertEqual(r['head_position'],'both')
        self.assertIn('fixed_fields',r['family_counts'])

    def test_explicit_o1_o2_ambiguity_keeps_boundary_family(self):
        v,w=sample()
        for r in v['windows']:
            r['organization']='mixed';r['organization_options']=['O1','O2']
        result=project(v,w)
        self.assertEqual(result['routing_status'],'sample_supported')
        self.assertEqual(result['organization_status'],'review')

    def test_other_ambiguity_not_assumed_prose(self):
        v,w=sample()
        for r in v['windows']:
            r['organization']='mixed';r['organization_options']=['O1','O4']
        self.assertEqual(project(v,w)['routing_status'],'review')

    def test_evidenced_incompatibility_is_other(self):
        v,w=sample()
        v['applicability']={'decision':'other','reason':'Requires a different boundary method.',
            'evidence':[{'window_id':win['window_id'],'line_id':win['lines'][0]['id'],
                         'quote':win['lines'][0]['text']} for win in w]}
        r=project(v,w)
        self.assertEqual(r['family'],'other')
        self.assertEqual(r['routing_status'],'other')

    def test_uncertainty_is_not_other(self):
        v,w=sample();v['applicability']={'decision':'uncertain','reason':'Insufficient evidence.','evidence':[]}
        r=project(v,w)
        self.assertEqual(r['routing_status'],'review')
        self.assertNotEqual(r['family'],'other')

    def test_invalid_negative_evidence_cannot_make_other(self):
        v,w=sample();v['applicability']={'decision':'other','reason':'Unsupported.',
            'evidence':[{'window_id':'1','line_id':'md:1','quote':'invented'}]}
        r=project(v,w)
        self.assertNotEqual(r['family'],'other')
        self.assertEqual(r['routing_status'],'review')


if __name__=='__main__':unittest.main()
