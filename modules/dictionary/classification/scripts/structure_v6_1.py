"""Evidence-role and body-integrity guards for the paired v6 regression."""
import copy
import re
import structure_v6 as v6

LABELS=v6.LABELS
ROLES={'main_entry','internal_heading','front_back_matter','unknown'}
PATTERNS={'direct_definition':'O1','topic_development':'O2',
          'repeated_fields':'O3','text_commentary':'O4'}
HEAD_TAGS={'bracket_headwords','bold_heads','numbered_heads'}
SECTION=re.compile(r'^\s*#{1,6}\s*(?:Appendix(?:\s+[A-Z0-9]+)?|Appendices|Bibliography|Index|附录|索引)(?:\b|\s|$)',re.I)


def add_context(windows,rows):
    result=copy.deepcopy(windows)
    headings=[i for i,r in enumerate(rows) if re.match(r'^#{1,6}\s',r)]
    sections=[i for i,r in enumerate(rows) if SECTION.match(r)]
    for w in result:
        start=int(w['lines'][0]['id'].split(':')[1])-1
        # Sparse ancestor candidates are clues, never a reconstructed heading tree.
        before=[i for i in headings if max(0,start-500)<=i<start][-12:]
        near=list(range(max(0,start-16),start))
        selected=sorted(set(before+near));budget=6000;context=[]
        for i in reversed(selected):
            if len(rows[i])>budget:continue
            context.append({'id':f'md:{i+1}','text':rows[i]});budget-=len(rows[i])
        w['context_lines']=list(reversed(context))
        markers=[i for i in sections if i<start]
        w['section_hint']=({'id':f'md:{markers[-1]+1}','text':rows[markers[-1]],
                            'warning':'Earlier section marker only; scope may have ended.'} if markers else None)
    return result


def check_evidence(evidence,lines,required=False):
    if not isinstance(evidence,list) or (required and not evidence):
        raise ValueError('missing_role_or_integrity_evidence')
    for e in evidence:
        if not e.get('quote') or e.get('line_id') not in lines or e['quote'] not in lines[e['line_id']]:
            raise ValueError('role_or_integrity_quote_not_verbatim')


def validate(value,windows):
    value=copy.deepcopy(value);byid={w['window_id']:w for w in windows};guards=[]
    for rec in value.get('windows',[]):
        w=byid.get(rec.get('window_id'))
        if w is None:raise ValueError('window_set_mismatch')
        lines={l['id']:l['text'] for l in w['lines']+w.get('context_lines',[])}
        if w.get('section_hint'):lines[w['section_hint']['id']]=w['section_hint']['text']
        kept=[];discarded=[]
        for entry in rec.get('entries',[]):
            if entry.get('role') not in ROLES:raise ValueError('missing_entry_role')
            if not entry.get('role_reason'):raise ValueError('missing_entry_role_reason')
            check_evidence(entry.get('role_evidence'),lines,required=True)
            head=entry.get('head',{}).get('quote','')
            redacted=bool(re.fullmatch(r'[\[\] Xx\-_*]+',head)) and bool(re.search(r'[Xx]{5,}',head))
            if entry['role']=='main_entry' and not redacted:kept.append(entry)
            else:discarded.append({**entry,'guard_reason':'redaction_not_head' if redacted else 'not_main_entry'})
        rec['entries']=kept;rec['excluded_entries']=discarded
        assessment=rec.get('body_assessment',{})
        if assessment.get('pattern') not in {*PATTERNS,'mixed','unknown'}:
            raise ValueError('missing_body_assessment')
        check_evidence(assessment.get('evidence'),lines,required=bool(kept))
        expected=PATTERNS.get(assessment['pattern'])
        if kept and expected and rec.get('organization')!=expected:
            guards.append({'window_id':rec['window_id'],'reason':'organization_body_pattern_conflict'})
            rec['organization']='unknown'
        integrity=rec.get('integrity',{})
        if integrity.get('status') not in ['usable','damaged','uncertain']:
            raise ValueError('missing_integrity_assessment')
        check_evidence(integrity.get('evidence'),lines,required=integrity['status']=='damaged')
        if integrity['status']=='damaged':
            rec['md_usable']=False
        # Head formatting must describe a confirmed head, not any bracketed field.
        head_ids={e['head']['line_id'] for e in kept}
        tags=[]
        for tag in rec.get('structure_tags',[]):
            if tag.get('tag') in HEAD_TAGS and not any(e.get('line_id') in head_ids for e in tag.get('evidence',[])):
                guards.append({'window_id':rec['window_id'],'reason':'head_tag_without_main_head_evidence','tag':tag})
            else:tags.append(tag)
        rec['structure_tags']=tags
    result=v6.validate(value,windows)
    type_value=copy.deepcopy(value)
    damaged=[]
    for rec in type_value['windows']:
        if rec.get('region')=='entry_body' and not rec['md_usable']:
            damaged.append(rec['window_id'])
            # Damaged windows do not vote; clean regions may still establish type.
            rec['region']='unknown';rec['entries']=[];rec['organization']='unknown'
    type_result=v6.validate(type_value,windows)
    result.update(semantic_guards=guards,validation_version='v6.1_role_integrity',
                  sampling_version='v6_same_windows_with_preceding_context',
                  classification_status=type_result['status'],classification_reason=type_result['reason'],
                  md_quality_status='review' if damaged else 'no_blocking_damage_observed',
                  damaged_main_windows=damaged)
    return result


