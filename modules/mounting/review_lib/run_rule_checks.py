"""Read-only batch QA. Python 3.10+, standard library only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import traceback
from check_schema import Schema
from check_name_rules import check_name
from check_duplicates import Duplicates
from check_taxonomy_coverage import Taxonomy

def dump(path,obj):
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')

def fingerprint(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''): h.update(b)
    s=Path(path).stat()
    return dict(path=str(path),size=s.st_size,mtime_ns=s.st_mtime_ns,sha256=h.hexdigest())

def load_records(path):
    # One file at a time, no corpus-wide materialization. JSONL fallback is explicit.
    text=Path(path).read_text(encoding='utf-8-sig')
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('duplicate JSON key: '+k)
            result[k]=v
        return result
    def constant(value): raise ValueError('nonstandard JSON constant: '+value)
    def parse(text): return json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
    try: data=parse(text)
    except json.JSONDecodeError:
        if text.lstrip().startswith('['): raise
        data=[parse(line) for line in text.splitlines() if line.strip()]
        return data,'jsonl'
    if isinstance(data,list): return data,'json_array'
    if isinstance(data,dict): return [data],'json_object'
    raise ValueError('unsupported JSON top level')

def audit_file(source,taxonomy_path,out,scope='node',core_paths=None):
    source=Path(source); out=Path(out); out.mkdir(parents=True,exist_ok=False)
    result=dict(source=str(source),status='running',records=0,duplicate_scope=scope,
                redundancy_acceptance='undetermined_branch_scope' if scope=='node' else 'configured',
                coverage_basis='raw_records_unique_nonempty_id; invalid IDs use row identity; not semantic acceptance')
    counts=Counter(); affected={s:set() for s in ('error','suspect','observation')}
    try:
        result['input']=fingerprint(source)
        records,fmt=load_records(source); result['format']=fmt
        taxonomy=None
        if taxonomy_path:
            result['taxonomy_input']=fingerprint(taxonomy_path)
            try: taxonomy=Taxonomy(json.loads(Path(taxonomy_path).read_text(encoding='utf-8-sig')),core_paths)
            except Exception as e: result['taxonomy_error']=str(e)
        else: result['taxonomy_error']='missing_or_ambiguous_taxonomy'
        schema=Schema(); duplicates=Duplicates(scope,taxonomy)
        with (out/'issues.jsonl').open('w',encoding='utf-8') as issues:
            def emit(row,r,level,code,field=None):
                counts[level+':'+code]+=1
                if row: affected[level].add(row)
                issues.write(json.dumps(dict(row=row,id=r.get('id') if isinstance(r,dict) else None,
                    name=r.get('name') if isinstance(r,dict) else None,level=level,code=code,field=field,
                    value=r.get(field) if field and isinstance(r,dict) else None),ensure_ascii=False)+'\n')
            if fmt!='json_array': emit(0,{},'error','top_level_not_array')
            for row,r in enumerate(records,1):
                result['records']=row
                for level,code in schema.check(r): emit(row,r,level,code)
                if not isinstance(r,dict): continue
                for field in ('name','knowledge_point'):
                    for level,code in check_name(r.get(field),field): emit(row,r,level,code,field)
                duplicates.add(r,row)
                if taxonomy:
                    for level,code in taxonomy.add(r,row): emit(row,r,level,code,'main_tags' if 'main' in code else 'related_tags')
        duplicate_rows=set(); groups=0
        with (out/'duplicates.jsonl').open('w',encoding='utf-8') as f:
            for group in duplicates.groups():
                group['ids']=[records[row-1].get('id') for row in group['rows']]
                f.write(json.dumps(group,ensure_ascii=False)+'\n'); groups+=1; duplicate_rows.update(group['rows'])
        result.update(duplicate_groups=groups,duplicate_affected_records=len(duplicate_rows),duplicate_unresolved_records=duplicates.unresolved)
        if taxonomy:
            result['coverage']=taxonomy.report()
            result['taxonomy_nodes']=len(taxonomy.nodes)
            with (out/'nodes.jsonl').open('w',encoding='utf-8') as f:
                for p,n in taxonomy.nodes.items():
                    f.write(json.dumps(dict(path=p,**n,main_count=taxonomy.count(p,'main'),combined_count=taxonomy.count(p,'combined')),ensure_ascii=False)+'\n')
        result['issues']=dict(counts)
        result['affected_records']={k:len(v) for k,v in affected.items()}
        result['input_after']=fingerprint(source)
        result['input_unchanged']=result['input']==result['input_after']
        if taxonomy_path:
            result['taxonomy_unchanged']=result['taxonomy_input']==fingerprint(taxonomy_path)
        result['status']='completed' if result['input_unchanged'] and result.get('taxonomy_unchanged',True) else 'input_changed'
    except Exception as e:
        result.update(status='failed',failure=str(e),traceback=traceback.format_exc())
    dump(out/'summary.json',result)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True,help='JSON/JSONL file or directory, recursively inventoried')
    p.add_argument('--taxonomy-dir',help='Recursive search for {subject}_taxonomy.json')
    p.add_argument('--config',help='JSON subjects mapping: subject -> taxonomy, core_paths, duplicate_scope')
    p.add_argument('--duplicate-scope',default='node',help='node (diagnostic) or ancestor depth 1/2/3/...')
    p.add_argument('--output',required=True,help='Must be a new directory outside inputs')
    a=p.parse_args(); root=Path(a.input).resolve(); out=Path(a.output).resolve()
    if out==root or root in out.parents: p.error('output must be outside input tree')
    if a.taxonomy_dir and (Path(a.taxonomy_dir).resolve()==out or Path(a.taxonomy_dir).resolve() in out.parents): p.error('output must be outside taxonomy tree')
    out.mkdir(parents=True,exist_ok=False)
    config=json.loads(Path(a.config).read_text(encoding='utf-8-sig')) if a.config else {}
    files=sorted(f for f in root.rglob('*') if f.is_file() and f.suffix.lower() in ('.json','.jsonl')) if root.is_dir() else [root]
    dump(out/'inventory.json',dict(created_utc=datetime.now(timezone.utc).isoformat(),files=[str(f) for f in files],config=config))
    results=[]
    for index,f in enumerate(files,1):
        subject=f.stem.removesuffix('_knowledge_point'); cfg=config.get('subjects',{}).get(subject,{})
        candidates=list(Path(a.taxonomy_dir).rglob(subject+'_taxonomy.json')) if a.taxonomy_dir else []
        tax=cfg.get('taxonomy') or (str(candidates[0]) if len(candidates)==1 else None)
        r=audit_file(f,tax,out/(str(index).zfill(2)+'_'+subject),cfg.get('duplicate_scope',a.duplicate_scope),cfg.get('core_paths'))
        r['subject']=subject; r['standard_filename']=f.name==subject+'_knowledge_point.json'; results.append(r)
        dump(out/'summary.json',results)
        print(json.dumps(dict(index=index,total=len(files),subject=subject,status=r['status'],records=r['records']),ensure_ascii=False),flush=True)
    lines=['# 全量知识点规则检查测试结果','',
        '只读检查；未执行语义质量判断或数据修复。空中英文名仅观察；疑似项不计作确定错误。',
        '同节点重复是默认诊断口径，尚不能等同已确认的同分支验收。覆盖率为原始有效路径挂载统计；重复ID可能低估覆盖，应先修复后重算。',
        '叶子直接计数；L3自身和后代汇总，按非空字符串ID去重。核心名单未提供则不可评估。',
        '', '| 学科 | 记录数 | 确定规则问题涉及条数 | 疑似涉及条数 | 同节点重复涉及条数 | 分类树/执行状态 |', '|---|---:|---:|---:|---:|---|']
    for r in results:
        aff=r.get('affected_records',{})
        lines.append('| '+ ' | '.join(map(str,[r['subject'],r['records'],aff.get('error','—'),aff.get('suspect','—'),r.get('duplicate_affected_records','—'),r.get('taxonomy_error',r['status'])]))+' |')
    lines+=['','完整问题见各子目录 issues.jsonl；重复组见 duplicates.jsonl；节点计数见 nodes.jsonl；两套覆盖率与阈值判定见 summary.json。']
    (out/'报告.md').write_text('\n'.join(lines),encoding='utf-8')
    return 1 if not results or any(r['status']!='completed' for r in results) else 0

if __name__=='__main__': raise SystemExit(main())
