"""Exact-leaf knowledge-point deduplication with dictionary priority."""
import json
from pathlib import Path
import hashlib
import sys
from collections import Counter

from inventory_full_branch_dedup import SUBJECTS,ROOT
from taxonomy_leaf_paths import normalize_path


def branch(row):
    tag=row.get('main_tags')
    return tag if isinstance(tag,str) else ''


def normalized(value):
    return value.strip().casefold() if isinstance(value,str) else ''


def definition_length(row):
    return sum(len(row.get(key) or '') for key in ('definition','description','en_definition','en_description')
               if isinstance(row.get(key),str))


def dedup_records(full_rows,dictionary_rows,eligible_leaf_paths=None):
    """Return (survivors, one audit record per removed input row)."""
    input_rows=[dict(r,origin='dictionary') for r in dictionary_rows]+[dict(r,origin='full') for r in full_rows]
    candidates=[];audit=[];id_to_index={}
    for row in input_rows:
        identifier=normalized(str(row.get('id') or ''))
        if identifier and identifier in id_to_index:
            previous_index=id_to_index[identifier];previous=candidates[previous_index]
            new_score=(row['origin']=='dictionary',definition_length(row))
            old_score=(previous['origin']=='dictionary',definition_length(previous))
            if new_score>old_score:
                audit.append(dict(removed_id=previous.get('id'),removed_origin=previous['origin'],
                                  kept_id=row.get('id'),kept_origin=row['origin'],reason='same_id_reconciled',branch=branch(previous)))
                candidates[previous_index]=row
            else:
                audit.append(dict(removed_id=row.get('id'),removed_origin=row['origin'],
                                  kept_id=previous.get('id'),kept_origin=previous['origin'],reason='same_id_reconciled',branch=branch(row)))
            continue
        if identifier:id_to_index[identifier]=len(candidates)
        candidates.append(row)
    parent=list(range(len(candidates)))
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def union(a,b):
        a=find(a);b=find(b)
        if a!=b:parent[b]=a
    seen={}
    for i,row in enumerate(candidates):
        part=branch(row)
        if not part or (eligible_leaf_paths is not None and normalize_path(part) not in eligible_leaf_paths):continue
        for field in ('name','knowledge_point'):
            value=normalized(row.get(field))
            if not value:continue
            key=(part,field,value)
            if key in seen:union(seen[key],i)
            else:seen[key]=i
    groups={}
    for i in range(len(candidates)):groups.setdefault(find(i),[]).append(i)
    winners=[]
    for members in groups.values():
        winner=max(members,key=lambda i:(candidates[i]['origin']=='dictionary',definition_length(candidates[i]),-i))
        chosen=dict(candidates[winner]);tags=chosen.get('related_tags')
        related=list(tags) if isinstance(tags,list) else ([tags] if isinstance(tags,str) and tags else [])
        related=[v for v in related if isinstance(v,str) and v]
        observed=set(related);observed.add(chosen.get('main_tags'))
        for i in members:
            if i==winner:continue
            loser=candidates[i]
            path=loser.get('main_tags')
            if isinstance(path,str) and path and path not in observed:
                related.append(path);observed.add(path)
            other_related=loser.get('related_tags') or []
            if isinstance(other_related,str):other_related=[other_related]
            for path in other_related:
                if isinstance(path,str) and path and path not in observed:
                    related.append(path);observed.add(path)
            shared_name=normalized(loser.get('name')) and normalized(loser.get('name'))==normalized(chosen.get('name'))
            shared_english=normalized(loser.get('knowledge_point')) and normalized(loser.get('knowledge_point'))==normalized(chosen.get('knowledge_point'))
            reason='same_leaf_name' if shared_name else 'same_leaf_english' if shared_english else 'same_leaf_transitive'
            audit.append(dict(removed_id=loser.get('id'),removed_origin=loser['origin'],
                              kept_id=chosen.get('id'),kept_origin=chosen['origin'],reason=reason,branch=branch(loser)))
        chosen['related_tags']=related
        winners.append((winner,chosen))
    winners.sort(key=lambda pair:pair[0])
    kept=[row for _,row in winners]
    if len(kept)+len(audit)!=len(input_rows):raise AssertionError('input coverage mismatch')
    return kept,audit


