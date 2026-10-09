import unittest
from structure_v6 import candidates, scan, sample_md, validate


class StructureV6Tests(unittest.TestCase):
    def test_bracket_head_recognized(self):
        self.assertEqual(candidates(['【词条】这是解释。'])[0]['family'],'bracket_head')

    def test_language_ratio_not_a_label(self):
        info=scan(['中文 English']*50)
        self.assertGreater(info['latin_char_fraction'],0)
        self.assertNotIn('bilingual_pairs',info)

    def test_stats_cover_later_body(self):
        rows=['前言']*510+['【词条】①解释，即另一个词条。']*40
        info=scan(rows)
        self.assertEqual(info['bracket_head_lines'],40)
        self.assertEqual(info['front_500']['bracket_head_lines'],0)

    def test_stats_driven_sampling_preserves_lines(self):
        rows=['Contents']+['item ........ 34']*150
        rows+=sum(([f'【词{i}】释义。'+'说明'*40,''] for i in range(500)),[])
        windows=sample_md(rows)
        targeted=[w for w in windows if w['kind']=='targeted']
        self.assertTrue(targeted)
        self.assertTrue(all(w['candidate']['line']>150 for w in targeted))
        for w in windows:
            for row in w['lines']:self.assertEqual(row['text'],rows[int(row['id'].split(':')[1])-1])

    def make_packet(self):
        windows=[];records=[]
        for i in range(2):
            h=f'md:{i*2+1}';b=f'md:{i*2+2}'
            windows.append({'window_id':str(i),'zone':i,'kind':'targeted','lines':[{'id':h,'text':'【甲】'},{'id':b,'text':'甲的释义。'}]})
            e={'line_id':h,'quote':'【甲】'}
            records.append({'window_id':str(i),'region':'entry_body','organization':'O1','md_position':'standalone','md_usable':True,
                'entries':[{'head':e,'body':{'line_id':b,'quote':'甲的释义。'}}],
                'structure_tags':[{'tag':'bracket_headwords','evidence':[e]}],'quality_issues':[]})
        return windows,records

    def test_tags_require_repeated_regions(self):
        windows,records=self.make_packet()
        self.assertIn('bracket_headwords',validate({'windows':records},windows)['structure_labels'])
        records[1]['structure_tags']=[]
        self.assertNotIn('bracket_headwords',validate({'windows':records},windows)['structure_labels'])

    def test_fabricated_tag_evidence_rejected(self):
        windows,records=self.make_packet()
        records[0]['structure_tags'][0]['evidence'][0]['quote']='不存在'
        with self.assertRaises(ValueError):validate({'windows':records},windows)

    def test_noise_is_not_book_type(self):
        windows,records=self.make_packet()
        records[0]['region']='index'
        result=validate({'windows':records},windows)
        self.assertNotIn('bracket_headwords',result['structure_labels'])

    def test_unknown_tag_rejected(self):
        windows,records=self.make_packet();records[0]['structure_tags'][0]['tag']='made_up'
        with self.assertRaises(ValueError):validate({'windows':records},windows)

    def test_invalid_extra_evidence_does_not_discard_valid_main_type(self):
        windows,records=self.make_packet()
        for r in records:
            r['quality_issues']=[{'tag':'table_flattened','evidence':[{'line_id':r['entries'][0]['head']['line_id'],'quote':'made up...'}]}]
        result=validate({'windows':records},windows)
        self.assertEqual(result['status'],'sample_classified')
        self.assertEqual(result['quality_issues'],[])
        self.assertEqual(result['annotation_status'],'review')
        self.assertEqual(len(result['rejected_annotations']),2)

    def test_intact_html_table_not_reported_as_flattened(self):
        windows,records=self.make_packet()
        html='<table><tr><td>A</td><td>甲</td></tr></table>'
        windows[0]['lines'].append({'id':'md:99','text':html})
        records[0]['quality_issues']=[{'tag':'table_flattened','evidence':[{'line_id':'md:99','quote':'<table>'}]}]
        result=validate({'windows':records},windows)
        self.assertEqual(result['quality_issues'],[])
        self.assertEqual(result['rejected_annotations'][0]['reason'],'intact_html_table_not_structure_loss')

    def test_commentary_not_encyclopedia_only_because_long(self):
        windows,records=self.make_packet()
        for r in records:
            r['organization']='O4'
            r['structure_tags']=[{'tag':'encyclopedic_entries','evidence':[dict(r['entries'][0]['head'])]}]
        result=validate({'windows':records},windows)
        self.assertNotIn('encyclopedic_entries',result['structure_labels'])
        self.assertEqual(len(result['rejected_annotations']),2)
