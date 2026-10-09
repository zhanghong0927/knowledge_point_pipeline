"""Repair failed boundary groups by regenerating each target with full sibling context.

Never invent cards or mark a group complete unless every card passes the original
schema validator and the model review. The original failed checkpoint is retained.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

import generate_semantic_boundaries as boundary


def split_payload(payload, target):
    return dict(payload, targets=[target])


def recover_single_wrong_code(saved, base, model):
    payload = saved['payload']
    if len(payload['targets']) != 1:
        return None
    target_code = payload['targets'][0]['code']
    for round_result in saved['output'].get('rounds', []):
        for attempt in round_result.get('generation', {}).get('attempts', []):
            if attempt.get('error') != 'foreign/duplicate card code':
                continue
            choice = ((attempt.get('raw') or {}).get('choices') or [{}])[0]
            if choice.get('finish_reason') != 'stop':
                continue
            content = (choice.get('message') or {}).get('content') or ''
            try:
                obj = json.loads(content[content.index('{'):content.rindex('}') + 1])
                if len(obj['cards']) != 1 or not isinstance(obj['cards'][0].get('node_code'), str):
                    continue
                original_code = obj['cards'][0]['node_code']
                obj['cards'][0]['node_code'] = target_code
                cards = boundary.validate_cards(obj, payload['targets'], payload['sibling_context'])
            except (ValueError, KeyError, TypeError):
                continue
            review = boundary.call(base, model, boundary.REVIEW_PROMPT, dict(payload, cards=cards),
                                   lambda value: boundary.validate_review(value, [target_code]))
            if review['result'] and review['result']['verdict'] == 'pass':
                return dict(status='accepted_candidate', cards=cards, rounds=[],
                            automatic_recovery='single_target_code_correction_reviewed',
                            original_code=original_code, independent_review=review)
    return None


def recover_group(saved, base, model, max_bytes):
    payload = saved['payload']
    if saved['output']['status'] != 'technical_failure':
        return None
    corrected = recover_single_wrong_code(saved, base, model)
    if corrected is not None:
        return dict(recovered=True, output=corrected)
    parts = []
    for target in payload['targets']:
        part = boundary.process_group(base, model, split_payload(payload, target), max_bytes)
        part = boundary.advisory_result(part)
        parts.append(part)
    if not all(p['status'] in ('accepted_candidate', 'advisory_candidate') for p in parts):
        return dict(recovered=False, parts=parts)
    cards = [c for p in parts for c in p['cards']]
    # Revalidate the exact original group; every expected node must be present.
    boundary.validate_cards({'cards': cards}, payload['targets'], payload['sibling_context'])
    return dict(recovered=True, output=dict(
        status='advisory_candidate' if any(p['status'] == 'advisory_candidate' for p in parts) else 'accepted_candidate',
        cards=cards, rounds=[], automatic_recovery='single_target_full_sibling_context',
        recovery_parts=parts,
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--workers', type=int, default=64)
    args = parser.parse_args()
    run = args.run
    config = json.loads((run / 'config.json').read_text())
    failed = []
    for path in (run / 'groups').glob('*.json'):
        saved = json.loads(path.read_text())
        if saved['output']['status'] == 'technical_failure':
            failed.append((path, saved))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(recover_group, saved, args.base, config['model'], config['max_context_bytes']): (path, saved)
                for path, saved in failed}
        for future in as_completed(jobs):
            path, saved = jobs[future]
            result = future.result()
            record = dict(file=path.name, targets=[x['code'] for x in saved['payload']['targets']], recovered=result['recovered'])
            if result['recovered']:
                backup = run / 'failed_group_backups' / path.name
                backup.parent.mkdir(exist_ok=True)
                if not backup.exists():
                    boundary.atomic_json(backup, saved)
                boundary.atomic_json(path, dict(payload=saved['payload'], output=result['output']))
                record['status'] = result['output']['status']
            else:
                record['failed_parts'] = [x['status'] for x in result['parts']]
            results.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    report = dict(total=len(failed), recovered=sum(x['recovered'] for x in results), unresolved=sum(not x['recovered'] for x in results), groups=results)
    boundary.atomic_json(run / 'automatic_recovery_report.json', report)
    print(json.dumps({k: report[k] for k in ('total', 'recovered', 'unresolved')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
