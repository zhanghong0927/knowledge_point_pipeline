"""Vendor existing dedup primitives and create a source-tracked portable ZIP."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parent
SOURCES=Path('D:/七月工作')


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def vendor():
    rule=SOURCES/'non_dictionary_closeout_20260930/closeout_core.py'
    helper=SOURCES/'机械工程知识点抽取/交付/跨学科知识点同义去重工具包_20260820/scripts/02_llm_review_and_fuse.py'
    shutil.copyfile(rule,ROOT/'legacy_rules.py')
    text=helper.read_text(encoding='utf-8-sig'); lines=text.splitlines(keepends=True)
    functions=('normalize_api_url','extract_json_object','normalize_partition')
    selected={n.name:n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef)}
    code='"""Unmodified selected helpers from the previous dedup toolkit; no fusion code."""\nimport json\nimport re\nfrom typing import Any\n\n'
    for name in functions:
        n=selected[name]; code+=''.join(lines[n.lineno-1:n.end_lineno])+'\n\n'
    (ROOT/'legacy_helpers.py').write_text(code,encoding='utf-8')
    provenance=[{'file':'legacy_rules.py','source':str(rule),'source_sha256':sha(rule),'sha256':sha(ROOT/'legacy_rules.py')},
                {'file':'legacy_helpers.py','source':str(helper),'source_sha256':sha(helper),
                 'functions':list(functions),'sha256':sha(ROOT/'legacy_helpers.py')}]
    (ROOT/'SOURCE_PROVENANCE.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')


def archive():
    files=[p for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts
           and p.suffix not in ('.pyc','.pyo') and p.name!='FILES_SHA256.json']
    hashes={p.relative_to(ROOT).as_posix():sha(p) for p in files}
    manifest=ROOT/'FILES_SHA256.json'
    manifest.write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
    target=ROOT.with_suffix('.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for p in files+[manifest]: z.write(p,ROOT.name+'/'+p.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for name,h in hashes.items(): assert hashlib.sha256(z.read(ROOT.name+'/'+name)).hexdigest()==h
    print(json.dumps({'zip':str(target),'files':len(files)+1,'bytes':target.stat().st_size,'sha256':sha(target)},ensure_ascii=False))


if __name__=='__main__':
    import sys
    archive() if '--archive' in sys.argv else vendor()
