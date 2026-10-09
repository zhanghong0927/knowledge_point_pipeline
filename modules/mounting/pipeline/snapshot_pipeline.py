"""Read-only snapshot of the eight concurrently running subjects."""
import json
from pathlib import Path
root=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
for subject in ('philosophy','economy','military','art','civil_engineering','management','sociology','education'):
    folder=root/subject
    status_path=folder/'status.json'
    status=json.loads(status_path.read_text()) if status_path.is_file() else {}
    boundary=folder/'boundaries_v5'/'summary.json'
    summary=json.loads(boundary.read_text()) if boundary.is_file() else {}
    print(json.dumps(dict(subject=subject,stage=status.get('stage'),status_updated=status.get('updated'),
                          done=(folder/'done.json').exists(),boundary_summary=summary),ensure_ascii=False))
