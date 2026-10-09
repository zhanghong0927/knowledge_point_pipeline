"""Read-only summary of boundary groups that exhausted retries."""
import json,sys
from collections import Counter
from pathlib import Path
for dirname in sys.argv[1:]:
    folder=Path(dirname);fail=[];errors=Counter()
    for path in (folder/'groups').glob('*.json'):
        row=json.loads(path.read_text());out=row['output']
        if out['status']!='technical_failure':continue
        entry=dict(file=path.name,targets=[n['code'] for n in row['payload']['targets']],
                   path=[n['path'] for n in row['payload']['targets']],reason=out.get('reason'))
        for round_row in out.get('rounds',[]):
            for step in ('generation','review'):
                call=round_row.get(step)
                if call and call['result'] is None:
                    for attempt in call['attempts']:
                        errors[(step,attempt.get('error'))]+=1
        fail.append(entry)
    print(json.dumps(dict(folder=str(folder),groups=len(fail),nodes=sum(len(x['targets']) for x in fail),
                          errors=[dict(step=k[0],error=k[1],count=v) for k,v in errors.most_common()],
                          first_groups=fail[:5]),ensure_ascii=False))
