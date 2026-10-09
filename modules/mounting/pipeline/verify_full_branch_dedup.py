"""Independent output checks for all completed exact-leaf dedup subjects."""
import json
from pathlib import Path

ROOT=Path('/home/wangqiyuan/work/full_exact_path_dedup_20260923')

def rows(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():yield json.loads(line)

def norm(value):return value.strip().casefold() if isinstance(value,str) else ''
def branch(row):
    value=row.get('main_tags')
    return value if isinstance(value,str) else ''

def main():
    report=json.loads((ROOT/'overall_summary.json').read_text())
    total=0;without_total=0
    for summary in report:
        if 'dedup_total' not in summary:continue
        d=ROOT/summary['subject'];seen=set();count=0;dict_count=0;without_ids=[]
        for row in rows(d/'full_deduplicated.jsonl'):
            count+=1;dict_count+=row['origin']=='dictionary'
            part=branch(row)
            if part:
                for field in ('name','knowledge_point'):
                    value=norm(row.get(field))
                    if not value:continue
                    key=(part,field,value)
                    if key in seen:raise ValueError((summary['subject'],'duplicate remaining',key))
                    seen.add(key)
            if row['origin']!='dictionary':without_ids.append(str(row.get('id')))
        other_ids=[str(row.get('id')) for row in rows(d/'without_dictionary.jsonl')]
        audit_count=0
        for audit in rows(d/'dedup_audit.jsonl'):
            audit_count+=1
            if audit['removed_origin']=='dictionary' and audit['kept_origin']!='dictionary':
                raise ValueError((summary['subject'],'dictionary lost priority',audit))
        assert count==summary['dedup_total']
        assert dict_count==summary['dictionary_retained']
        assert len(other_ids)==summary['without_dictionary'] and other_ids==without_ids
        assert audit_count==summary['removed']
        assert count+audit_count==summary['full_input']+summary['dictionary_input']
        total+=count;without_total+=len(other_ids)
        print(json.dumps(dict(subject=summary['subject'],dedup=count,without_dictionary=len(other_ids),remaining_duplicates=0),ensure_ascii=False),flush=True)
    print(json.dumps(dict(total=total,without_dictionary=without_total),ensure_ascii=False))

if __name__=='__main__':main()
