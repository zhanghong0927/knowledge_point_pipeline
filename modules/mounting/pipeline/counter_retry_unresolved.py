"""Independent counter-review only for unresolved first-pass unreasonable verdicts."""
import argparse
import json
from collections import Counter
from pathlib import Path
from runner import reviewer
from salvage_exact_evidence import salvage


def run_counter(item,first,base,model):
    call=reviewer.call_one(base,model,item,True)
    second=call.get('result')
    salvage_checks=[]
    if second is None:
        second,salvage_checks=salvage(call,item)
    return dict(request_id=item['request_id'],first=first,second=second,
                second_call=call,second_salvage_checks=salvage_checks,
                final=reviewer.combine(first,second))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('salvage',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--base',required=True)
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    input_path=args.source/'input.jsonl'
    if not input_path.is_file():input_path=args.source/'samples.jsonl'
    with input_path.open(encoding='utf-8') as stream:
        items={row['request_id']:row for row in map(json.loads,stream)}
    latest={}
    with (args.source/'responses.jsonl').open(encoding='utf-8') as stream:
        for row in map(json.loads,stream):latest[row['request_id']]=row
    with (args.salvage/'unresolved.jsonl').open(encoding='utf-8') as stream:
        unresolved=list(map(json.loads,stream))
    args.output.mkdir(parents=True,exist_ok=False)
    results=[]
    with (args.output/'results.jsonl').open('w',encoding='utf-8') as stream:
        for row in unresolved:
            rid=row['request_id'];item=items[rid]
            first,_=salvage(latest[rid]['first'],item)
            if first is None or first['judgment']!='unreasonable':
                raise ValueError(f'{rid}: first pass not safely unreasonable')
            result=run_counter(item,first,args.base,args.model)
            stream.write(json.dumps(result,ensure_ascii=False)+'\n');stream.flush()
            results.append(result)
    report=dict(source=str(args.source),salvage=str(args.salvage),count=len(results),
                verdicts=dict(Counter(r['final']['judgment'] for r in results)))
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
