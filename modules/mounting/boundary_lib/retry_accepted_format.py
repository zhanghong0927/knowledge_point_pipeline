"""Retry only failed review serializations, retaining original attempts."""
import json,sys
from pathlib import Path
from ab_workflow import BASE,MODEL,write_json,write_lines
from accepted_review import metrics
sys.path.insert(0,'/home/wangqiyuan/work/knowledge_rule_checks_20260922')
import mount_pilot_v3 as reviewer
p=Path(sys.argv[1]);out=p/'format_retry';out.mkdir(exist_ok=False)
items={r['request_id']:r for r in map(json.loads,(p/'input.jsonl').read_text(encoding='utf-8').splitlines())}
results={r['request_id']:r for r in map(json.loads,(p/'responses.jsonl').read_text(encoding='utf-8').splitlines())}
extra='\n本轮仅修正输出证据格式。仍须阅读全部中英文定义和描述判断。为避免长文本排版差异，evidence仅从name、knowledge_point、main_tags中选择连续原文；语义推理写入reason。不可因上次技术失败而改变判定倾向。'
reviewer.PROMPT+=extra;reviewer.COUNTER_PROMPT+=extra
(out/'prompt.txt').write_text(reviewer.PROMPT,encoding='utf-8')
(out/'counter_prompt.txt').write_text(reviewer.COUNTER_PROMPT,encoding='utf-8')
with (out/'responses.jsonl').open('w',encoding='utf-8') as f:
    for rid,old in list(results.items()):
        if old['final']['judgment']!='technical_failure':continue
        r=reviewer.review_one(BASE,MODEL,items[rid]);results[rid]=r
        f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush();print(rid,r['final']['judgment'],flush=True)
links=json.loads((p/'links.json').read_text(encoding='utf-8'))
details=[dict(link,record=items[link['request_id']],review=results[link['request_id']]['final']) for link in links]
write_lines(out/'details.jsonl',details)
summary={s:{v:metrics([x['review']['judgment'] for x in details if x['subject']==s and x['variant']==v]) for v in ['with_cards','without_cards']} for s in ['mechanical','architecture']}
write_json(out/'summary.json',summary)
print(json.dumps(summary,ensure_ascii=False),flush=True)
