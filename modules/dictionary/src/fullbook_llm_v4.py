"""Whole-book extraction with isolated entry repair and source-only cleanup."""
import argparse
from bisect import bisect_left
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import uuid

import fullbook_llm_v2 as v2
import fullbook_v4_structure as structure
import fullbook_v4_anchors as anchors
from fullbook_llm_v2 import structural_units, attach_format, book_lock, dispatch_ranges
from model_range_crop import read, write

VERSION = 'fullbook-structured-v4'
PROMPT = v2.PROMPT + '''
Return only heads OWNED by [lo,hi). Units outside ownership are context, not output.
Units marked excluded_zone contain no selectable source text. Abbreviation appendices,
contents and indexes are not the main-entry section. Do not extract their list names.
A table or picture may be displaced from the PREVIOUS entry. Check captions and
nearby entry names: exclude a table whose caption identifies a different entry.
Exclude image-link-only units and repeated page headers from body using disjoint spans;
do not stop at internal section headings or lose the text continuing after the image.
Preserve list items that belong to the entry. Body must not end mid-sentence merely
because of a page break. If continuation is unavailable, mark body_complete=false.
Original OCR/spelling is not yours to correct. Keep original head language fields.
head_role=secondary_style is only a hint: compare with surrounding main headings
and PDF cues before deciding whether it is an internal section or a real entry.
Selections may add occurrence: a zero-based index among exact matches WITHIN that
unit. Use this whenever a short quote occurs multiple times. Never guess an index.
The full bilingual head may continue on the next short line: include that line as
a head part and its Chinese/English field, not as the first sentence of the body.
head_role=internal_after_bilingual_main is a hierarchy/pattern warning, not proof.
Use surrounding structure to distinguish a subtitle from an independent entry.
Layout can interleave neighboring articles: a new title in the middle of a sentence
does not belong to that sentence. Select disjoint spans only when ownership is clear.
If neighboring prose cannot be separated reliably, leave body empty, do not borrow it.
document_layout_risk=true indicates repeated source-order interruptions: results
will be isolated for review, but still select only clearly supported source spans.
If a body goes beyond the packet, end at the last COMPLETE sentence and set
body_complete=false. Never end inside a word such as 'parame-' or a Chinese phrase.
'''
REPAIR_PROMPT = PROMPT + '''
REPAIR MODE: initial discovery has already finished. Repair ONLY repair_tasks.
Return {"repairs":[{"repair_id":string,"entry":entry_object_or_null,"reason":string}]}.
Every requested repair_id must appear exactly once, no extra IDs. Valid entries from
discovery are retained separately and must NOT be re-output or modified.
For each task correct its literal source locations and fields; do not invent a new
unrelated entry. If no reliable explicit head exists for this candidate, entry=null
with a reason. A bad body alone can be removed while preserving a reliable head.
'''


def normalize(value):
    return re.sub(r'[^\w\u3400-\u9fff]+','',value.casefold())


def annotate_units(units):
    """Exclude only explicitly labelled list sections, never all empty entries."""
    headings=[re.sub(r'^\s*#+\s*','',r['text']).strip() for r in units if r['kind']=='heading']
    def strong(value):
        letters=''.join(re.findall('[A-Za-z]',value))
        return len(letters)>=3 and letters.isupper() and not re.search('[\u3400-\u9fff]',value)
    caps=sum(strong(h) for h in headings)
    caps_style=caps>=20 and caps/max(1,len(headings))>=.45
    zone = None;zone_level=0;seen_heads=set();previous=None;current_main=None
    for row in units:
        heading = re.sub(r'^\s*#+\s*','',row['text']).strip()
        if row['kind']=='heading':
            match=re.match(r'^\s*(#{1,6})\s',row['text'])
            level=len(match[1]) if match else (1 if re.search(r'\n=+',row['text']) else 2)
            if zone and level<=zone_level:zone=None
            if re.search(r'^(?:附录|appendix\b)',heading,re.I):
                zone = 'abbreviation_appendix' if re.search(r'缩略|abbreviat|acronym',heading,re.I) else None
                zone_level=level
            elif re.fullmatch(r'(?:table of )?contents|index|目录|索引',heading,re.I):
                zone = 'contents_or_index'
                zone_level=level
            if caps_style and strong(heading):
                row['head_role']='main_style';current_main=normalize(heading)
            elif caps_style and current_main:
                row['head_role']='secondary_style'
        image_before=previous and re.fullmatch(r'!\[[^\]]*\]\([^\n]+\)',previous['text'].strip())
        table_before=previous and (previous['kind']=='table' or previous['text'].lstrip().startswith('<table'))
        base_caption=re.split(r'[—–-]\s*(?:Fig\.?|Figure)\s*\d+',heading,flags=re.I)[0]
        if (image_before or table_before) and normalize(base_caption) in seen_heads:
            row['layout_caption_for']=normalize(base_caption)
            row['layout_object_unit']=previous['unit']
            row['head_role']='caption'
        if row['kind']=='heading' and row.get('head_role')!='caption':
            seen_heads.add(normalize(heading))
        if zone:
            row['excluded_zone']=zone
        if row['text'].strip():previous=row
    return units


