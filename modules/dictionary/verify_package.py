import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root/'MANIFEST_SHA256.json').read_text(encoding='utf-8'))
failures = []
for name, expected in manifest.items():
    path = (root/name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        failures.append(name)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        failures.append(name)
print(json.dumps({'verified':not failures,'files':len(manifest),'failures':failures},ensure_ascii=False))
raise SystemExit(1 if failures else 0)
