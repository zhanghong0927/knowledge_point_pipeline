"""Read-only backlog count by technical-failure stage."""
import json
from pathlib import Path
root=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
for subject in ('mechanical_engineering','architecture','history','literature'):
    d=root/subject
    counts={}
    for stage in ('audit','review'):
        p=d/stage/'final.jsonl';rows=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        counts[stage]=sum(x['review']['judgment']=='technical_failure' for x in rows)
        q=d/('technical_retry_'+stage)/'summary.json'
        if q.exists():counts[stage+'_first_retry_remaining']=json.loads(q.read_text())['remaining']
    p=d/'remount/with_cards.jsonl'
    rows=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    counts['mount']=sum(x['knowledge_labeling']['status']!='ok' for x in rows)
    counts['total']=counts['audit']+counts['review']+counts['mount']
    print(subject,json.dumps(counts,ensure_ascii=False))
