"""Isolated one-record probe for repetition collapse; retains raw model response."""
import argparse
import json
from pathlib import Path
from runner import reviewer,BASE,MODEL
from salvage_exact_evidence import salvage

parser=argparse.ArgumentParser()
parser.add_argument('--input',required=True,type=Path)
parser.add_argument('--request-id',required=True)
parser.add_argument('--output',required=True,type=Path)
args=parser.parse_args()
with args.input.open(encoding='utf-8') as stream:
    rows=[json.loads(line) for line in stream if line.strip()]
items={r['request_id']:r for r in rows}
item=items[args.request_id]
suffix='\n本条若无法从原始字段确定作者或年代，判uncertain。reason不超过60个汉字；禁止列举人物别名、生平或重复句子。'
reviewer.PROMPT+=suffix
reviewer.COUNTER_PROMPT+=suffix
args.output.mkdir(parents=True,exist_ok=False)
(args.output/'input.jsonl').write_text(json.dumps(item,ensure_ascii=False)+'\n',encoding='utf-8')
(args.output/'prompt_suffix.txt').write_text(suffix,encoding='utf-8')
raw=reviewer.review_one(BASE,MODEL,item)
(args.output/'raw_response.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
first,first_checks=salvage(raw['first'],item)
second=None;second_checks=[]
if first and first['judgment']=='unreasonable' and raw.get('second'):
    second,second_checks=salvage(raw['second'],item)
final=reviewer.combine(first,second)
(args.output/'review.json').write_text(json.dumps(dict(request_id=item['request_id'],final=final,
    first_checks=first_checks,second_checks=second_checks),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(request_id=item['request_id'],judgment=final['judgment']),ensure_ascii=False))