def model_packet(units,lo,hi,overlap):
    selected=[]
    for row in units[max(0,lo-overlap):min(len(units),hi+overlap)]:
        selected.append({**row,'text':'','source_length':len(row['text'])} if row.get('excluded_zone') else row)
    return {'lo':lo,'hi':hi,'units':selected}


def validate_items(items,part,text,book):
    """One invalid entry never invalidates its correctly sourced neighbors."""
    accepted, rejected, excluded = [], [], []
    rows={r['unit']:r for r in part['units']}
    for index,item in enumerate(items):
        try:
            head=item['head'][0]
            row=rows[head['unit']]
            if row.get('excluded_zone'):
                excluded.append({'index':index,'reason':row['excluded_zone']});continue
            if row.get('head_role') == 'caption':
                excluded.append({'index':index,'reason':row['head_role']});continue
            if not part['lo']<=head['unit']<part['hi']:
                # Verify that this is truly a neighboring source head, not a bogus ID.
                if head['quote'] not in row['text']:raise ValueError('Context head absent')
                excluded.append({'index':index,'reason':'context_only'});continue
            real=structure.preceding_same_heading(head,part['units'])
            if real is not None:raise ValueError(f'Body mention used as head; use actual heading unit {real}')
            built=anchors.validate_entry(item,part,text,book)
            for span in built['source']['body_spans']:
                if any(r.get('excluded_zone') and span[0] < r['offset']+r.get('source_length',len(r['text'])) and r['offset']<span[1]
                       for r in rows.values()):
                    raise ValueError('Body crosses an excluded source region')
            accepted.append({'raw':item,'built':built})
        except (KeyError,IndexError,TypeError,ValueError) as exc:
            rejected.append({'repair_id':f'e{index:05d}','entry':item,
                             'error':f'{type(exc).__name__}: {exc}'})
    return accepted,rejected,excluded


def repair_matches(original,fixed,part):
    rows={r['unit']:r for r in part['units']}
    def anchors(item):
        out=[]
        for head in item['head']:
            row=rows[head['unit']]
            pos=v2.legacy.unique_anchor(row['text'],head['quote'])
            out.append((head['unit'],pos,head['quote']))
        if not out:raise ValueError('Empty head')
        return out
    try:old=anchors(original)
    except (KeyError,ValueError,TypeError,IndexError):
        try:
            old_unit=original['head'][0]['unit'];new=anchors(fixed)
            return isinstance(old_unit,int) and abs(new[0][0]-old_unit)<=1
        except (KeyError,ValueError,TypeError,IndexError):return False
    try:return old==anchors(fixed)
    except (KeyError,ValueError,TypeError,IndexError):return False


def subtract(spans,removals):
    out=[]
    for start,end in spans:
        pieces=[(start,end)]
        for a,b in removals:
            next_pieces=[]
            for c,d in pieces:
                if b<=c or a>=d:next_pieces.append((c,d));continue
                if c<a:next_pieces.append((c,a))
                if b<d:next_pieces.append((b,d))
            pieces=next_pieces
        out.extend(pieces)
    return out


