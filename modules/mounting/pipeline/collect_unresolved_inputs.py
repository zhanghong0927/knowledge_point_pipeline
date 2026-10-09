"""Build an isolated exact-ID retry set from unresolved salvage reports."""
import argparse
import hashlib
import json
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('stage',type=Path)
parser.add_argument('salvage',type=Path)
parser.add_argument('output',type=Path)
args=parser.parse_args()
ids={row['request_id'] for row in map(json.loads,(args.salvage/'unresolved.jsonl').open())}
rows=[row for row in map(json.loads,(args.stage/'input.jsonl').open()) if row['request_id'] in ids]
assert len(rows)==len(ids) and len({r['request_id'] for r in rows})==len(rows)
args.output.parent.mkdir(parents=True,exist_ok=False)
args.output.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
print(json.dumps(dict(count=len(rows),path=str(args.output),sha256=hashlib.sha256(args.output.read_bytes()).hexdigest()),ensure_ascii=False))
