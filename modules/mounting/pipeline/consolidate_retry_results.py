"""Materialize audited retry overlays without editing source runs."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def merge_stage(original,steps):
    ids=[row['item']['request_id'] for row in original]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate original request IDs')
    merged={rid:dict(row,initial_review=row['review'],resolution_trace=[]) for rid,row in zip(ids,original)}
    for kind,label,rows in steps:
        seen=set()
        for row in rows:
            rid=row['item']['request_id'] if kind=='full' else row['request_id']
            if rid in seen or rid not in merged:raise ValueError(f'duplicate or unknown ID: {rid}')
            seen.add(rid)
            target=merged[rid]
            if kind=='full':
                if row['item']!=target['item']:raise ValueError(f'item changed: {rid}')
                review=row['review']
            elif kind=='recovered':
                review=row['recovered_final']
                if target['review']['judgment']!='technical_failure':raise ValueError(f'recovery target not technical: {rid}')
                if review['judgment']=='technical_failure':raise ValueError(f'recovery remains technical: {rid}')
            elif kind in ('responses','counter'):
                review=row['final']
                if review['judgment']=='technical_failure':continue
                if target['review']['judgment']!='technical_failure':raise ValueError(f'retry target not technical: {rid}')
            else:raise ValueError(f'unknown overlay type: {kind}')
            target['review']=review
            if review!=target['initial_review'] or kind!='full':target['resolution_trace'].append(label)
        if kind=='full' and seen!=set(ids):raise ValueError('full overlay ID set changed')
    return [merged[rid] for rid in ids]


def load_rows(path):
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--step',action='append',default=[],help='kind:label:path')
    args=parser.parse_args()
    steps=[]; sources=[args.source]
    for spec in args.step:
        kind,label,path=spec.split(':',2)
        source=Path(path)
        steps.append((kind,label,load_rows(source)));sources.append(source)
    result=merge_stage(load_rows(args.source),steps)
    args.output.mkdir(parents=True,exist_ok=False)
    with (args.output/'final_reviews.jsonl').open('w',encoding='utf-8') as stream:
        for row in result:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    changed=[row for row in result if row['resolution_trace']]
    with (args.output/'resolution_delta.jsonl').open('w',encoding='utf-8') as stream:
        for row in changed:
            stream.write(json.dumps(dict(request_id=row['item']['request_id'],initial_review=row['initial_review'],
                                         final_review=row['review'],trace=row['resolution_trace']),ensure_ascii=False)+'\n')
    report=dict(source=str(args.source),steps=[dict(kind=k,label=l) for k,l,_ in steps],
                source_hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
                records=len(result),changed=len(changed),initial_counts=dict(Counter(r['initial_review']['judgment'] for r in result)),
                final_counts=dict(Counter(r['review']['judgment'] for r in result)),
                technical_ids=[r['item']['request_id'] for r in result if r['review']['judgment']=='technical_failure'])
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('records','changed','initial_counts','final_counts','technical_ids')},ensure_ascii=False))


if __name__=='__main__':main()
