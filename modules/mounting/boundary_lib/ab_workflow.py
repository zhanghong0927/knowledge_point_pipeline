"""Prepare reproducible same-input card/no-card mounting comparison assets."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

KEEP=('id','name','knowledge_point','definition','en_definition','description','en_description','source')
BASE='http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
MODEL='/mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B'

def select_sample(rows,count,seed):
    valid=[(i,r) for i,r in enumerate(rows) if isinstance(r,dict) and (r.get('name') or r.get('knowledge_point'))]
    ranked=sorted(valid,key=lambda ir:hashlib.sha256((seed+'|'+str(ir[0])+'|'+str(ir[1].get('id',''))).encode()).hexdigest())[:count]
    return [dict(source_index=i,original_id=r.get('id'),original_main_tags=r.get('main_tags'),input=dict({k:r[k] for k in KEEP if k in r},record_id=seed+'_'+str(i))) for i,r in ranked]

def check_assets(tree,cards):
    codes=[]
    def walk(n):
        codes.append(n['code'])
        for c in n.get('children',[]):walk(c)
    walk(tree);ids=[c['node_code'] for c in cards]
    if len(codes)!=len(set(codes)) or len(ids)!=len(set(ids)) or set(codes)!=set(ids):raise ValueError('tree/card coverage mismatch')
    if any(not isinstance(c.get('semantic_card'),str) or not c['semantic_card'].strip() for c in cards):raise ValueError('empty generated card')
    return codes

def empty_cards(tree,cards):
    return [dict(node_code=c,semantic_card='',seed_examples=[]) for c in check_assets(tree,cards)]

def write_json(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
def write_lines(path,rows):path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')

def prepare_sample(source,out,subject,count):
    out.mkdir(parents=True,exist_ok=False)
    raw=source.read_bytes();data=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data,list):raise ValueError('knowledge source must be array')
    picked=select_sample(data,count,subject+'_20260922')
    if len(picked)!=count:raise ValueError('insufficient input sample')
    write_lines(out/'input.jsonl',[x['input'] for x in picked]);write_json(out/'sample_provenance.json',picked)
    write_json(out/'manifest.json',dict(source=str(source),sha256=hashlib.sha256(raw).hexdigest(),source_records=len(data),sample_records=count,subject=subject,seed=subject+'_20260922',selection='SHA256-ranked eligible row indices',original_mount_used_as_gold=False))
    print(json.dumps(dict(subject=subject,sampled=count,total=len(data)),ensure_ascii=False),flush=True)

def prepare_profiles(full,out,package,subject):
    summary=json.loads((full/'summary.json').read_text(encoding='utf-8'))
    if not summary['full_tree_run']:raise ValueError('full tree generation required')
    tree=json.loads((full/'knowledge_tree.json').read_text(encoding='utf-8'))
    cards=[json.loads(x) for x in (full/'semantic_cards.jsonl').read_text(encoding='utf-8').splitlines()]
    blank=empty_cards(tree,cards)
    for variant,rows in [('with_cards',cards),('without_cards',blank)]:
        profile=out/'profiles'/variant
        shutil.copytree(package/'profiles'/'template',profile)
        (profile/'data').mkdir(exist_ok=True)
        write_json(profile/'data'/'knowledge_tree.json',tree);write_lines(profile/'data'/'semantic_cards.jsonl',rows)
        config=json.loads((profile/'profile.json').read_text(encoding='utf-8'))
        config.update(subject_name=tree['name_zh'],subject_scope='以所提供分类树实际覆盖的主题为范围，包括：'+'、'.join(x['name_zh'] for x in tree['children'])+'。不得仅因跨学科而排除。')
        write_json(profile/'profile.json',config)
    write_json(out/'asset_check.json',dict(nodes=len(cards),cards=len(cards),runtime_over_700=sum(len(c['semantic_card'])>700 for c in cards),tree_sha256=hashlib.sha256((full/'knowledge_tree.json').read_bytes()).hexdigest(),cards_sha256=hashlib.sha256((full/'semantic_cards.jsonl').read_bytes()).hexdigest(),sample_sha256=hashlib.sha256((out/'input.jsonl').read_bytes()).hexdigest(),full_run=str(full)))

def run_mounts(out,package,workers):
    def run(variant):
        cmd=[sys.executable,str(package/'scripts'/'hierarchical-knowledge-labeling-beam-v3.py'),'--input',str(out/'input.jsonl'),'--output',str(out/(variant+'.jsonl')),'--profile-dir',str(out/'profiles'/variant),'--api-url',BASE+'/v1/chat/completions','--model',MODEL,'--workers',str(workers),'--retry-failed-workers',str(workers),'--retry-failed-rounds','1','--retries','1','--timeout','180','--temperature','0','--max-seed-examples','0','--no-progress','--progress-interval','10']
        write_json(out/(variant+'_command.json'),cmd)
        with (out/(variant+'.log')).open('w',encoding='utf-8') as f:code=subprocess.call(cmd,stdout=f,stderr=subprocess.STDOUT)
        print(variant,'exit',code,flush=True)
        return code
    with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(run,['with_cards','without_cards']))
    if any(codes):raise RuntimeError('mapping subprocess failed; inspect logs')

def main():
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['sample','mount']);p.add_argument('--out',type=Path,required=True);p.add_argument('--subject',required=True);p.add_argument('--source',type=Path);p.add_argument('--full',type=Path);p.add_argument('--package',type=Path);p.add_argument('--count',type=int,default=100);p.add_argument('--workers',type=int,default=4)
    a=p.parse_args()
    if a.stage=='sample':prepare_sample(a.source,a.out,a.subject,a.count)
    else:
        prepare_profiles(a.full,a.out,a.package,a.subject);run_mounts(a.out,a.package,a.workers)

if __name__=='__main__':main()
