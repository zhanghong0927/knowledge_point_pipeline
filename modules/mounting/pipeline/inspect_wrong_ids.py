"""Print only identifiers of mismatched model responses."""
import json,sys
from pathlib import Path
folder=Path(sys.argv[1]); ids=set(sys.argv[2:])
inputs={r['request_id']:r for r in map(json.loads,(folder/'input.jsonl').open())}
latest={}
for row in map(json.loads,(folder/'responses.jsonl').open()):latest[row['request_id']]=row
for rid in ids:
    row=latest[rid];item=inputs[rid]
    print('REQUEST',rid,'item_id',item.get('id'),'name',item.get('name'),'knowledge_point',item.get('knowledge_point'))
    for i,a in enumerate(row['first']['attempts']):
        try:
            obj=json.loads(a['raw']['choices'][0]['message']['content'])
            print('ATTEMPT',i,'returned_id',obj.get('request_id'),'judgment',obj.get('judgment'))
        except Exception as exc:print('ATTEMPT',i,'parse',str(exc))
