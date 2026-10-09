import json,sys
from pathlib import Path
for p in sys.argv[1:]:
    print(p)
    for row in map(json.loads,Path(p).open()):
        print(row.get('record_id'),row.get('name'),{k:len(str(v)) for k,v in row.items() if k in ('definition','description','en_definition','en_description','knowledge_card')})