def cleanup_body(entry,units,text):
    """Remove only literal, identifiable layout noise; keep an exact deletion log."""
    spans=entry['source']['body_spans']
    if not spans:return entry
    start,end=spans[0][0],spans[-1][1]
    names={normalize(entry[k]) for k in ('head','knowledge_point','name') if entry[k]}
    offsets=[r['offset'] for r in units]
    first=max(0,bisect_left(offsets,start)-1)
    last=bisect_left(offsets,end)
    removals=[]
    for row in units[first:last]:
        a,b=row['offset'],row['offset']+len(row['text'])
        # A selection may start/end inside this unit. Never remove its outside text.
        if not any(c<=a and b<=d for c,d in spans):continue
        value=row['text'].strip()
        reason=None
        if re.fullmatch(r'!\[[^\]]*\]\([^\n]+\)',value):
            reason='image_markup'
        elif row['kind']=='heading' and normalize(value) in names:
            reason='repeated_own_head'
        elif row.get('layout_caption_for'):
            reason='layout_caption'
            if row['layout_caption_for'] not in names:
                obj=units[row['layout_object_unit']]
                c,d=obj['offset'],obj['offset']+len(obj['text'])
                if any(x<=c and d<=y for x,y in spans):
                    removals.append({'start':c,'end':d,'reason':'other_entry_figure_or_table'})
        elif re.fullmatch(r'.+?[—–-]\s*(?:Fig\.?|Figure)\s*\d+[a-z]?',value,re.I):
            prefix=re.split(r'[—–-]\s*(?:Fig\.?|Figure)\s*\d+',value,flags=re.I)[0]
            if normalize(prefix) in names:reason='own_figure_caption'
        if reason:removals.append({'start':a,'end':b,'reason':reason})
    if removals:
        entry['source']['original_body_spans']=spans
        entry['source']['body_spans']=subtract(spans,[(r['start'],r['end']) for r in removals])
        entry['body_cleanup_removed']=removals
        entry['raw_content']='\n\n'.join(text[a:b] for a,b in entry['source']['body_spans'])
    # A visible continuation cannot coexist with a claim that all prose is complete.
    for row in units[max(first,last-1):min(len(units),last+10)]:
        a,b=row['offset'],row['offset']+len(row['text'])
        if b<=end:continue
        value=text[max(a,end):b].strip()
        if not value:continue
        if row.get('head_role')=='main_style' or row.get('excluded_zone'):break
        if row['kind']=='heading':
            if normalize(value) in names or row.get('head_role') in ('secondary_style','caption'):continue
            break
        if row.get('layout_caption_for') or re.match(r'^(?:!\[|<table|See also\b|Compare\b|参见)',value,re.I):continue
        if row['kind'] in ('paragraph','list','bullet_list','ordered_list'):
            entry['body_complete']=False
            entry['body_coverage_warning']='visible_following_text_not_selected'
        break
    return entry


