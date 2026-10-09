"""File-only bridges between 0611 book lists and the existing dictionary MD audit."""
import argparse
from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
TRACKS = ('辞海类','其他重要书籍')
DECISIONS = ('PASS','REVIEW','DROP')


def read_csv(path):
    csv.field_size_limit(2_147_483_647)
    with Path(path).open(encoding='utf-8-sig',newline='') as handle:
        reader = csv.DictReader(line.replace('\x00','') for line in handle)
        return list(reader.fieldnames or []),list(reader)


def write_csv(path,fields,rows):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def write_json(path,value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_ids(rows):
    ids = [r.get('identifier','').strip() for r in rows]
    if any(not identifier for identifier in ids) or len(ids)!=len(set(ids)):
        raise ValueError('Missing or duplicate original book identifiers')
    return ids


def split_tracks(source,out):
    fields,rows = read_csv(source)
    validate_ids(rows)
    if any(r.get('book_track') not in TRACKS for r in rows):
        raise ValueError('Unknown or missing book_track')
    out = Path(out)
    out.mkdir(parents=True,exist_ok=False)
    counts = {}
    for track,name in zip(TRACKS,('dictionary.csv','important.csv')):
        selected = [r for r in rows if r['book_track']==track]
        write_csv(out/name,fields,selected)
        counts[track] = len(selected)
    result = {'input':str(Path(source).resolve()),'input_sha256':sha(source),'counts':counts,'ids_preserved':True}
    write_json(out/'SUMMARY.json',result)
    return result


def prepare_dictionary(source,scope,out,path_maps,source_oss_endpoint=None):
    import general_book_screening_pipeline as general
    fields,rows = read_csv(source)
    validate_ids(rows)
    if any(r.get('book_track','辞海类')!='辞海类' for r in rows):
        raise ValueError('Dictionary preparation cannot accept important-book rows')
    config = json.loads(Path(scope).read_text(encoding='utf-8-sig'))
    if not config.get('subject_name') or not isinstance(config.get('boundary'),dict):
        raise ValueError('Expected subject_name and an explicit boundary object')
    out = Path(out).resolve()
    out.mkdir(parents=True,exist_ok=False)
    (out/'MD').mkdir()
    mappings = general.parse_path_mappings(path_maps)
    before = sha(source)
    records = []
    for index,row in enumerate(rows,1):
        item = {'identifier':row['identifier'],'status':'failed','error':'','file_name':'','local_path':''}
        try:
            uri = row.get('parsed_path','').strip()
            path = general.resolve_parsed_path(uri,mappings)
            if path is None and source_oss_endpoint:
                path = general.fetch_oss_path(uri,out/'cache',None,source_oss_endpoint)
            if path is None or not path.is_file():
                raise FileNotFoundError('MD cannot be resolved from parsed_path')
            if not path.stat().st_size:
                raise ValueError('Empty MD file')
            path = path.resolve()
            title = re.sub(r'[^\w\u4e00-\u9fff .()-]','_',row.get('title',''))[:80].strip(' .') or 'book'
            name = f'{index:06d}__{hashlib.sha256(row["identifier"].encode()).hexdigest()[:12]}__{title}.md'
            dest = out/'MD'/name
            expected = sha(path)
            if os.name=='nt': shutil.copyfile(path,dest)
            else: dest.symlink_to(path)
            if sha(dest)!=expected: raise ValueError('MD changed during preparation')
            item.update(status='ready',file_name=name,local_path=str(dest),source_path=str(path),
                        source_sha256=expected,size=path.stat().st_size)
        except Exception as exc:
            item['error'] = repr(exc)
        records.append(item)
    if sha(source)!=before: raise ValueError('Candidate CSV changed during preparation')
    write_csv(out/'CANDIDATES.csv',fields,rows)
    subject = config['subject_name']
    definition = {'name':subject,'code':'BOOK','sources':['MD'],'sample_count':16,'chunk_chars':1400,
        'book_type_hint':'词典、辞海、百科或术语工具书；需独立词头与对应释义，不按普通章节知识审核。',
        'extraction_focus':'以此学科边界审核独立词条：'+json.dumps(config,ensure_ascii=False)}
    write_json(out/'audit_config.json',{'subjects':[definition]})
    mapping = {'subject':subject,'candidate_sha256':sha(out/'CANDIDATES.csv'),
               'scope_sha256':sha(scope),'records':records}
    write_json(out/'FILEMAP.json',mapping)
    result = {'input':len(rows),'ready':sum(r['status']=='ready' for r in records),
              'technical_failures':sum(r['status']!='ready' for r in records),'ids_preserved':True}
    write_json(out/'PREPARED.json',result)
    return result


def export_results(rows,out):
    validate_ids(rows)
    if any(r.get('book_track') not in TRACKS or r.get('final_decision') not in DECISIONS for r in rows):
        raise ValueError('Invalid final track or decision')
    out = Path(out)
    fields = list(dict.fromkeys(k for r in rows for k in r)) or ['identifier','book_track','final_decision']
    write_csv(out/'最终审核结果.csv',fields,rows)
    counts = {}
    for track in TRACKS:
        counts[track] = {}
        for decision in DECISIONS:
            selected = [r for r in rows if r['book_track']==track and r['final_decision']==decision]
            write_csv(out/track/decision/'本级0611字段书目.csv',fields,selected)
            counts[track][decision] = len(selected)
    technical = [r for r in rows if r.get('pipeline_failure_type') or r.get('audit_status') in
                 ('api_failed','unresolved_md_path','not_run','failed','missing_audit','md_unavailable')]
    write_csv(out/'技术失败待复核.csv',fields,technical)
    result = {'total':len(rows),'counts':counts,'technical_failures':len(technical),
              'coverage_ok':sum(sum(v.values()) for v in counts.values())==len(rows),
              'semantic_quality_approved':False}
    write_json(out/'SUMMARY.json',result)
    return result


def finalize_dictionary(prepared,audit,out):
    prepared = Path(prepared)
    mapping = json.loads((prepared/'FILEMAP.json').read_text(encoding='utf-8'))
    if sha(prepared/'CANDIDATES.csv')!=mapping['candidate_sha256']:
        raise ValueError('Frozen candidates changed')
    _,rows = read_csv(prepared/'CANDIDATES.csv')
    validate_ids(rows)
    mapped = {r['identifier']:r for r in mapping['records']}
    if len(mapped)!=len(mapping['records']) or set(mapped)!={r['identifier'] for r in rows}:
        raise ValueError('Frozen candidate/map identifier sets differ')
    expected = {r['file_name'] for r in mapped.values() if r['status']=='ready'}
    _,audit_rows = read_csv(audit)
    by_file = {}
    for row in audit_rows:
        name = row.get('file_name','')
        if row.get('subject')!=mapping['subject'] or name not in expected or name in by_file:
            raise ValueError('Unexpected or duplicate dictionary audit record')
        if row.get('decision') not in DECISIONS:
            raise ValueError('Invalid dictionary audit decision')
        by_file[name] = row
    final = []
    for original in rows:
        item = mapped[original['identifier']]
        vote = by_file.get(item['file_name'])
        if item['status']=='ready':
            if sha(item['local_path'])!=item['source_sha256'] or sha(item['source_path'])!=item['source_sha256']:
                raise ValueError('Frozen MD changed after preparation')
            if vote and Path(vote['source_path']).resolve()!=Path(item['local_path']).resolve():
                raise ValueError('Audit source path differs from frozen MD')
        row = {**original,'book_track':'辞海类','pipeline_failure_type':''}
        if vote:
            row.update({'v3_'+key:value for key,value in vote.items()})
            row.update(final_decision=vote['decision'],audit_status='completed',audit_summary=vote.get('summary',''))
        else:
            reason = 'md_unavailable' if item['status']!='ready' else 'missing_audit'
            row.update(final_decision='REVIEW',audit_status=reason,pipeline_failure_type=reason,
                       audit_summary=item.get('error') or 'MD审核结果缺失，未形成内容结论。')
        final.append(row)
    result = export_results(final,out)
    result['input'] = len(rows)
    write_json(Path(out)/'SUMMARY.json',result)
    return result


def merge_results(inputs,out):
    rows = []
    for path in inputs: rows.extend(read_csv(path)[1])
    return export_results(rows,out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest='command',required=True)
    split = subs.add_parser('split')
    split.add_argument('--input-csv',type=Path,required=True)
    split.add_argument('--out',type=Path,required=True)
    prep = subs.add_parser('dictionary-prepare')
    prep.add_argument('--input-csv',type=Path,required=True)
    prep.add_argument('--subject-config',type=Path,required=True)
    prep.add_argument('--out',type=Path,required=True)
    prep.add_argument('--path-map',action='append',default=[])
    prep.add_argument('--source-oss-endpoint')
    final = subs.add_parser('dictionary-finalize')
    final.add_argument('--prepared',type=Path,required=True)
    final.add_argument('--audit-results',type=Path,required=True)
    final.add_argument('--out',type=Path,required=True)
    merge = subs.add_parser('merge-results')
    merge.add_argument('--input-csv',action='append',type=Path,required=True)
    merge.add_argument('--out',type=Path,required=True)
    a = p.parse_args()
    if a.command=='split': result = split_tracks(a.input_csv,a.out)
    elif a.command=='dictionary-prepare': result = prepare_dictionary(a.input_csv,a.subject_config,a.out,a.path_map,a.source_oss_endpoint)
    elif a.command=='dictionary-finalize': result = finalize_dictionary(a.prepared,a.audit_results,a.out)
    else: result = merge_results(a.input_csv,a.out)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
