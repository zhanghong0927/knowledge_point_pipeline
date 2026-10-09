"""Read-only diagnostic for unexpectedly large dedup components."""
import json
from collections import Counter
from pathlib import Path
import sys

root=Path('/home/wangqiyuan/work/full_branch_dedup_20260923')
for subject in sys.argv[1:] or ['literature']:
    d=root/subject
    with (d/'dedup_audit.jsonl').open(encoding='utf-8') as stream:
        removed=[json.loads(line) for line in stream]
    counts=Counter((r['kept_id'],r['kept_origin']) for r in removed)
    top=counts.most_common(10)
    wanted={key[0] for key,_ in top}
    with (d/'full_deduplicated.jsonl').open(encoding='utf-8') as stream:
        rows={str(r['id']):r for r in map(json.loads,stream) if str(r['id']) in wanted}
    print(json.dumps(dict(subject=subject,removed_by_reason=dict(Counter(r['reason'] for r in removed)),
                          top_clusters=[dict(kept_id=key[0],origin=key[1],removed=n,name=rows.get(str(key[0]),{}).get('name'),
                                             english=rows.get(str(key[0]),{}).get('knowledge_point'),
                                             branch=rows.get(str(key[0]),{}).get('main_tags')) for key,n in top]),ensure_ascii=False))