class Runner(v2.Runner):
    def backoff(self,attempt):
        time.sleep(min(2**attempt,4))

    def fits_messages(self,messages):
        if self.tokenizer:
            size=len(self.tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True))
        else:size=sum(len(m['content'].encode()) for m in messages)
        return size+self.args.output_tokens+2048<=self.args.context

    def fits(self,part):
        return self.fits_messages([{'role':'system','content':PROMPT},{'role':'user','content':v2.legacy.encode(part)}])

    def _run_book(self,spec,folder):
        started=time.monotonic()
        content=Path(spec['md_path']).read_bytes();text=content.decode('utf-8-sig')
        book={k:spec[k] for k in ('identifier','title','md_path')}
        book['md_sha256']=v2.legacy.digest(content)
        units=structure.annotate(structural_units(text))
        attach_format(units,spec.get('pdf_evidence_path'),book['md_sha256'],text)
        code_files=[Path(__file__),Path(v2.__file__),Path(v2.legacy.__file__),Path(__file__).with_name('model_range_crop.py'),
            Path(anchors.__file__),Path(structure.__file__),Path(structure.v3.__file__)]
        identity={'version':VERSION,'book':book,'model':self.args.model,'api_url':self.base,
            'context':self.args.context,'output_tokens':self.args.output_tokens,'overlap':self.args.overlap,
            'attempts':getattr(self.args,'attempts',3),'tokenizer':self.args.tokenizer,
            'code':{p.name:v2.legacy.digest(p.read_bytes()) for p in code_files},
            'units_sha256':v2.legacy.digest(v2.legacy.encode(units).encode())}
        if (folder/'INPUT.json').exists() and read(folder/'INPUT.json')!=identity:
            raise ValueError('Run identity differs; use a new output directory')
        write(folder/'INPUT.json',identity);write(folder/'units.json',units)
        execution_id=uuid.uuid4().hex;execution_dir=folder/'executions'/execution_id
        stats={'execution_id':execution_id,'requests':0,'cache_hits':0,'raw_response_reuse':0,
               'request_seconds':0.,'prompt_tokens':0,'completion_tokens':0,'unknown_usage':0,
               'repair_requests':0,'technical_errors':0,'validation_errors':0}
        entries=[];leaves=[];lock=threading.Lock()

        def add(key,value=1):
            with lock:stats[key]+=value

        def event(kind,**fields):
            write(execution_dir/'events'/f'{time.time_ns()}_{uuid.uuid4().hex}.json',
                  {'kind':kind,'utc':datetime.now(timezone.utc).isoformat(),**fields})

        def complete(lo,hi,saved,part):
            valid,invalid,_=validate_items(saved.get('accepted_raw',[]),part,text,book)
            if invalid:raise ValueError('Saved validated entries no longer match source')
            built=[cleanup_body(e['built'],units,text) for e in valid]
            for entry in built:structure.repair_split_name(entry,units,text)
            with lock:
                entries.extend(built)
                leaves.append({'lo':lo,'hi':hi,'status':saved['status'],'entries':len(built),
                    'unresolved':len(saved.get('unresolved',[])),
                    'excluded_items':len(saved.get('excluded',[])),
                    'excluded_units':sum(bool(r.get('excluded_zone')) for r in units[lo:hi])})

        def process(lo,hi):
            key=f'{lo:08d}_{hi:08d}';cache=folder/'chunks'/f'{key}.json'
            part=structure.packet(units,lo,hi,self.args.overlap)
            if cache.exists():
                saved=read(cache)
                if saved['status']=='split':return [(lo,saved['mid']),(saved['mid'],hi)]
                add('cache_hits');complete(lo,hi,saved,part);return
            if all(r.get('excluded_zone') or not r['text'].strip() for r in units[lo:hi]):
                saved={'status':'completed','accepted_raw':[],'excluded':[],'unresolved':[], 'rule_only':True}
                write(cache,saved);complete(lo,hi,saved,part);return
            # Reserve space for repair tasks instead of filling the discovery budget.
            reserve={'role':'user','content':'x'*6000}
            if not self.fits_messages([{'role':'system','content':PROMPT},
                {'role':'user','content':v2.legacy.encode(part)},reserve]):
                if hi-lo>1:
                    mid=(lo+hi)//2;write(cache,{'status':'split','mid':mid});return [(lo,mid),(mid,hi)]
                saved={'status':'oversized_structure','accepted_raw':[],'excluded':[],
                       'unresolved':[{'error':'Intact structure exceeds budget'}]}
                write(cache,saved);complete(lo,hi,saved,part);return
            accepted={};pending=None;excluded=[];last_error='';scanned=False
            split=False
            for attempt in range(getattr(self.args,'attempts',3)):
                repairing=pending is not None
                request_part={**part}
                selected=[]
                if repairing:
                    for task in pending:
                        trial={**part,'repair_tasks':selected+[task]}
                        msgs=[{'role':'system','content':REPAIR_PROMPT},{'role':'user','content':v2.legacy.encode(trial)}]
                        if not self.fits_messages(msgs):continue
                        selected.append(task)
                    if not selected:break
                    request_part['repair_tasks']=selected
                messages=[{'role':'system','content':REPAIR_PROMPT if repairing else PROMPT},
                          {'role':'user','content':v2.legacy.encode(request_part)}]
                if last_error and not repairing:
                    feedback={'role':'user','content':'Previous response invalid: '+last_error[:300]}
                    if self.fits_messages(messages+[feedback]):messages.append(feedback)
                response_path=folder/'responses'/f'{key}_{attempt+1}.json'
                error_path=folder/'errors'/f'{key}_{attempt+1}.json'
                if error_path.exists():last_error=read(error_path)['error'];continue
                if response_path.exists():
                    response=read(response_path);add('raw_response_reuse')
                else:
                    event('request_started',chunk=key,attempt=attempt+1,repair=repairing)
                    add('requests');start=time.monotonic()
                    if repairing:add('repair_requests')
                    try:
                        response=self.call_model({'model':self.args.model,'temperature':0,
                            'max_tokens':self.args.output_tokens,'chat_template_kwargs':{'enable_thinking':False},
                            'response_format':{'type':'json_object'},'messages':messages})
                    except v2.TelemetryError:raise
                    except Exception as exc:
                        add('technical_errors');add('unknown_usage')
                        last_error=f'{type(exc).__name__}: {exc}'
                        write(error_path,{'error':last_error})
                        event('request_failed',chunk=key,attempt=attempt+1,error=last_error)
                        if attempt+1<getattr(self.args,'attempts',3):self.backoff(attempt)
                        continue
                    elapsed=time.monotonic()-start;add('request_seconds',elapsed)
                    write(response_path,response)
                    usage=response.get('usage') or {}
                    for name in ('prompt_tokens','completion_tokens'):
                        if isinstance(usage.get(name),int):add(name,usage[name])
                    if any(not isinstance(usage.get(k),int) for k in ('prompt_tokens','completion_tokens')):add('unknown_usage')
                    event('request_received',chunk=key,attempt=attempt+1,seconds=elapsed,usage=usage)
                try:
                    choice=response['choices'][0]
                    if choice.get('finish_reason')=='length' and not repairing:
                        split=True;break
                    if choice.get('finish_reason')!='stop':raise ValueError('Incomplete model response')
                    data=json.loads(choice['message']['content'])
                    if not repairing:
                        if data.get('scanned_all') is False:split=True;break
                        if data.get('scanned_all') is not True or not isinstance(data.get('entries'),list):
                            raise ValueError('Invalid discovery schema')
                        scanned=True
                        valid,pending,omitted=validate_items(data['entries'],part,text,book)
                        for e in valid:accepted.setdefault(e['built']['id'],e['raw'])
                        excluded.extend(omitted)
                    else:
                        repairs=data.get('repairs')
                        if not isinstance(repairs,list):raise ValueError('repairs list required')
                        ids=[x['repair_id'] for x in selected]
                        untouched=[x for x in pending if x['repair_id'] not in ids]
                        next_pending=[]
                        for task in selected:
                            rid=task['repair_id']
                            matches=[x for x in repairs if isinstance(x,dict) and x.get('repair_id')==rid]
                            if len(matches)!=1 or 'entry' not in matches[0]:
                                next_pending.append({**task,'error':'Missing, duplicate, or malformed repair'});continue
                            fixed=matches[0];value=fixed['entry']
                            if value is None:
                                if not isinstance(fixed.get('reason'),str) or not fixed['reason'].strip():
                                    next_pending.append({**task,'error':'Null repair needs reason'});continue
                                excluded.append({'repair_id':rid,'reason':fixed['reason']});continue
                            if not anchors.repair_matches(task['entry'],value,part):
                                next_pending.append({**task,'error':'Repair changed candidate identity'});continue
                            valid,bad,omitted=validate_items([value],part,text,book)
                            for e in valid:accepted.setdefault(e['built']['id'],e['raw'])
                            excluded.extend(omitted)
                            if bad:next_pending.append({**bad[0],'repair_id':rid})
                        pending=untouched+next_pending
                    if not pending:break
                    add('validation_errors',len(pending))
                except (ValueError,TypeError,KeyError,IndexError) as exc:
                    add('validation_errors');last_error=f'{type(exc).__name__}: {exc}'
                    event('validation_failed',chunk=key,attempt=attempt+1,error=last_error)
                    continue
            if split and hi-lo>1 and not accepted:
                mid=(lo+hi)//2;write(cache,{'status':'split','mid':mid});return [(lo,mid),(mid,hi)]
            unresolved=pending or ([] if scanned and not split else [{'error':last_error or 'Incomplete scan'}])
            saved={'status':'completed' if scanned and not unresolved else 'partial',
                   'accepted_raw':list(accepted.values()),'unresolved':unresolved,'excluded':excluded}
            write(cache,saved);complete(lo,hi,saved,part)
            event('chunk_finished',chunk=key,status=saved['status'],entries=len(accepted),unresolved=len(unresolved))

        event('book_started',identifier=spec['identifier'])
        if units:dispatch_ranges(process,[(0,len(units))],self.pool,self.workers)
        entries.sort(key=lambda e:e['source']['head_spans'][0][0]);v2.mark_conflicts(entries)
        structure.guard_entries(entries,units,text)
        for e in entries:
            e['name_evidence']=e['source']['head_spans'];e['body_evidence']=e['source']['body_spans']
            e['source']['offset_convention']='UTF-8-sig decoded original text, half-open characters'
        cursor=0
        for leaf in sorted(leaves,key=lambda x:x['lo']):
            if leaf['lo']!=cursor or leaf['hi']<=cursor:raise ValueError('Ownership coverage gap or overlap')
            cursor=leaf['hi']
        if cursor!=len(units):raise ValueError('Ownership tail missing')
        stats['elapsed_seconds']=round(time.monotonic()-started,3)
        summary={'identifier':spec['identifier'],'status':'completed' if all(x['status']=='completed' for x in leaves) else 'partial',
            'total_units':len(units),'completed_units':sum(x['hi']-x['lo'] for x in leaves if x['status']=='completed'),
            'rule_excluded_units':sum(bool(r.get('excluded_zone')) for r in units),
            'entries':len(entries),'empty_body':sum(not e['raw_content'] for e in entries),
            'incomplete_body':sum(not e['body_complete'] for e in entries),
            'structural_review':sum(e.get('structural_review_required',False) for e in entries),
            'eligible_entries':sum(e.get('eligible_for_name_screening',False) for e in entries),
            'unresolved_items':sum(x['unresolved'] for x in leaves),
            'visited_units':cursor,
            'cleaned_entries':sum(bool(e.get('body_cleanup_removed')) for e in entries),
            'pdf_annotated_units':sum(bool(r.get('pdf_format')) for r in units),
            'chunks':leaves,'execution':stats,'semantic_quality_approved':False}
        write(folder/'entries.json',entries)
        write(folder/'accepted_entries.json',[e for e in entries if e.get('eligible_for_name_screening')])
        write(folder/'quarantined_entries.json',[e for e in entries if not e.get('eligible_for_name_screening')])
        write(folder/'SUMMARY.json',summary)
        write(execution_dir/'SUMMARY.json',stats)
        return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--api-url',required=True);p.add_argument('--model',required=True)
    p.add_argument('--context',type=int,default=100000);p.add_argument('--output-tokens',type=int,default=16000)
    p.add_argument('--server-context',type=int);p.add_argument('--tokenizer')
    p.add_argument('--workers',type=int,default=1024);p.add_argument('--book-workers',type=int,default=10)
    p.add_argument('--overlap',type=int,default=4);p.add_argument('--timeout',type=int,default=900)
    args=p.parse_args();args.attempts=3
    if min(args.workers,args.book_workers,args.output_tokens,args.timeout)<1 or args.overlap<0 or args.context<=args.output_tokens+4096:
        p.error('Invalid concurrency or budget settings')
    specs=read(args.manifest)
    if not specs or len({s['identifier'] for s in specs})!=len(specs):p.error('Unique book identifiers required')
    runner=Runner(args);runner.check_server();args.out.mkdir(exist_ok=True,parents=True)
    summaries=[];started=time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.book_workers,len(specs))) as pool:
            futures={pool.submit(runner.run_book,s):s for s in specs}
            for future in concurrent.futures.as_completed(futures):
                try:summaries.append(future.result())
                except Exception as exc:summaries.append({'identifier':futures[future]['identifier'],'status':'technical_failure','error':str(exc)})
                write(args.out/'SUMMARY.json',{'version':VERSION,'expected_books':len(specs),'finished_books':len(summaries),
                    'elapsed_seconds':time.monotonic()-started,'configured_workers':args.workers,
                    'peak_active_requests_this_process':runner.peak_requests,'books':summaries})
    finally:runner.close()
    if any(s['status']!='completed' for s in summaries):raise SystemExit(2)


if __name__=='__main__':main()
