"""Verify the packaged files against the SHA256 manifest, without network access."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    hashes = json.loads((ROOT/'MANIFEST_SHA256.json').read_text(encoding='utf-8'))
    for name, expected in hashes.items():
        path = ROOT/name
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Manifest path escapes package')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Hash mismatch: '+name)
    print(json.dumps({'verified_files':len(hashes),'sha256_ok':True}))


if __name__ == '__main__':
    main()