def read_with_sha(path):
    before=path.stat();raw=path.read_bytes();after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('source changed during read: '+str(path))
    text=raw.decode('utf-8-sig')
    try:
        data=json.loads(text)
        if isinstance(data,list):rows=data
        elif isinstance(data,dict):rows=next((data[k] for k in ('data','records','knowledge_points') if isinstance(data.get(k),list)),None)
        else:rows=None
    except json.JSONDecodeError:rows=None
    if rows is None:rows=[json.loads(line) for line in text.splitlines() if line.strip()]
    if not all(isinstance(row,dict) for row in rows):raise ValueError('non-object record: '+str(path))
    return rows,dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),mtime_ns=after.st_mtime_ns)


def write_jsonl(path,rows):
    with path.open('w',encoding='utf-8') as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False)+'\n')


def process_subject(subject,full_path,dictionary_path,output):
    full,fmeta=read_with_sha(full_path);dictionary,dmeta=read_with_sha(dictionary_path)
    kept,audit=dedup_records(full,dictionary)
    non_dictionary=[r for r in kept if r['origin']!='dictionary']
    output.mkdir(exist_ok=False,parents=True)
    write_jsonl(output/'full_deduplicated.jsonl',kept)
    write_jsonl(output/'without_dictionary.jsonl',non_dictionary)
    write_jsonl(output/'dedup_audit.jsonl',audit)
    summary=dict(subject=subject,full_input=len(full),dictionary_input=len(dictionary),
                 union_input=len(full)+len(dictionary),dedup_total=len(kept),
                 dictionary_retained=len(kept)-len(non_dictionary),without_dictionary=len(non_dictionary),
                 removed=len(audit),removed_by_reason=dict(Counter(r['reason'] for r in audit)),
                 full_source=fmeta,dictionary_source=dmeta,
                 definition='union current dictionary and current full; reconcile same ID; then dedup only within literally identical nonempty main_tags path by Chinese or English title; dictionary priority')
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if (full_path.stat().st_size,full_path.stat().st_mtime_ns)!=(fmeta['bytes'],fmeta['mtime_ns']):raise RuntimeError('full source changed after processing')
    if (dictionary_path.stat().st_size,dictionary_path.stat().st_mtime_ns)!=(dmeta['bytes'],dmeta['mtime_ns']):raise RuntimeError('dictionary source changed after processing')
    print(json.dumps({k:summary[k] for k in ('subject','full_input','dictionary_input','dedup_total','dictionary_retained','without_dictionary','removed')},ensure_ascii=False),flush=True)
    return summary


def main():
    root=Path('/home/wangqiyuan/work/full_exact_path_dedup_20260923')
    root.mkdir(exist_ok=True)
    summaries=[]
    for subject,full_name,dict_name in SUBJECTS:
        full_path=ROOT/'knowledge_points'/full_name
        dictionary_path=ROOT/'dictionaries'/subject/dict_name
        if not full_path.is_file():
            summaries.append(dict(subject=subject,status='missing_full',path=str(full_path)))
            continue
        output=root/subject
        if (output/'summary.json').is_file():
            summary=json.loads((output/'summary.json').read_text());summaries.append(summary);continue
        summaries.append(process_subject(subject,full_path,dictionary_path,output))
        (root/'overall_progress.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'overall_summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    completed=[s for s in summaries if 'dedup_total' in s]
    print(json.dumps(dict(completed=len(completed),missing=[s['subject'] for s in summaries if 'dedup_total' not in s],
                          total=sum(s['dedup_total'] for s in completed),
                          without_dictionary=sum(s['without_dictionary'] for s in completed)),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
