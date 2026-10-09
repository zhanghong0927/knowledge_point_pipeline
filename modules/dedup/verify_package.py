"""Check the portable toolkit file hashes without modifying it."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    files=json.loads((ROOT/'FILES_SHA256.json').read_text(encoding='utf-8'))
    for relative,expected in files.items():
        path=ROOT/relative
        if not path.resolve().is_relative_to(ROOT.resolve()): raise ValueError('Unsafe manifest path')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected: raise ValueError('Hash mismatch: '+relative)
    print(json.dumps({'verified_files':len(files),'sha256_ok':True}))


if __name__=='__main__': main()
