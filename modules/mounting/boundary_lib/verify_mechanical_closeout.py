import json,hashlib
from pathlib import Path
p=Path('/home/wangqiyuan/work/semantic_boundaries_full_ab_20260922/mechanical_full_closeout_v2_20260922')
def rows(f):return [json.loads(s) for s in (p/f).read_text().splitlines() if s.strip()]
state=json.loads(p.with_suffix('.state.json').read_text());assert state['stage'].startswith('completed')
b=rows('baseline/final.jsonl');e=rows('effect_review/final.jsonl');d=rows('effect_details.jsonl')
assert len(b)==23748 and len({r['item']['request_id'] for r in b})==23748
cohort=json.loads((p/'remount_cohort.json').read_text());ids={r['request_id'] for r in cohort}
assert len(ids)==len(cohort)
for v in ['with_cards','without_cards']:
 r=rows('remount/'+v+'.jsonl');assert len(r)==len(ids) and {x['record_id'] for x in r}==ids
for v in ['original','with_cards','without_cards']:
 r=[x for x in d if x['variant']==v];assert len(r)==len(ids) and {x['record_id'] for x in r}==ids
manifest=json.loads((p/'manifest.json').read_text());assert hashlib.sha256(Path(manifest['source']).read_bytes()).hexdigest()==manifest['source_sha256']
bad=[dict(phase=phase,**r) for phase,data in [('baseline',b),('effect_review',e)] for r in data if r['review']['judgment']=='technical_failure']
(p/'remaining_technical_failures.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in bad))
counts={phase:sum(r['phase']==phase for r in bad) for phase in ['baseline','effect_review']}
report=p/'全量审核与重挂对照结果.md'
text=report.read_text();text+='\n\n## 剩余技术异常\n\n初审 '+str(counts['baseline'])+' 条；最终复核 '+str(counts['effect_review'])+' 个独立请求。未作有效质量判定；详见 remaining_technical_failures.jsonl。\n'
report.write_text(text)
print(json.dumps(dict(verified=True,source=len(b),cohort=len(ids),failures=counts,summary=json.loads((p/'effect_summary.json').read_text())['all']),ensure_ascii=False))
