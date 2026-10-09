"""Recover review results using only literal model citations; never edit raw responses."""
import argparse
import hashlib
import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, '/home/wangqiyuan/work/knowledge_rule_checks_20260922')
from mount_pilot_v3 import validate_result, combine


def salvage(call, item):
    if not call:
        return None, []
    checks = []
    for i, attempt in enumerate(call['attempts']):
        raw = attempt.get('raw') or {}
        try:
            if raw['choices'][0]['finish_reason'] != 'stop':
                continue
            content = raw['choices'][0]['message']['content'].strip()
            if content.startswith('```'):
                content = content.split('\n',1)[1].rsplit('```',1)[0]
            obj = json.loads(content)
            returned_id = obj.get('request_id')
            corrected_id = None
            if returned_id != item['request_id']:
                if not isinstance(returned_id,str) or SequenceMatcher(None,returned_id,item['request_id']).ratio()<0.7:
                    raise ValueError('unrelated request_id')
                corrected_id = returned_id
                obj['request_id'] = item['request_id']
            discarded = []
            for key in ('support_evidence','conflict_evidence'):
                kept = []
                for ev in obj.get(key,[]):
                    field, quote = ev.get('field'), ev.get('text')
                    source = item.get(field)
                    if isinstance(source,str) and isinstance(quote,str) and quote.strip() and quote in source:
                        kept.append(ev)
                    else:
                        discarded.append(dict(array=key,field=field,text=quote))
                obj[key] = kept
            result = validate_result(obj,item)
            checks.append(dict(attempt=i,corrected_request_id=corrected_id,
                               discarded_evidence=discarded,policy_gates=result['policy_gates']))
            return result, checks
        except Exception as exc:
            checks.append(dict(attempt=i,error=str(exc)))
    return None, checks


def process(folder, output):
    input_path=folder/'input.jsonl'
    if not input_path.is_file():input_path=folder/'samples.jsonl'
    response_path=folder/'responses.jsonl'
    with input_path.open(encoding='utf-8') as stream:
        inputs={row['request_id']:row for row in map(json.loads,stream)}
    latest={}
    with response_path.open(encoding='utf-8') as stream:
        for row in map(json.loads,stream):
            latest[row['request_id']]=row
    repaired=[]; unresolved=[]
    for rid,row in latest.items():
        if row['final']['judgment']!='technical_failure':
            continue
        item=inputs[rid]
        first,first_checks=salvage(row.get('first'),item)
        second=None;second_checks=[]
        if first and first['judgment']=='unreasonable':
            second,second_checks=salvage(row.get('second'),item)
        final=combine(first,second)
        record=dict(request_id=rid,original_final=row['final'],recovered_final=final,
                    first_checks=first_checks,second_checks=second_checks)
        if final['judgment']=='technical_failure':unresolved.append(record)
        else:repaired.append(record)
    output.mkdir(parents=True,exist_ok=False)
    def write_lines(name,rows):
        with (output/name).open('w',encoding='utf-8') as f:
            for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    write_lines('repaired.jsonl',repaired)
    write_lines('unresolved.jsonl',unresolved)
    report=dict(source_dir=str(folder),source_input_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
                source_responses_sha256=hashlib.sha256(response_path.read_bytes()).hexdigest(),
                original_technical=len(repaired)+len(unresolved),recovered_by_verdict=dict(Counter(r['recovered_final']['judgment'] for r in repaired)),
                still_technical=len(unresolved))
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    process(args.source,args.output)
