"""Auditable selection and exact-node deduplication, never ID-only merging."""
import copy

FIELDS=('id','knowledge_point','name','definition','en_definition','description','en_description','main_tags','related_tags','source')

def select_status(manual_drop,name_status,mount_ok,mount_rejected):
    if manual_drop:return 'manual_drop'
    if name_status=='drop':return 'name_drop'
    if name_status not in ('keep','review'):return 'missing_pipeline'
    if mount_ok:return 'accepted'
    return 'mount_rejected' if mount_rejected else 'mount_unresolved'

def standard(row):
    out={f:copy.deepcopy(row.get(f,[] if f=='related_tags' else '')) for f in FIELDS}
    out['id']=str(out['id'])
    for f in FIELDS:
        if f not in ('source','related_tags'):
            if out[f] is None:out[f]=''
            if not isinstance(out[f],str):raise ValueError(f'Non-string {f}: {out["id"]}')
    if out['related_tags'] is None:out['related_tags']=[]
    if not isinstance(out['related_tags'],list):raise ValueError('related_tags must be list')
    if not out['id'] or not (out['name'].strip() or out['knowledge_point'].strip()):raise ValueError('Missing name/ID')
    return out

def merge_records(subject,dictionary,non_dictionary,path_key=lambda p:p):
    """Dictionary wins same-node title duplicates; unrelated same IDs survive."""
    seen={};used=set();merged=[];mapping=[];removed=[]
    # Within each origin prefer longer definitions, maintaining stable ties.
    groups=[('dictionary',dictionary),('non_dictionary',non_dictionary)]
    for origin,rows in groups:
        assert len({str(r['id']) for r in rows})==len(rows)
        ordered=sorted(enumerate(rows),key=lambda x:(-len(x[1].get('definition','')),-len(x[1].get('en_definition','')),x[0]))
        selected=[]
        for index,row in ordered:
            path=path_key(row['main_tags']);keys=[(path,f,str(row.get(f)or'').strip().casefold()) for f in ('name','knowledge_point') if str(row.get(f)or'').strip() and path]
            hit=next((seen[k] for k in keys if k in seen),None)
            if hit:
                removed.append({'origin':origin,'removed_id':row['id'],'kept_origin':hit['origin'],'kept_id':hit['id'],'reason':'same_node_title','name':row['name'],'knowledge_point':row['knowledge_point'],'main_tags':row['main_tags']})
                continue
            out=copy.deepcopy(row);old=str(out['id'])
            if old in used:
                candidate=f'{subject}_non_dictionary_{old}';suffix=1
                while candidate in used:
                    suffix+=1;candidate=f'{subject}_non_dictionary_{old}_{suffix}'
                out['id']=candidate
                mapping.append({'origin':origin,'old_id':old,'new_id':candidate,'name':out['name'],'knowledge_point':out['knowledge_point']})
            used.add(out['id']);selected.append((index,out))
            for k in keys:seen[k]={'id':out['id'],'origin':origin}
        merged.extend(out for _,out in sorted(selected))
    assert len(merged)==len({r['id'] for r in merged})
    return merged,mapping,removed
