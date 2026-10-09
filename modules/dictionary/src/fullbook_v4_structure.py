"""Evidence-bounded structure hints, exact name continuation and risk isolation."""
from bisect import bisect_right
import re
import fullbook_llm_v3 as v3

def heading(row):
    return re.sub(r'^\s*#+\s*','',row['text']).strip()

def bilingual(value):
    return bool(re.search(r'[\u3400-\u9fff].*[（(][A-Za-zÀ-ÿ]',value))

def annotate(units):
    v3.annotate_units(units)
    index_zone=False;previous=None;adjacent=[]
    for row in units:
        value=heading(row)
        if row['kind']=='heading':
            if re.search(r'(?:术语索引|词目索引|汉英索引|英汉索引|汉语拼音索引|alphabetical index|index of terms)',value,re.I):
                index_zone=True
            elif index_zone and not re.fullmatch(r'[A-Za-z]|[一二三四五六七八九十]|[\u3400-\u9fff]?\s*[A-Z]',value):
                index_zone=False
            if previous and previous['kind']=='heading' and bilingual(heading(previous)) and not bilingual(value):
                adjacent.append((previous,row))
        if index_zone:row['excluded_zone']='term_index'
        if row['text'].strip():previous=row
    for parent,row in adjacent:
        parent_level=len(re.match(r'^\s*(#+)',parent['text'])[1]) if re.match(r'^\s*(#+)',parent['text']) else 2
        level=len(re.match(r'^\s*(#+)',row['text'])[1]) if re.match(r'^\s*(#+)',row['text']) else 2
        if level>parent_level or len(adjacent)>=3:
            row['head_role']='internal_after_bilingual_main'
            row['parent_head_unit']=parent['unit']
    nonempty=[r for r in units if r['text'].strip()]
    interrupted=[]
    for i,row in enumerate(nonempty[1:-1],1):
        prev,nxt=nonempty[i-1],nonempty[i+1]
        if (row['kind']=='heading' and prev['kind']==nxt['kind']=='paragraph'
                and re.search(r'[A-Za-z]-\s*$',prev['text']) and re.match(r'^[a-z]',nxt['text'].strip())):
            interrupted.append(row['unit'])
    if len(interrupted)>=3:
        for row in units:row['document_layout_risk']=True
    return units

def preceding_same_heading(head,units):
    rows={r['unit']:r for r in units};row=rows.get(head.get('unit'))
    if not row or row['kind']!='paragraph':return None
    for uid in range(row['unit']-1,max(-1,row['unit']-5),-1):
        other=rows.get(uid)
        if other and other['kind']=='heading':
            if other.get('head_role')!='caption' and v3.normalize(heading(other))==v3.normalize(head.get('quote','')):
                return uid
            break
    return None

def packet(units,lo,hi,overlap):
    # Preserve forward context instead of shrinking it to zero when budgets are tight.
    stop=min(len(units),hi+max(overlap,8));limit=min(len(units),hi+48)
    while stop<limit:
        tail=next((r['text'].strip() for r in reversed(units[hi:stop]) if r['text'].strip()),'')
        if re.search(r'[.!?。！？][”’\"\')）\]]*$',tail):break
        stop+=1
    rows=[]
    for row in units[max(0,lo-max(overlap,4)):stop]:
        keep={k:row[k] for k in ('unit','text','kind','heading_path','head_role','parent_head_unit','pdf_format','excluded_zone') if k in row}
        # Full offsets remain available to validation; no source reflow occurs.
        keep['offset']=row['offset']
        if row.get('excluded_zone'):keep.update(text='',source_length=len(row['text']))
        rows.append(keep)
    return {'lo':lo,'hi':hi,'units':rows,'document_layout_risk':any(r.get('document_layout_risk') for r in units)}

def repair_split_name(entry,units,text):
    name=entry.get('name','')
    if not entry.get('knowledge_point') or not re.fullmatch(r'[\u3400-\u9fff]',name):return
    end=entry['source']['head_spans'][-1][1]
    offsets=[r['offset'] for r in units]
    i=max(0,bisect_right(offsets,end)-1)
    after=text[end:units[i]['offset']+len(units[i]['text'])]
    if after.strip():return
    following=next((r for r in units[i+1:i+5] if r['text'].strip()),None)
    if not following or following['kind']=='heading':return
    value=following['text'].strip()
    if not re.fullmatch(r'[\u3400-\u9fff]{2,24}',value):return
    a=following['offset']+following['text'].index(value);b=a+len(value)
    # Require an independent intact occurrence, not just a short first sentence.
    full=name+value
    if not any(m.end()<=entry['source']['head_spans'][0][0] or m.start()>=b
               for m in re.finditer(re.escape(full),text)):return
    spans=entry['source']['body_spans']
    if not spans or not (spans[0][0]<=a and b<=spans[0][1]):return
    entry['source']['original_head_spans']=list(entry['source']['head_spans'])
    entry['source']['head_spans']=list(entry['source']['head_spans'])+[(a,b)]
    entry['source'].setdefault('original_body_spans',list(spans))
    entry['source']['body_spans']=v3.subtract(spans,[(spans[0][0],b)])
    entry['name']+=value;entry['head']+=value
    entry['raw_content']='\n\n'.join(text[x:y] for x,y in entry['source']['body_spans'])
    entry['head_format_repair']={'reason':'adjacent_chinese_head_continuation','added_span':[a,b]}

def guard_entries(entries,units,text):
    offsets=[r['offset'] for r in units]
    heads=sorted(((e['source']['head_spans'][0][0],e) for e in entries),key=lambda x:x[0])
    positions=[a for a,e in heads]
    for entry in entries:
        risks=set(entry.get('extraction_risks',[]))
        a=entry['source']['head_spans'][0][0]
        row=units[max(0,bisect_right(offsets,a)-1)]
        if row.get('head_role')=='internal_after_bilingual_main':risks.add('internal_heading_as_entry')
        if row.get('excluded_zone'):risks.add('excluded_section_entry')
        for start,end in entry['source']['body_spans']:
            j=bisect_right(positions,start-1)
            while j<len(heads) and heads[j][0]<end:
                if heads[j][1] is not entry:risks.add('other_entry_head_in_body')
                j+=1
        # A lone Chinese character followed by Chinese text is unresolved name damage.
        first_line=entry.get('raw_content','').strip().split('\n',1)[0]
        if entry.get('knowledge_point') and re.fullmatch(r'[\u3400-\u9fff]',entry.get('name','')) and re.fullmatch(r'[\u3400-\u9fff]{2,24}',first_line):
            risks.add('possible_fragmented_bilingual_head')
        if risks:
            entry['extraction_risks']=sorted(risks)
            entry['structural_review_required']=True
            entry['eligible_for_name_screening']=False
        else:entry['eligible_for_name_screening']=not entry.get('structural_review_required',False)
    crossed=sum('other_entry_head_in_body' in e.get('extraction_risks',[]) for e in entries)
    if (crossed>=10 and crossed/max(1,len(entries))>=.10) or any(r.get('document_layout_risk') for r in units):
        for entry in entries:
            entry['extraction_risks']=sorted(set(entry.get('extraction_risks',[]))|{'systematic_interleaving_review'})
            entry['structural_review_required']=True
            entry['eligible_for_name_screening']=False
    return entries
