#!/usr/bin/env python3
"""Audit plans and local handoffs without invoking model services."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('integration', 'cleaning'), help=argparse.SUPPRESS)
    parser.add_argument('--report-dir', type=Path, default=ROOT/'validation_logs/offline_flow')
    args = parser.parse_args()
    if args.suite:
        directory = ROOT/'tests' if args.suite == 'integration' else ROOT/'modules/cleaning/tests'
        with patch('socket.socket.connect', side_effect=AssertionError('Network disabled in offline tests')), \
             patch('socket.create_connection', side_effect=AssertionError('Network disabled in offline tests')):
            suite = unittest.defaultTestLoader.discover(str(directory))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        print('OFFLINE_RESULT=' + json.dumps({'run':result.testsRun,'failed':len(result.failures),
                                             'errors':len(result.errors),'skipped':len(result.skipped)}))
        return 0 if result.wasSuccessful() else 1

    import pipeline
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in ('integration', 'cleaning'):
        with (out/(name+'.log')).open('w') as log:
            process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--suite', name],
                                     stdout=log, stderr=subprocess.STDOUT)
        text = (out/(name+'.log')).read_text()
        lines = [line for line in text.splitlines() if line.startswith('OFFLINE_RESULT=')]
        results[name] = {'exit_code':process.returncode,
                         **(json.loads(lines[-1].split('=',1)[1]) if lines else {'missing_test_summary':True})}
    graph = {}
    for name in ('dictionary', 'important_rule', 'important_llm'):
        cfg_path = ROOT/'configs'/('pipeline.important_llm.example.json' if name=='important_llm' else 'pipeline.example.json')
        config, values = pipeline.load_config(cfg_path)
        if name=='important_rule':
            config['track']='important';config['important_extraction']='rule'
        stages=pipeline.plan(config,values)
        generated=set(); unresolved=[]; blocked=[]
        for stage in stages:
            for task in stage['tasks']:
                for path in task['requires']:
                    if path.startswith(values['run']+'/') and path not in generated:
                        unresolved.append({'task':task['name'],'path':path})
                if task['blocked']:
                    blocked.append({'task':task['name'],'reason':task['blocked']})
                generated.update(task['produces'])
        graph[name]={'missing_internal_producers':unresolved,'unimplemented_hooks':blocked,
                     'task_count':sum(len(s['tasks']) for s in stages)}
    baseline=json.loads((ROOT/'MODULE_MANIFEST.json').read_text())['important_llm']
    core_changes=[name for name,h in baseline['sha256'].items()
                  if hashlib.sha256((ROOT/'modules/important_llm'/name).read_bytes()).hexdigest()!=h]
    code_paths=[ROOT/'pipeline.py',*list((ROOT/'adapters').glob('*.py')),
                ROOT/'modules/cleaning/scripts/rule_clean.py',ROOT/'modules/cleaning/scripts/model_clean.py']
    report={'scope':'offline_contracts_and_local_processing_only','model_api_calls':0,
            'model_and_mount_decisions':'fixtures, not actual model judgments',
            'tests':results,'plans':graph,'important_llm_core_changes':core_changes,
            'code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths},
            'all_six_stage_interfaces_connected':all(not p['missing_internal_producers'] and not p['unimplemented_hooks'] for p in graph.values()),
            'production_run_validated':False,
            'remaining_blocker':None,
            'runtime_prerequisites':'Provide real source inputs, installed dependencies and deployed model endpoints before API tests',
            'not_validated':['real service capability and latency','semantic accuracy','full-corpus screening/extraction',
                             'actual taxonomy mounting','production throughput','dependency installation on another machine']}
    passed=all(r['exit_code']==0 for r in results.values()) and not core_changes and all(
        not r['missing_internal_producers'] for r in graph.values())
    report['offline_checks_passed']=passed
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if passed else 1


if __name__=='__main__':
    raise SystemExit(main())
