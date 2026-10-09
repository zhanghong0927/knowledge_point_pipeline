import unittest
import fullbook_llm_v2 as v2
import fullbook_v4_structure as g

def record(text,head,start,end):
    a=text.index(head)
    return {'head':head,'knowledge_point':'','name':head,'raw_content':text[start:end],
        'body_complete':True,'source':{'head_spans':[(a,a+len(head))],'body_spans':[(start,end)]}}

class StructureTests(unittest.TestCase):
    def test_caption_is_not_independent_head(self):
        text='## 书籍 (Livres)\n\n### 弗洛伊德的图书馆\n\n书的正文。\n'
        rows=g.annotate(v2.structural_units(text))
        self.assertEqual(next(r for r in rows if '图书馆' in r['text']).get('head_role'),'internal_after_bilingual_main')

    def test_index_letters_do_not_end_section(self):
        text='## 汉英术语索引\n\n## A\n\n安全 safety 1.2.3\n\n## B\n\n边界 boundary 2.3.4\n\n## 正文\n\n真实定义。\n'
        rows=g.annotate(v2.structural_units(text))
        self.assertTrue(next(r for r in rows if 'boundary' in r['text']).get('excluded_zone'))
        self.assertFalse(next(r for r in rows if '真实定义' in r['text']).get('excluded_zone'))

    def test_split_chinese_name_rejoins_exact_source(self):
        text='## epoxy powder 塑\n\n封用环氧树脂粉\n\n一种材料。\n\n索引：塑封用环氧树脂粉\n'
        rows=g.annotate(v2.structural_units(text));a=text.index('封用')
        e=record(text,'epoxy powder 塑',a,text.index('索引'));e['knowledge_point']='epoxy powder';e['name']='塑'
        g.repair_split_name(e,rows,text)
        self.assertEqual(e['name'],'塑封用环氧树脂粉')
        self.assertNotIn('封用环氧树脂粉',e['raw_content'])
        self.assertEqual(e['raw_content'],'\n\n'.join(text[a:b] for a,b in e['source']['body_spans']))

    def test_short_body_is_not_name_continuation(self):
        text='## water 水\n\n无色透明\n'
        e=record(text,'water 水',text.index('无色'),len(text));e.update(knowledge_point='water',name='水')
        g.repair_split_name(e,g.annotate(v2.structural_units(text)),text)
        self.assertEqual(e['name'],'水')
        self.assertEqual(e['raw_content'],'无色透明\n')

    def test_adjacent_peer_is_not_subtitle(self):
        rows=g.annotate(v2.structural_units('## 苹果 (apple)\n\n## 香蕉\n\n一种水果。\n'))
        self.assertNotEqual(next(r for r in rows if '香蕉' in r['text']).get('head_role'),'internal_after_bilingual_main')

    def test_foreign_head_is_quarantined_not_rewritten(self):
        text='## ALPHA\n\nAlpha text.\n\n## BETA\n\nBeta text.\n'
        rows=g.annotate(v2.structural_units(text))
        a=record(text,'ALPHA',text.index('Alpha text'),len(text))
        b=record(text,'BETA',text.index('Beta text'),len(text))
        g.guard_entries([a,b],rows,text)
        self.assertIn('other_entry_head_in_body',a['extraction_risks'])
        self.assertFalse(a['eligible_for_name_screening'])

    def test_internal_heading_without_separate_entry_is_kept(self):
        text='## ALPHA\n\nFirst.\n\n## Applications\n\nSecond.\n'
        a=record(text,'ALPHA',text.index('First'),len(text));g.guard_entries([a],g.annotate(v2.structural_units(text)),text)
        self.assertFalse(a.get('extraction_risks'))

    def test_sentence_overlap_includes_next_paragraph(self):
        text='## ALPHA\n\n罗马天\n\n主教的观点。\n\n## BETA\n'
        rows=v2.structural_units(text);hi=next(r['unit'] for r in rows if '主教' in r['text'])
        part=g.packet(rows,0,hi,4)
        self.assertTrue(any('主教' in r['text'] for r in part['units']))

    def test_systematic_interleaving_is_isolated(self):
        text=''.join(f'## TERM{i}\n\nBody{i}.\n\n' for i in range(12))
        entries=[record(text,f'TERM{i}',text.index(f'Body{i}'),len(text)) for i in range(12)]
        g.guard_entries(entries,g.annotate(v2.structural_units(text)),text)
        self.assertTrue(all('systematic_interleaving_review' in e.get('extraction_risks',[]) for e in entries))

    def test_source_interleaving_detected_without_model_overlaps(self):
        text=''.join(f'Unfinished para-\n\n## HEAD{i}\n\ngraph continues.\n\n' for i in range(3))
        rows=g.annotate(v2.structural_units(text))
        self.assertTrue(any(r.get('document_layout_risk') for r in rows))

    def test_body_mention_points_to_real_heading(self):
        rows=g.annotate(v2.structural_units('## LOGIC GATE\n\nA logic gate is a device.\n'))
        self.assertEqual(g.preceding_same_heading({'unit':2,'quote':'logic gate'},rows),0)

if __name__=='__main__':unittest.main()
