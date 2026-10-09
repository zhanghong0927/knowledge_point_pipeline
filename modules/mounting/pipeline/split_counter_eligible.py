"""Split unresolved first-pass items by whether a safe unreasonable verdict exists."""
import argparse
import json
from pathlib import Path
from salvage_exact_evidence import salvage

parser=argparse.ArgumentParser()
parser.add_argument('source',type=Path)
parser.add_argument('salvage',type=Path)
parser.add_argument('output',type=Path)
args=parser.parse_args()
input_path=args.source/'input.jsonl'
if not input_path.is_file():input_path=args.source/'samples.jsonl'
with input_path.open(encoding='utf-8') as stream:
    items={r['request_id']:r for r in map(json.loads,stream)}
latest={}
with (args.source/'responses.jsonl').open(encoding='utf-8') as stream:
    for row in map(json.loads,stream):latest[row['request_id']]=row
with (args.salvage/'unresolved.jsonl').open(encoding='utf-8') as stream:
    unresolved=list(map(json.loads,stream))
eligible=[];other=[]
for row in unresolved:
    rid=row['request_id'];first,_=salvage(latest[rid]['first'],items[rid])
    (eligible if first and first['judgment']=='unreasonable' else other).append(row)
args.output.mkdir(parents=True,exist_ok=False)
for name,rows in [('unresolved.jsonl',eligible),('other.jsonl',other)]:
    (args.output/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
print(json.dumps(dict(eligible=len(eligible),other=[r['request_id'] for r in other]),ensure_ascii=False))
