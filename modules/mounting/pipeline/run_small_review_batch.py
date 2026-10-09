"""Resume small failed review batches without first-item probe gate."""
import argparse
import json
from collections import Counter
from pathlib import Path
from core import load_records
from runner import review_batch

parser=argparse.ArgumentParser()
parser.add_argument('--input',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
args=parser.parse_args()
items=load_records(args.input)
assert len(items)==len({x['request_id'] for x in items})
results=review_batch(items,args.output,chunk_size=80)
print(json.dumps(dict(count=len(results),verdicts=dict(Counter(r['judgment'] for r in results.values()))),ensure_ascii=False))
