"""Read-only inventory of current full and dictionary knowledge-point deliveries."""
import hashlib
import json
from pathlib import Path
from collections import Counter

ROOT=Path('/mnt/nas_si002991c1cm/deliver')
SUBJECTS=[
    ('mechanical_engineering','mechanical_engineering_knowledge_point.json','mechanical_engineering_knowledge_point.json'),
    ('history','history_knowledge_point.json','history_knowledge_point.json'),
    ('sociology','sociology_knowledge_point.json','sociology_knowledge_point.json'),
    ('civil_engineering','civil_engineering_knowledge_point.json','civil_engineering_knowledge_point.jsonl'),
    ('architecture','architecture_knowledge_points.json','architecture_knowledge_points.json'),
    ('philosophy','philosophy_knowledge_point.json','philosophy_knowledge_point.json'),
    ('literature','literature_knowledge_point.json','literature_knowledge_point.json'),
    ('art','art_knowledge_point.json','art_knowledge_point.json'),
    ('economy','economics_knowledge_point.json','economics_knowledge_point.json'),
    ('military','military_knowledge_point.json','military_knowledge_point.json'),
    ('education','education_knowledge_point.json','education_knowledge_point.json'),
    ('management','management_knowledge_point.json','management_knowledge_point.json'),
]

def read(path):
    text=path.read_text(encoding='utf-8-sig')
    try:
        data=json.loads(text)
        if isinstance(data,list):return data
        if isinstance(data,dict):
            for key in ('data','records','knowledge_points'):
                if isinstance(data.get(key),list):return data[key]
    except json.JSONDecodeError:pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]

def norm(value):return value.strip().casefold() if isinstance(value,str) else ''
def branch(row):
    tag=row.get('main_tags')
    if isinstance(tag,str):return tag.strip('/').split('/')[0]
    return ''

def fingerprint(row):
    return (branch(row),norm(row.get('name')),norm(row.get('knowledge_point')))

def main():
    out=[]
    for subject,full_name,dict_name in SUBJECTS:
        full=ROOT/'knowledge_points'/full_name
        dictionary=ROOT/'dictionaries'/subject/dict_name
        if not dictionary.is_file():
            print(json.dumps(dict(subject=subject,error='dictionary missing',path=str(dictionary)),ensure_ascii=False),flush=True)
            continue
        drows=read(dictionary);dids={str(r.get('id')) for r in drows};dfp={fingerprint(r) for r in drows}
        info=dict(subject=subject,full_path=str(full),dict_path=str(dictionary),dict_count=len(drows),
                  dict_mtime=dictionary.stat().st_mtime_ns,dict_bytes=dictionary.stat().st_size)
        if full.is_file():
            frows=read(full);fids={str(r.get('id')) for r in frows};ffp={fingerprint(r) for r in frows}
            info.update(full_count=len(frows),full_mtime=full.stat().st_mtime_ns,full_bytes=full.stat().st_size,
                        id_overlap=len(fids&dids),fingerprint_overlap=len(ffp&dfp),
                        dictionary_missing_from_full_ids=len(dids-fids),
                        dictionary_missing_from_full_fingerprints=len(dfp-ffp),
                        branches=len({branch(r) for r in frows}),empty_branch=sum(not branch(r) for r in frows),
                        sample_branch=list(Counter(branch(r) for r in frows).items())[:5])
            del frows
        else:info['error']='full file missing'
        out.append(info);print(json.dumps(info,ensure_ascii=False),flush=True)
        del drows
    target=Path('/home/wangqiyuan/work/full_branch_dedup_20260923')
    target.mkdir(exist_ok=True)
    (target/'inventory.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
