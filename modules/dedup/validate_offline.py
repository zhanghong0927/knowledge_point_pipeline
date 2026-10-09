"""Syntax, test and CLI validation; no live model requests or production input."""
import argparse
import ast
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=ROOT/'VALIDATION.json')
    args=parser.parse_args()
    checks=[]
    for path in ROOT.rglob('*.py'):
        ast.parse(path.read_text(encoding='utf-8-sig'))
    commands=[('unit_and_simulated_model_tests',['-m','unittest','discover','-s','tests','-v']),
              ('main_help',['dedup.py','--help']),('run_help',['dedup.py','run','--help']),
              ('batch_help',['dedup.py','batch','--help']),('verify_help',['dedup.py','verify','--help'])]
    for label,command in commands:
        result=subprocess.run([sys.executable,*command],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',
                              env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        checks.append({'check':label,'exit_code':result.returncode,'output':result.stdout+result.stderr})
        if result.returncode:
            print(checks[-1]['output']); raise RuntimeError('Offline validation failed: '+label)
        print(label+': OK',flush=True)
    report={'offline_validation_passed':True,'python':platform.python_version(),'platform':platform.platform(),
            'live_model_calls':0,'production_data_processed':False,'semantic_quality_approved':False,'checks':checks}
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()
