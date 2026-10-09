"""Compositional dictionary structure labels with whole-MD sampling signals."""
import collections
import copy
import re
from html.parser import HTMLParser
import md_primary_v5 as v5

LABELS={
 'classified_wordlist':'分类词目表型', 'category_entries':'类目词条型',
 'bracket_headwords':'【】词头型', 'encyclopedic_entries':'百科长条目型',
 'person_fixed_fields':'人名词典·固定字段型', 'person_narrative':'人名词典·叙述传记型',
 'historical_materials':'史料汇编型', 'structured_handbook':'结构化学科手册型',
 'bilingual_pairs':'双语配对型',
 'alphabetical':'字母编排', 'chronological':'年代编排',
 'bold_heads':'粗体词头', 'numbered_heads':'编号词头',
 'numbered_senses':'编号义项', 'cross_references':'互参条',
 'parallel_inline':'双语行内配对', 'parallel_lines':'双语上下行配对',
 'parallel_table':'双语表格配对', 'fixed_fields':'固定字段',
}
ISSUES={'table_flattened','head_marker_lost','reading_order_suspected',
        'body_missing_suspected','hierarchy_ambiguous','ocr_suspected'}
BRACKET=re.compile(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?【[^】\n]{1,100}】')
DOTS=re.compile(r'\.{3,}|…{2,}')


def intact_table(text):
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__();self.stack=[];self.cells=0;self.tables=0;self.bad=False
        def handle_starttag(self,tag,attrs):
            if tag in ['table','tr','td','th']:
                self.stack.append(tag)
                if tag in ['td','th']:self.cells+=1
                if tag=='table':self.tables+=1
        def handle_endtag(self,tag):
            if tag in ['table','tr','td','th']:
                if not self.stack or self.stack[-1]!=tag:self.bad=True
                else:self.stack.pop()
    parser=TableParser()
    try:parser.feed(text);parser.close()
    except Exception:return False
    return bool(parser.tables and parser.cells>=2 and not parser.stack and not parser.bad)


def candidates(rows):
    pool={c['line']:c for c in v5.candidates(rows)}
    for i,row in enumerate(rows):
        if BRACKET.match(row) and row.strip(' #*【】') not in ['鉴赏','赏析','原文','参考文献']:
            pool[i]={'line':i,'family':'bracket_head','score':5,'text':row[:220]}
    return sorted(pool.values(),key=lambda c:c['line'])


def metrics(rows):
    text='\n'.join(rows);zh=len(re.findall(r'[\u4e00-\u9fff]',text));en=len(re.findall(r'[A-Za-z]',text))
    paragraphs=[len(p.strip()) for p in re.split(r'\n\s*\n',text) if p.strip()]
    ordered=sorted(paragraphs)
    def q(f):return ordered[int((len(ordered)-1)*f)] if ordered else 0
    return {'lines':len(rows),'chars':len(text),'cjk_chars':zh,'latin_chars':en,
            'latin_char_fraction':round(en/max(1,en+zh),4),
            'paragraph_chars':{'median':q(.5),'p90':q(.9),'max':q(1)},
            'bracket_head_lines':sum(bool(BRACKET.match(r)) for r in rows),
            'dot_leader_lines':sum(bool(DOTS.search(r)) for r in rows),
            'numbered_sense_lines':sum(bool(re.search('[①②③④⑤⑥⑦⑧⑨⑩]',r)) for r in rows),
            'cross_reference_hint_lines':sum(bool(re.search(r'即[\u4e00-\u9fff]|参见|另见|\bsee also\b',r,re.I)) for r in rows),
            'image_markers':len(re.findall(r'!\[|<img\b',text,re.I)),
            'formula_markers':len(re.findall(r'\$\$|\\\[|\\begin\{',text)),
            'html_table_markers':len(re.findall(r'<table\b',text,re.I)),
            'table_like_lines':sum(r.count('|')>=3 for r in rows),
            'replacement_characters':text.count('\ufffd')}


def scan(rows):
    summary=metrics(rows);pool=candidates(rows);n=len(rows)
    summary['candidate_families']=dict(collections.Counter(c['family'] for c in pool))
    summary['front_500']=metrics(rows[:500]);summary['regions']=[]
    for zone in range(12):
        lo=n*zone//12;hi=n*(zone+1)//12;part=rows[lo:hi]
        m=metrics(part)
        m.update(zone=zone,start_line=lo+1,end_line=hi,
                 candidate_families=dict(collections.Counter(c['family'] for c in pool if lo<=c['line']<hi)))
        summary['regions'].append(m)
    return summary


def noise_score(rows,anchor):
    nearby=[r for r in rows[max(0,anchor-8):anchor+15] if r.strip()]
    return sum(bool(DOTS.search(r)) for r in nearby)/max(1,len(nearby))


def sample_md(rows):
    profile=scan(rows);pool=candidates(rows);n=len(rows);chosen=[];used=set()
    # Choose across the whole body, preferring stable signals over dot-leader lists.
    for zone in range(6):
        lo=n*zone//6;hi=n*(zone+1)//6
        local=[c for c in pool if lo<=c['line']<hi and noise_score(rows,c['line'])<.35]
        if not local:continue
        center=(lo+hi)/2
        c=max(local,key=lambda c:(c['score']-4*noise_score(rows,c['line']),-abs(c['line']-center)))
        chosen.append((c['line'],'targeted',zone,c));used.add(c['family'])
    frequent=collections.Counter(c['family'] for c in pool)
    for family,count in frequent.most_common():
        if len(chosen)>=8:break
        if family in used or count<4:continue
        alternatives=[c for c in pool if c['family']==family and noise_score(rows,c['line'])<.35
                      and all(abs(c['line']-a)>100 for a,_,_,_ in chosen)]
        if alternatives:
            c=min(alternatives,key=lambda c:abs(c['line']-n*.5))
            chosen.append((c['line'],'targeted',min(5,c['line']*6//max(1,n)),c));used.add(family)
    chosen.extend((int(n*f),'control',6+i,None) for i,f in enumerate([.31,.73]))
    windows=[]
    for k,(anchor,kind,zone,c) in enumerate(chosen):
        begin=max(0,anchor-6);end=begin;size=0
        while end<n and end<anchor+110:
            extra=len(rows[end])
            if size+extra>8000 and end>anchor+2:break
            size+=extra;end+=1
        windows.append({'window_id':f'w{k}','zone':zone,'kind':kind,'candidate':c,
                        'lines':[{'id':f'md:{i+1}','text':rows[i]} for i in range(begin,end)],
                        'regional_signals':profile['regions'][min(11,anchor*12//max(n,1))]})
    if windows:
        windows[0]['book_signals']={k:v for k,v in profile.items() if k not in ['regions','front_500']}
    return windows


def validate(value,windows):
    value=copy.deepcopy(value)
    result=v5.validate(value,windows);byid={w['window_id']:w for w in windows}
    evidence=collections.defaultdict(list);issues=[];rejected=[]
    for r in result['records']:
        lines={line['id']:line['text'] for line in byid[r['window_id']]['lines']}
        if r.get('region') not in ['entry_body','internal_section','toc','index','appendix','unknown']:
            raise ValueError('invalid_region')
        for field,allowed in [('structure_tags',set(LABELS)),('quality_issues',ISSUES)]:
            if field not in r or not isinstance(r[field],list):raise ValueError('missing_'+field)
            for tag in r[field]:
                if tag.get('tag') not in allowed:raise ValueError('unknown_'+field)
                if not tag.get('evidence'):raise ValueError('tag_without_evidence')
                if any(not e.get('quote') or e.get('line_id') not in lines or e['quote'] not in lines[e['line_id']] for e in tag['evidence']):
                    rejected.append({'window_id':r['window_id'],'field':field,'annotation':tag,'reason':'tag_quote_not_verbatim'})
                    continue
                if field=='quality_issues' and tag['tag']=='table_flattened' and all(intact_table(lines[e['line_id']]) for e in tag['evidence']):
                    rejected.append({'window_id':r['window_id'],'field':field,'annotation':tag,'reason':'intact_html_table_not_structure_loss'})
                    continue
                if field=='structure_tags' and tag['tag']=='encyclopedic_entries' and r['organization']=='O4':
                    rejected.append({'window_id':r['window_id'],'field':field,'annotation':tag,'reason':'commentary_not_encyclopedia_by_length'})
                    continue
                item={**tag,'window_id':r['window_id'],'zone':r['zone'],'region':r['region']}
                if field=='quality_issues':issues.append(item)
                elif r['region']=='entry_body' and r['verified_entries']:
                    evidence[tag['tag']].append(item)
    supported=[];unconfirmed=[]
    for tag,items in evidence.items():
        # Distinct strata and distinct original lines, not repeated overlapping snippets.
        zones={x['zone'] for x in items if byid[x['window_id']]['kind']=='targeted'}
        ids={e['line_id'] for x in items for e in x['evidence']}
        (supported if len(zones)>=2 and len(ids)>=2 else unconfirmed).append(tag)
    result.update(structure_labels=sorted(supported),structure_label_names=[LABELS[t] for t in sorted(supported)],
                  unconfirmed_tags=sorted(unconfirmed),tag_evidence=dict(evidence),quality_issues=issues,
                  rejected_annotations=rejected,annotation_status='review' if rejected else 'evidence_validated',
                  confidence_basis={'main_type':result['reason'],'tags':'at_least_two_targeted_regions_and_distinct_source_lines',
                                    'semantic_truth_not_guaranteed_by_quote_validation':True},
                  sampling_version='v6_whole_md_signals',extraction_performed=False)
    return result


PROMPT=v5.PROMPT+'''
本版额外输出可组合结构标签，不把七类作为互斥主型。O1-O4不变，内容范围（综合/专科）不能代替结构判断。
候选book_signals和regional_signals是全书/区域统计，不是已确认分类；中英比例不能证明双语配对，首500行不能代表正文，长段落不能独自证明O2。
每窗口必须额外输出structure_tags和quality_issues两个数组，每项为{tag:代码,evidence:[{line_id,quote}],reason:依据}。没有就空数组。只引用MD可见原文。标签需要实际组织关系，不能只因某词出现而标注。内部结构也可记标签，但书级标签仅由真实主条目窗口支持。
分类词目表型需见类目下词目列表与相应正文之间的关系；类目词条型需见类目层级下真实词条。人名词典区别固定字段与叙述传记；史料汇编区别结构化手册；互参条保留关系不合并。双语须见多条实际配对，不能仅有英文引文或一本书里同时存在两种语言。
目录、索引、页眉噪声只作为region或问题记录，不作为主体书型。噪声清理、OCR改字、去重、抽取和挂载均不执行。
结构标签代码及含义：'''+str(LABELS)+'''
质量问题仅用：table_flattened表格压平，head_marker_lost词头格式丢失（有格式附注支持），reading_order_suspected疑似串栏/读序异常，body_missing_suspected疑似正文缺失，hierarchy_ambiguous层级不清，ocr_suspected疑似OCR异常。用原文证据说明，不凭单行长就断言表格压平，不把普通换行当损坏；仅疑似的问题须明确写疑似。
如果HTML表格仍保留完整table/tr/td标签，即使压在一行也不算结构丢失。引文只选该行的一小段原样子串，不得自行加省略号，不需要复制整行长HTML。
'''
