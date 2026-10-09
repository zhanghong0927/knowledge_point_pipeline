"""Run portable offline regression suites and CLI/schema checks, without live models."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    result = {'live_model_calls':False,'suites':[],'cli':[],'scope_configs':[],'checks':{}}
    suites = [('extract_clean_adapters','tests','src'+os.pathsep+'.'),
              ('classification','classification/tests','classification/scripts'),
              ('screening','screening','screening')]
    for name,folder,pythonpath in suites:
        env = {**os.environ,'PYTHONPATH':pythonpath+os.pathsep+os.environ.get('PYTHONPATH','')}
        started = time.monotonic()
        run = subprocess.run([sys.executable,'-m','unittest','discover','-s',folder,'-p','test_*.py','-v'],
                             cwd=ROOT,env=env,capture_output=True,text=True,timeout=240)
        log = run.stdout+run.stderr
        (args.out/(name+'.log')).write_text(log,encoding='utf-8')
        count = re.search(r'Ran (\d+) tests?',log)
        skip = re.search(r'skipped=(\d+)',log)
        result['suites'].append({'name':name,'exit_code':run.returncode,
            'tests':int(count[1]) if count else None,'skipped':int(skip[1]) if skip else 0,
            'seconds':round(time.monotonic()-started,3)})
    scripts = ['portable_pipeline.py','src/run_fullbook_v5.py','src/fullbook_llm_v5.py',
               'src/fullbook_llm_v5_retry16.py','classification/scripts/classify_books.py',
               'screening/dictionary_md_audit.py','name_filters/01_name_title_quality.py',
               'name_filters/02_subject_scope_filter.py']
    for script in scripts:
        run = subprocess.run([sys.executable,script,'--help'],cwd=ROOT,capture_output=True,text=True,timeout=30)
        result['cli'].append({'script':script,'exit_code':run.returncode})
        (args.out/(Path(script).stem+'_help.txt')).write_text(run.stdout+run.stderr,encoding='utf-8')
    spec = importlib.util.spec_from_file_location('scope_schema',ROOT/'name_filters/02_subject_scope_filter.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path in sorted((ROOT/'scope_configs').glob('*.json')):
        try:
            module.load_scope_config(path)
            result['scope_configs'].append({'file':path.name,'valid':True})
        except Exception as exc:
            result['scope_configs'].append({'file':path.name,'valid':False,'error':repr(exc)})
    result['reference_only_configs'] = [str(p.relative_to(ROOT))
        for p in sorted((ROOT/'scope_configs/reference_only').glob('*.json'))]
    provenance = json.loads((ROOT/'SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    exact = [p for p in provenance if (ROOT/p['package']).is_file()]
    result['checks']['source_hashes_match'] = all(
        hashlib.sha256((ROOT/p['package']).read_bytes()).hexdigest()==p['sha256'] for p in exact)
    result['checks']['source_files_verified'] = len(exact)
    files = [p for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts
             and p.suffix in ('.py','.json','.md','.txt','.cjs')]
    errors = []
    for path in files:
        if path.suffix=='.py':
            try: ast.parse(path.read_text(encoding='utf-8-sig'))
            except Exception as exc: errors.append({'file':str(path.relative_to(ROOT)),'error':repr(exc)})
    result['checks']['python_syntax_errors'] = errors
    result['checks']['possible_secret_files'] = [str(p.relative_to(ROOT)) for p in files
        if re.search(r'\bsk-[A-Za-z0-9_-]{12,}',p.read_text(encoding='utf-8-sig'))]
    result['offline_verified'] = (all(s['exit_code']==0 and s['tests'] for s in result['suites'])
        and all(c['exit_code']==0 for c in result['cli'])
        and all(s['valid'] for s in result['scope_configs'])
        and result['checks']['source_hashes_match'] and not errors
        and not result['checks']['possible_secret_files'])
    result['limitations'] = ['No live model batch or semantic quality certification',
                            'Historical data-dependent fixtures may be explicitly skipped',
                            'Optional tokenizer and Node Excel exporter are not installed by this validation']
    (args.out/'VALIDATION.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['offline_verified'] else 1


if __name__=='__main__':
    raise SystemExit(main())