PROMPT=v6.PROMPT+'''
v6.1补充规则（与上文冲突时以此为准）：
1. 本次仍只判断主体组织类型和格式特点，不抽取词条正文。先判断词头角色，再判断组织。
Markdown标题、粗体、全大写不证明是主词条。必须结合前文和正文确认它命名独立的被解释对象，
而不是人物传记的“早年/最后征战/遗产”、专题中的经济/历史影响/结论或附录文件的小标题。
context_lines是此前原文的稀疏标题和近邻文字，不是连续正文或已确认层级。
section_hint只是之前最近附录/索引标记，可能已结束；不可单凭它否定后续真实词条。
真实词条头必须仍从lines取；不得用context_lines补造当前窗口的主词条。
entries允许记录被怀疑的候选，但每项新增role=main_entry|internal_heading|front_back_matter|unknown、
role_reason一句话、role_evidence=[{line_id,quote}]。只有main_entry会计入支持。
引用能说明上下级关系的短原文；有不确定性就unknown。不能把[XXXX]保密删节、作者署名、
参见中的被引名称、内部字段当独立词条。窗口确实含真词条时，不因同时有内部标题而全部排除。
2. 每窗口新增body_assessment={pattern,evidence:[{line_id,quote}],reason}。
pattern只能direct_definition|topic_development|repeated_fields|text_commentary|mixed|unknown。
O1是直接释义/用法/义项组织；O2是围绕对象连续展开历史、机制、论述或叙述传记。
百科词条开头一句下定义不使它成为O1，要检查开头后面的连续内容；长短不是硬阈值。
短传记资料或长释义也不自动变O2。O3须反复固定字段主导，不因百科偶见国家面积等字段就判O3。
证据取真实主词条的正文，不能取内部标题或仅复制开头定义证明整个条目。没有主词条时可unknown。
3. 每窗口新增integrity={status:usable|damaged|uncertain,evidence:[{line_id,quote}],reason}。
明确不同条目的段落交叉插入、标题后接上一篇残句、破坏边界的重复/顺序错乱，为damaged且md_usable=false。
要引用实际矛盾或串文片段，不能凭主题略变或普通重复就判损坏。普通换行、无PDF附注、无害定位误差、
小标题层级相同但语义清晰，不算损坏。证据不足只记uncertain，不凭猜测否决整本。
定义开头后转历史论述是正常展开，不是串文。PDF仍仅作格式辅助，MD是分类和可用性的依据。
4. bracket_headwords/bold_heads/numbered_heads必须引用已确认主词头所在行；【注】【赏析】【纹饰】等内部字段不算词头。
classified_wordlist必须真正见分类词目列表与条目正文的对应；只有类目下正文用category_entries。
每项证据是一个原始单行的短子串，不跨行、不改字、不追加省略号。输出覆盖全部window_id。
'''
