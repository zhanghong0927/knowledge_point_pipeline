"""Offline classification projection, independent of extraction and model transport."""
import collections
import re
from structure_v6 import LABELS

FAMILIES={'O1':'entry_prose','O2':'entry_prose','O3':'fixed_fields','O4':'text_commentary'}


def quoted(e,lines):
    return (isinstance(e,dict) and isinstance(e.get('quote'),str) and bool(e['quote'])
            and e.get('line_id') in lines and e['quote'] in lines[e['line_id']])


def head_tag_bound(tag, evidence, entries, lines, metadata):
    for entry in entries:
        h=entry['head']
        if not entry['role_verified'] or h['line_id']!=evidence['line_id']:continue
        line=lines[h['line_id']];start=line.find(h['quote']);end=start+len(h['quote'])
        qstart=line.find(evidence['quote']);qend=qstart+len(evidence['quote'])
        if max(start,qstart)>=min(end,qend):continue
        if tag=='bracket_headwords':
            if not any(m.start()<=start and m.end()>=end for m in re.finditer(r'【[^】]+】|\[[^\]]+\]',line)):continue
        if tag=='numbered_heads':
            prefix=re.sub(r'^\s*#{1,6}\s*','',line[:end]).lstrip()
            if not re.match(r'(?:\d+[.)、\s]|[①-⑳]|[一二三四五六七八九十]+[、.)])',prefix):continue
        if tag=='bold_heads':
            explicit=any(m.start()<=start and m.end()>=end for m in re.finditer(r'\*\*[^*]+\*\*|__[^_]+__',line))
            normalize=lambda s:re.sub(r'\W+','',s).casefold()
            needle=normalize(h['quote']);pdf_bold=False
            for match in metadata[h['line_id']].get('pdf_format',[]):
                runs=[];current=''
                for span in match.get('spans',[]):
                    if span.get('bold') is True:current+=span.get('text','')
                    else:runs.append(current);current=''
                runs.append(current)
                pdf_bold=pdf_bold or bool(needle and any(needle in normalize(run) for run in runs))
            if not explicit and not pdf_bold:continue
        return True
    return False


def plain_head(text):
    text=re.sub(r'^\s*#{1,6}\s*','',text)
    return text.strip().strip('*_[]【】 .:;').strip()


def source_position(head,body,lines):
    line=lines[head['line_id']]
    if head['line_id']==body['line_id']:
        if line.find(body['quote'])>line.find(head['quote']):return 'inline'
    elif plain_head(line)==plain_head(head['quote']):
        return 'standalone'
    return 'unknown'


