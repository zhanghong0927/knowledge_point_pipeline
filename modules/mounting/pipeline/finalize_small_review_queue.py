"""Finalize the 21 small retry items; unresolved technical items default to DROP."""

import json
from pathlib import Path
import time


ROOT = Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922/closeout_queue_20260923/small_review_jobs')
EXPECTED_JOBS = 12
EXPECTED_ITEMS = 21


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def write_jsonl(path, rows):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    temporary.replace(path)


def main():
    # The independently bounded worker pool may need up to 40 minutes; do not
    # let this finalizer wait forever or mistake a partial queue for completion.
    deadline = time.time() + 3600
    while time.time() < deadline:
        progress = ROOT / 'queue_status.json'
        if progress.exists():
            status = json.loads(progress.read_text(encoding='utf-8'))
            if status.get('completed_jobs') == EXPECTED_JOBS and status.get('total_jobs') == EXPECTED_JOBS:
                break
        time.sleep(30)
    else:
        raise TimeoutError('Small review queue did not finish within one hour; no default DROP applied')

    jobs = sorted(path for path in ROOT.iterdir() if path.is_dir() and (path / 'pending.jsonl').exists())
    if len(jobs) != EXPECTED_JOBS:
        raise ValueError(f'Expected {EXPECTED_JOBS} jobs, got {len(jobs)}')
    output = []
    for job in jobs:
        stage = 'audit' if job.name.endswith('_audit') else 'review'
        subject = job.name.removesuffix('_' + stage)
        status = json.loads((job / 'status.json').read_text(encoding='utf-8'))
        pending = read_jsonl(job / 'pending.jsonl')
        result_rows = read_jsonl(job / 'run' / 'final.jsonl')
        results = {row['item']['request_id']: row['review'] for row in result_rows}
        if len(results) != len(result_rows):
            raise ValueError(f'Duplicate retry results: {job.name}')
        if not set(results).issubset({row['request_id'] for row in pending}):
            raise ValueError(f'Unexpected retry ID: {job.name}')
        for item in pending:
            review = results.get(item['request_id'], {})
            judgment = review.get('judgment')
            unresolved = status.get('state') != 'completed' or not judgment or judgment == 'technical_failure'
            output.append({
                'subject': subject,
                'stage': stage,
                'request_id': item['request_id'],
                'id': item.get('id'),
                'name': item.get('name'),
                'knowledge_point': item.get('knowledge_point'),
                'decision': 'DROP' if unresolved else judgment,
                'decision_basis': 'user_default_after_retry_failure' if unresolved else 'retry_model_result',
                'retry_reason': review.get('reason', ''),
                'job_state': status.get('state'),
                'original_item': item,
            })
    if len(output) != EXPECTED_ITEMS or len({(row['subject'], row['stage'], row['request_id']) for row in output}) != EXPECTED_ITEMS:
        raise ValueError('Result coverage mismatch; no output written')
    write_jsonl(ROOT / 'final_decisions.jsonl', output)
    report = {
        'total': len(output),
        'user_default_drop': sum(row['decision_basis'] == 'user_default_after_retry_failure' for row in output),
        'retry_resolved': sum(row['decision_basis'] == 'retry_model_result' for row in output),
        'source_delivery_mutated': False,
        'note': 'DROP is limited to this 21-item retry set; source delivery files were not deleted.',
    }
    temporary = ROOT / 'final_decisions.summary.json.tmp'
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(ROOT / 'final_decisions.summary.json')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
