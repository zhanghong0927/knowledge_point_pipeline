"""Run offline tests, validate subject configs, and check CLI entry points."""
import ast
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=ROOT/'VALIDATION.json')
    args = parser.parse_args()
    started = time.monotonic()
    env = {**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'scripts'),str(ROOT)]),
           'PYTHONIOENCODING':'utf-8'}
    checks = []
    def check(name,args):
        result = subprocess.run([sys.executable,*args],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8')
        checks.append({'check':name,'exit_code':result.returncode,'output':(result.stdout+result.stderr).strip()})
        print(name+': '+('OK' if result.returncode == 0 else 'FAILED'),flush=True)
        if result.returncode:
            print(checks[-1]['output'])
            raise RuntimeError('Offline check failed: '+name)
    for path in ROOT.rglob('*.py'):
        ast.parse(path.read_text(encoding='utf-8-sig'))
    check('shared_and_adapter_tests',['-m','unittest','discover','-s','tests'])
    check('dictionary_v3_tests',['-m','unittest','discover','-s','dictionary_md_v3','-p','test_*.py'])
    for path in sorted((ROOT/'scripts').glob('*.py')):
        check(path.name+' --help',[str(path),'--help'])
    for script in ('screening_io.py','dictionary_md_v3/dictionary_md_audit.py'):
        check(script+' --help',[script,'--help'])
    sys.path.insert(0,str(ROOT/'scripts'))
    from stream_subject_recall_0611 import load_subject_config
    from general_book_screening_pipeline import load_subject_config as load_audit_config
    configs = []
    for path in sorted((ROOT/'configs/subject_recall').glob('*.json')):
        if path.name == 'subject_boundary_template.json':
            json.loads(path.read_text(encoding='utf-8-sig'))
            configs.append({'file':path.name,'template':True,'ready_to_run':False})
            continue
        config = load_subject_config(path)
        compatible = ROOT/'configs/audit_compatible'/path.name
        load_subject_config(compatible)
        load_audit_config(compatible)
        configs.append({'file':path.name,'compatible_file':str(compatible.relative_to(ROOT)),
                        'subject':config.subject_name,'slug':config.subject_slug,
                        'template':False,'ready_to_run':True})
    report = {'offline_checks_passed':True,'live_model_calls':0,'live_oss_calls':0,
              'semantic_quality_approved':False,'elapsed_seconds':round(time.monotonic()-started,2),
              'configs':configs,'checks':checks}
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'offline_checks_passed':True,'configs':len(configs)},ensure_ascii=False))


if __name__ == '__main__':
    main()
