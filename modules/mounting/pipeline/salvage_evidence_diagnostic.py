"""Read-only diagnostic: keep only verbatim citations, then re-run policy gates."""
import json
import sys
from pathlib import Path

sys.path.insert(0, '/home/wangqiyuan/work/knowledge_rule_checks_20260922')
from mount_pilot_v3 import validate_result, combine

for dirname in sys.argv[1:]:
    folder = Path(dirname)
    inputs = {r['request_id']: r for r in map(json.loads, (folder/'input.jsonl').open())}
    latest = {}
    for row in map(json.loads, (folder/'responses.jsonl').open()):
        latest[row['request_id']] = row
    print('\nSTAGE', folder.name)
    for rid, row in latest.items():
        if row['final']['judgment'] != 'technical_failure':
            continue
        item = inputs[rid]
        salvaged = {}
        for pass_name in ('first','second'):
            call = row.get(pass_name)
            if not call:
                continue
            candidates = []
            for attempt in call['attempts']:
                raw = attempt.get('raw') or {}
                try:
                    content = raw['choices'][0]['message']['content'].strip()
                    if content.startswith('```'):
                        content = content.split('\n',1)[1].rsplit('```',1)[0]
                    obj = json.loads(content)
                    removed = []
                    for key in ('support_evidence','conflict_evidence'):
                        valid = []
                        for ev in obj.get(key,[]):
                            field, quote = ev.get('field'), ev.get('text')
                            source = item.get(field)
                            if isinstance(source,str) and isinstance(quote,str) and quote.strip() and quote in source:
                                valid.append(ev)
                            else:
                                removed.append((field, quote))
                        obj[key] = valid
                    result = validate_result(obj,item)
                    candidates.append((result,removed))
                except Exception as exc:
                    candidates.append((None,[('validation_error',str(exc))]))
            for result,removed in candidates:
                if result:
                    salvaged[pass_name] = result
                    print(rid, pass_name, 'verdict',result['judgment'],'gates',result['policy_gates'],
                          'kept',len(result['support_evidence'])+len(result['conflict_evidence']),
                          'removed',[(f,str(q)[:45]) for f,q in removed])
                    break
            else:
                print(rid, pass_name, 'no salvage', candidates)
        if salvaged.get('first'):
            first = salvaged['first']
            second = salvaged.get('second')
            print(rid, 'combined', combine(first,second)['judgment'])
