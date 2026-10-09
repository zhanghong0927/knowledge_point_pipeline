"""Read-only current dictionary duplicate audit; exact and normalized separate."""
import sys,json
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timezone
from run_rule_checks import load_records,fingerprint,dump

SUBJECTS=[('机械','mechanical_engineering'),('历史学','history'),('社会学','sociology'),('土木','civil_engineering'),('建筑','architecture'),('哲学','philosophy'),('文学','literature'),('艺术学','art'),('经济学','economy'),('军事学','military'),('教育学','education'),('管理学','management')]

def main():
    root=Path(sys.argv[1]); out=Path(sys.argv[2]); out.mkdir(parents=True,exist_ok=False)
    summaries=[]
    for label,subject in SUBJECTS:
        files=sorted(f for f in (root/subject).rglob('*') if f.is_file() and f.suffix.lower() in ('.json','.jsonl') and 'knowledge_point' in f.name)
        result=dict(subject=subject,label=label,candidates=[str(f) for f in files])
        if len(files)!=1:
            result['status']='missing_or_ambiguous'; summaries.append(result); continue
        path=files[0]; before=fingerprint(path); records,fmt=load_records(path)
        index={f:defaultdict(list) for f in ('name','knowledge_point')}
        normalized={f:defaultdict(list) for f in index}
        empty=defaultdict(int)
        for row,r in enumerate(records,1):
            if not isinstance(r,dict): empty['non_object']+=1; continue
            for field in index:
                value=r.get(field)
                if not isinstance(value,str) or not value.strip(): empty[field]+=1; continue
                index[field][value].append(row)
                key=' '.join(value.split())
                if field=='knowledge_point': key=key.casefold()
                normalized[field][key].append(row)
        affected=set(); same_node=set(); groups_by_field={}; normalized_extra={}
        with (out/(subject+'_duplicates.jsonl')).open('w',encoding='utf-8') as output:
            for field,values in index.items():
                groups=0; field_rows=set(); excess=0; cross=0; same_groups=0
                for value,rows in values.items():
                    if len(rows)<2: continue
                    groups+=1; excess+=len(rows)-1; field_rows.update(rows); affected.update(rows)
                    by_path=defaultdict(list)
                    for row in rows:
                        p=records[row-1].get('main_tags')
                        if isinstance(p,str) and p.strip(): by_path[p].append(row)
                    repeated_paths={p:rs for p,rs in by_path.items() if len(rs)>1}
                    for rs in repeated_paths.values(): same_node.update(rs)
                    same_groups+=len(repeated_paths)
                    if len(by_path)>1: cross+=1
                    output.write(json.dumps(dict(field=field,value=value,rows=rows,same_node_groups=repeated_paths,
                        records=[dict(row=row,**{k:records[row-1].get(k) for k in ('id','name','knowledge_point','main_tags','source')}) for row in rows]),ensure_ascii=False)+'\n')
                groups_by_field[field]=dict(groups=groups,affected=len(field_rows),excess_within_field=excess,cross_node_groups=cross,same_node_groups=same_groups)
        with (out/(subject+'_normalization_candidates.jsonl')).open('w',encoding='utf-8') as output:
            for field,values in normalized.items():
                count=0
                for key,rows in values.items():
                    originals=set(records[row-1][field] for row in rows)
                    if len(rows)>1 and len(originals)>1:
                        count+=1; output.write(json.dumps(dict(field=field,key=key,rows=rows,originals=sorted(originals)),ensure_ascii=False)+'\n')
                normalized_extra[field]=count
        after=fingerprint(path)
        result.update(status='completed' if before==after else 'input_changed',input=before,format=fmt,records=len(records),fields=groups_by_field,
            duplicate_affected_records=len(affected),same_node_affected_records=len(same_node),normalization_candidate_groups=normalized_extra,empty_or_invalid=dict(empty),input_unchanged=before==after)
        summaries.append(result); dump(out/'summary.json',summaries)
        print(label,result['records'],groups_by_field,len(affected),flush=True)
    dump(out/'summary.json',summaries)
    lines=['# 12学科辞海知识点重复检查','',datetime.now(timezone.utc).isoformat(),'',
        '严格重复：非空中文name或英文knowledge_point原值完全相同。空值不参与；中英文涉及记录取并集，不能把两列相加。',
        '同挂载节点重复优先复核；跨节点同名可能合理存在，不直接建议删除。英文大小写及多余空白归一化候选另存，不混入严格重复。',
        '重复组不等于应删除条数，也未判断同名异义或保留版本质量。只读检查，未去重。','',
        '| 学科 | 总条数 | 中文重复组 | 英文重复组 | 重复涉及条数（合并去重） | 其中同节点重复涉及条数 |',
        '|---|---:|---:|---:|---:|---:|']
    for r in summaries:
        if r['status']!='completed': lines.append('| '+r['label']+' | '+r['status']+' | | | | |'); continue
        lines.append('| '+' | '.join(map(str,[r['label'],r['records'],r['fields']['name']['groups'],r['fields']['knowledge_point']['groups'],r['duplicate_affected_records'],r['same_node_affected_records']]))+' |')
    lines+=['','## 输入文件','']+[r['label']+'：`'+r['input']['path']+'`' for r in summaries if 'input' in r]
    (out/'重复检查报告.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__': main()
