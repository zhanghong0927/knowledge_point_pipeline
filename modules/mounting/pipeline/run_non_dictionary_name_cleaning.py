"""Resume-safe stage-one title cleaning of exact-path-deduplicated non-dictionary rows."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/home/wangqiyuan/work/full_exact_path_dedup_20260923')
SCRIPT = Path('/home/wangqiyuan/work/name_clean_benchmark_20260922/package/01_name_title_quality.py')
ENDPOINT = 'http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job.v5000-prod.nhss.zhejianglab.com/v1/chat/completions'
MODEL = '/mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B'
WORKERS = 1024
BATCH_SIZE = 10


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def validate_counts(report, expected):
    if report.get('records') != expected or report.get('completed') != expected:
        raise ValueError(f'coverage mismatch: expected={expected}, records={report.get("records")}, completed={report.get("completed")}')
    if sum(report.get('counts', {}).values()) != expected:
        raise ValueError('decision counts mismatch')


def load_cleaner():
    spec = importlib.util.spec_from_file_location('name_cleaner', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_rows(source, output, expected, cleaner):
    input_keys = {cleaner.record_key(row) for row in cleaner.build_records(source, 0, 0, 'auto', 0)}
    if len(input_keys) != expected:
        raise ValueError('input record keys are not unique')
    rows = [json.loads(line) for line in output.open(encoding='utf-8') if line.strip()]
    output_keys = [cleaner.record_key(row) for row in rows]
    if len(output_keys) != expected or set(output_keys) != input_keys:
        raise ValueError('output record key coverage mismatch')


def main():
    output_root = ROOT / 'name_cleaning'
    output_root.mkdir(exist_ok=True)
    guard = (output_root / 'runner.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    script_sha = digest(SCRIPT)
    inventory = json.loads((ROOT / 'overall_summary.json').read_text(encoding='utf-8'))
    cleaner = load_cleaner()
    summaries = []

    def status(stage, **kwargs):
        write_json(output_root / 'status.json', dict(stage=stage, pid=os.getpid(), updated=time.time(),
                                                      endpoint=ENDPOINT, workers=WORKERS, **kwargs))

    status('running', finished_subjects=0)
    for item in inventory:
        if 'dedup_total' not in item:
            summaries.append(dict(subject=item['subject'], status='missing_full'))
            write_json(output_root / 'summary.json', summaries)
            continue
        subject = item['subject']
        source = ROOT / subject / 'without_dictionary.jsonl'
        destination = output_root / subject
        destination.mkdir(exist_ok=True)
        config = dict(source=str(source), source_sha256=digest(source), script_sha256=script_sha,
                      expected_records=item['without_dictionary'], endpoint=ENDPOINT, model=MODEL,
                      workers=WORKERS, batch_size=BATCH_SIZE, context_chars=0)
        config_path = destination / 'run_config.json'
        if config_path.exists() and json.loads(config_path.read_text(encoding='utf-8')) != config:
            raise ValueError('resume config changed: ' + subject)
        if not config_path.exists():
            write_json(config_path, config)
        status('running', subject=subject, finished_subjects=len(summaries))
        try:
            verified_path = destination / 'verified.json'
            if verified_path.exists():
                verified = json.loads(verified_path.read_text(encoding='utf-8'))
                if verified.get('source_sha256') != config['source_sha256']:
                    raise ValueError('verified source hash changed')
                summaries.append(verified)
                continue
            command = [sys.executable, str(SCRIPT), '--input', str(source), '--out-dir', str(destination),
                       '--api-url', ENDPOINT, '--model', MODEL, '--no-auth', '--workers', str(WORKERS),
                       '--batch-size', str(BATCH_SIZE), '--context-chars', '0', '--resume']
            write_json(destination / 'command.json', command)
            with (destination / 'run.log').open('a', encoding='utf-8') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
            report = json.loads((destination / 'llm_name_title_format_report.json').read_text(encoding='utf-8'))
            validate_counts(report, item['without_dictionary'])
            verify_rows(source, destination / 'llm_name_title_format_judgments.jsonl',
                        item['without_dictionary'], cleaner)
            if digest(source) != config['source_sha256']:
                raise ValueError('source hash changed during run')
            verified = dict(subject=subject, status='verified', records=item['without_dictionary'],
                            source_sha256=config['source_sha256'], counts=report['counts'],
                            pending=report.get('pending', 0), api_status_counts=report.get('api_status_counts', {}))
            write_json(verified_path, verified)
            summaries.append(verified)
        except Exception as exc:
            summaries.append(dict(subject=subject, status='error', error=str(exc)))
        finally:
            write_json(output_root / 'summary.json', summaries)
            status('running', finished_subjects=len(summaries))
    errors = [s for s in summaries if s['status'] == 'error']
    status('completed_with_issues' if errors else 'completed', finished_subjects=len(summaries), errors=len(errors))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
