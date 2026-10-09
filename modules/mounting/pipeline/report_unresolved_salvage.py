"""Read-only concise unresolved salvage audit."""
import json,sys
from pathlib import Path
for dirname in sys.argv[1:]:
    folder=Path(dirname)
    print('\n',folder)
    for row in map(json.loads,(folder/'unresolved.jsonl').open()):
        print(json.dumps(dict(request_id=row['request_id'],first_checks=row['first_checks'],
                              second_checks=row['second_checks']),ensure_ascii=False))