def project(value,windows,excluded_heads=(),require_boundary=False,
            require_scope=False,require_commentary=False):
    records=value.get('windows',[]);byid={w['window_id']:w for w in windows}
    if len(records)!=len(windows) or {r.get('window_id') for r in records}!=set(byid):
        raise ValueError('window_set_mismatch')
    support=[];warnings=[];quality=[];tags=[];boundaries=[]
    for rec in records:
        wid=rec['window_id'];win=byid[wid];lines={l['id']:l['text'] for l in win['lines']}
        scope=rec.get('body_scope')
        if require_scope and scope not in {'primary','supplement','unknown'}:
            raise ValueError('body_scope_missing_or_invalid')
        primary_scope=not require_scope or scope=='primary'
        contract=rec.get('boundary_contract',{})
        if require_boundary:
            if contract.get('status') not in {'supported','unsupported','uncertain','not_body'}:
                raise ValueError('boundary_contract_missing_or_invalid')
            if contract['status'] in {'supported','unsupported'} and (
                not contract.get('reason') or not contract.get('evidence') or
                not all(quoted(e,lines) for e in contract['evidence'])
            ):
                warnings.append({'window':wid,'reason':'invalid_boundary_evidence','assessment':contract})
                contract={**contract,'status':'uncertain'}
            boundaries.append({'window':wid,'zone':win['zone'],'region':rec.get('region'),
                               'primary_scope':primary_scope,
                               'scope_excluded':require_scope and scope=='supplement',**contract})
        valid=[]
        for e in rec.get('entries',[]):
            h,b=e.get('head',{}),e.get('body',{})
            reason=None
            if not quoted(h,lines) or not quoted(b,lines):reason='invalid_head_body_quote'
            elif ' '.join(h['quote'].split())==' '.join(b['quote'].split()):reason='duplicate_head_body'
            elif h['line_id'] in excluded_heads:reason='audit_rejected_main_head'
            elif e.get('role') not in [None,'main_entry']:reason='not_main_entry'
            elif re.fullmatch(r'[Xx _*-]+',plain_head(h['quote'])) and re.search(r'[Xx]{5,}',h['quote']):reason='redaction'
            if not reason and require_commentary and rec.get('organization')=='O4':
                c=e.get('commentary',{})
                if not quoted(c,lines) or any(
                    ' '.join(c['quote'].split())==' '.join(anchor['quote'].split()) or
                    (c['line_id']==anchor['line_id'] and (
                        c['quote'] in anchor['quote'] or anchor['quote'] in c['quote']))
                    for anchor in (h,b)
                ):reason='missing_or_duplicate_commentary'
            if reason:
                warnings.append({'window':wid,'reason':reason,'entry':e});continue
            position=source_position(h,b,lines)
            if position=='unknown':warnings.append({'window':wid,'reason':'unresolved_head_line_position','entry':e})
            valid.append({'head':h,'body':b,'position':position,'role_verified':e.get('role')=='main_entry',
                          **({'commentary':e['commentary']} if 'commentary' in e else {})})
        main=rec.get('region')=='entry_body' and primary_scope
        integrity=rec.get('integrity',{})
        if main and (not rec.get('md_usable',True) or integrity.get('status') in ['damaged','uncertain']):
            quality.append({'window':wid,'integrity':integrity,'md_usable':rec.get('md_usable')})
        for tag in rec.get('structure_tags',[]):
            if tag.get('tag') not in LABELS or not tag.get('evidence') or not all(quoted(e,lines) for e in tag['evidence']):
                warnings.append({'window':wid,'reason':'invalid_optional_tag','annotation':tag});continue
            if tag['tag'] in {'bracket_headwords','bold_heads','numbered_heads'} and (
                not main or not all(head_tag_bound(tag['tag'],e,valid,lines,{l['id']:l for l in win['lines']}) for e in tag['evidence'])
            ):
                warnings.append({'window':wid,'reason':'tag_not_bound_to_main_head','annotation':tag});continue
            tags.append({'window':wid,**tag})
        family=FAMILIES.get(rec.get('organization'))
        if rec.get('organization')=='mixed' and set(rec.get('organization_options',[]))=={'O1','O2'}:
            family='entry_prose'
        if main and valid and family and (not require_boundary or contract['status']=='supported'):
            support.append({'window':wid,'zone':win['zone'],'kind':win['kind'],
                            'organization':rec['organization'],'family':family,
                            'entries':valid})
    counts=collections.Counter(s['family'] for s in support)
    family=counts.most_common(1)[0][0] if counts else 'unknown'
    chosen=[s for s in support if s['family']==family]
    zones={s['zone'] for s in chosen if s['kind']=='targeted'}
    anchors={e['head']['line_id'] for s in chosen if s['kind']=='targeted' for e in s['entries']}
    enough=len(zones)>=2 and len(anchors)>=2 and counts[family]/max(1,len(support))>=.7
    verified=[s for s in chosen if any(e['role_verified'] for e in s['entries'])]
    verified_zones={s['zone'] for s in verified if s['kind']=='targeted'}
    verified_ids={e['head']['line_id'] for s in verified if s['kind']=='targeted' for e in s['entries'] if e['role_verified']}
    positions=sorted({e['position'] for s in support for e in s['entries'] if e['position']!='unknown'})
    role_ok=len(verified_zones)>=2 and len(verified_ids)>=2
    status=('sample_supported' if role_ok else 'legacy_candidate_unverified') if enough and positions else 'review'
    organizations=collections.Counter(s['organization'] for s in support)
    primary=organizations.most_common(1)[0][0] if organizations else 'unknown'
    subtype_ok=primary in FAMILIES and organizations[primary]/max(1,len(support))>=.7 and len({s['zone'] for s in support if s['kind']=='targeted' and s['organization']==primary})>=2
    result={'routing_status':status,'family':family,'family_counts':dict(counts),'head_position':positions[0] if len(positions)==1 else 'both' if positions else 'unknown',
            'organization_candidate':primary,'organization_counts':dict(organizations),
            'organization_status':'sample_supported' if subtype_ok else 'review',
            'md_quality_status':'review' if quality else 'no_problem_reported_in_samples',
            'quality_observations':quality,'support':support,'warnings':warnings,'validated_optional_annotations':tags,
            'ready_for_extraction':False,'extraction_rules_tested':False,'human_verified':False,
            'whole_book_verified':False,'scope':'proposed rule family only; not extraction approval'}
    assessment=value.get('applicability')
    result['boundary_assessments']=boundaries
    result['applicability_status']='not_assessed'
    if assessment is not None:
        decision=assessment.get('decision')
        result['applicability_assessment']=assessment
        if decision=='compatible':result['applicability_status']='model_considered_compatible'
        elif decision=='uncertain':
            result['applicability_status']='uncertain';result['routing_status']='review'
        else:
            evidence=assessment.get('evidence',[]);zones=set();ids=set();valid=bool(evidence)
            if require_boundary:
                body=[b for b in boundaries if not b['scope_excluded'] and b['region'] in {'entry_body','internal_section'} and b['status']!='not_body']
                unsupported={b['window'] for b in body if b['primary_scope'] and b['status']=='unsupported'}
                valid=valid and len(unsupported)/max(1,len(body))>=.7
            for e in evidence:
                win=byid.get(e.get('window_id'))
                if not win or not quoted(e,{l['id']:l['text'] for l in win['lines']}):
                    valid=False;continue
                if require_boundary and e['window_id'] not in unsupported:valid=False
                zones.add(win['zone']);ids.add(e['line_id'])
            if decision=='other' and assessment.get('reason') and valid and len(zones)>=2 and len(ids)>=2:
                result.update(family_before_scope=result['family'],family='other',routing_status='other',
                              applicability_status='evidenced_incompatibility')
            else:
                result.update(routing_status='review',applicability_status='unverified')
                result['warnings'].append({'reason':'invalid_or_insufficient_applicability_evidence'})
    if require_boundary and result['routing_status']=='sample_supported':
        body=[b for b in boundaries if not b['scope_excluded'] and b['region'] in {'entry_body','internal_section'} and b['status']!='not_body']
        if sum(b['status']=='unsupported' for b in body)/max(1,len(body))>=.3:
            result['routing_status']='review'
            result['warnings'].append({'reason':'conflicting_main_body_boundaries'})
    return result
