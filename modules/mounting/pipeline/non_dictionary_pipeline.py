"""Freeze deduplicated non-dictionary inputs and run the existing QA pipeline."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
ENDPOINT = 'http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com'
OLD_ROOT = Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922')
DEDUP_ROOT = Path('/home/wangqiyuan/work/full_exact_path_dedup_20260923')
NEW_ROOT = Path('/home/wangqiyuan/work/non_dictionary_12subjects_run_20260923')


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def choose_cards(old_subject, tree):
    incomplete = []
    for name in ('boundaries_v6_alt', 'boundaries_v5', 'boundaries'):
        cards = old_subject / name
        summary_file = cards / 'summary.json'
        if not summary_file.exists():
            continue
        summary = json.loads(summary_file.read_text(encoding='utf-8'))
        if summary.get('total_tree_nodes') != summary.get('accepted_candidate_cards') or not summary.get('source_unchanged'):
            incomplete.append(name)
            continue
        required = ('source_tree.json', 'knowledge_tree.json', 'semantic_cards.jsonl')
        if any(not (cards / filename).is_file() for filename in required):
            incomplete.append(name)
            continue
        if digest(cards / 'source_tree.json') != digest(tree):
            raise ValueError(f'tree mismatch: {old_subject}')
        return cards
    raise ValueError(f'no complete boundary cards: {old_subject}; incomplete={incomplete}')


def validate_entry(entry, source):
    subject = entry['subject']
    if not source.is_file():
        raise ValueError(f'non-dictionary input missing: {subject}: {source}')
    for label in ('full_source', 'dictionary_source'):
        original = Path(entry[label]['path'])
        if not original.is_file() or digest(original) != entry[label]['sha256']:
            raise ValueError(f'{label.removesuffix("_source")} source changed: {subject}: {original}')
    ids = set()
    count = 0
    with source.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            record_id = row.get('id')
            if not isinstance(record_id, str) or record_id in ids:
                raise ValueError(f'invalid or duplicate ID: {subject}: {record_id}')
            ids.add(record_id)
            count += 1
    if count != entry['without_dictionary']:
        raise ValueError(f'non-dictionary count mismatch: {subject}: {count} != {entry["without_dictionary"]}')
    return {'records': count, 'sha256': digest(source)}


def prepare_one(entry, source, old_subject, destination, validated=None):
    validated = validated or validate_entry(entry, source)
    tree = old_subject / 'taxonomy.json'
    old_manifest = json.loads((old_subject / 'manifest.json').read_text(encoding='utf-8')) if (old_subject / 'manifest.json').is_file() else None
    if old_manifest and digest(tree) != old_manifest['tree_sha256']:
        raise ValueError(f'old taxonomy snapshot changed: {entry["subject"]}')
    if old_manifest and digest(Path(old_manifest['tree'])) != old_manifest['tree_sha256']:
        raise ValueError(f'current taxonomy changed: {entry["subject"]}')
    cards = choose_cards(old_subject, tree)
    manifest = dict(subject=entry['subject'], source=str(source), tree=str(Path(old_manifest['tree']) if old_manifest else tree),
                    source_sha256=validated['sha256'], tree_sha256=digest(tree), records=validated['records'],
                    nodes=json.loads((cards / 'summary.json').read_text(encoding='utf-8'))['total_tree_nodes'],
                    status='queued', source_kind='exact_path_deduplicated_non_dictionary',
                    boundary_reused_from=str(cards))
    if destination.exists():
        if json.loads((destination / 'manifest.json').read_text(encoding='utf-8')) != manifest:
            raise ValueError(f'existing manifest differs: {destination}')
        if digest(destination / 'source.snapshot') != manifest['source_sha256']:
            raise ValueError(f'existing snapshot differs: {destination}')
        return manifest
    destination.mkdir(parents=True)
    shutil.copy2(source, destination / 'source.snapshot')
    shutil.copy2(tree, destination / 'taxonomy.json')
    target = destination / 'boundaries'
    target.mkdir()
    for filename in ('source_tree.json', 'knowledge_tree.json', 'semantic_cards.jsonl', 'summary.json'):
        shutil.copy2(cards / filename, target / filename)
    atomic_json(destination / 'manifest.json', manifest)
    if digest(destination / 'source.snapshot') != manifest['source_sha256']:
        raise ValueError(f'copied snapshot differs: {destination}')
    return manifest


def prepare(summary_path=DEDUP_ROOT / 'overall_summary.json', dedup_root=DEDUP_ROOT,
            old_root=OLD_ROOT, new_root=NEW_ROOT):
    entries = json.loads(summary_path.read_text(encoding='utf-8'))
    checked = []
    missing = []
    # Validate every input and boundary before writing any new run data.
    for entry in entries:
        subject = entry['subject']
        if entry.get('status') == 'missing_full':
            missing.append(subject)
            continue
        source = dedup_root / subject / 'without_dictionary.jsonl'
        validated = validate_entry(entry, source)
        old_subject = old_root / subject
        tree = old_subject / 'taxonomy.json'
        old_manifest = json.loads((old_subject / 'manifest.json').read_text(encoding='utf-8'))
        if digest(tree) != old_manifest['tree_sha256'] or digest(Path(old_manifest['tree'])) != old_manifest['tree_sha256']:
            raise ValueError(f'taxonomy changed: {subject}')
        choose_cards(old_subject, tree)
        checked.append((entry, source, old_subject, validated))
    new_root.mkdir(parents=True, exist_ok=True)
    inventory = [prepare_one(entry, source, old_subject, new_root / entry['subject'], validated)
                 for entry, source, old_subject, validated in checked]
    atomic_json(new_root / 'inventory.json', inventory)
    atomic_json(new_root / 'input_summary.json', {
        'subjects': len(inventory), 'records': sum(item['records'] for item in inventory),
        'missing_full': missing, 'endpoint': ENDPOINT, 'source_summary': str(summary_path),
        'boundary_source': str(old_root), 'deliveries_mutated': False,
    })
    return inventory


def old_dictionary_active(old_root):
    for subject in ('sociology', 'philosophy'):
        status_file = old_root / subject / 'status.json'
        if not status_file.exists():
            continue
        status = json.loads(status_file.read_text(encoding='utf-8'))
        if status.get('stage') in ('completed', 'blocked') or (old_root / subject / 'done.json').exists():
            continue
        pid = status.get('pid')
        if pid:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                pass
    return False


def latest_progress_time(destination, started):
    paths = (destination / 'status.json', destination / 'audit' / 'progress.json',
             destination / 'review' / 'progress.json', destination / 'remount' / 'mount.log',
             destination / 'remount' / 'new.jsonl')
    return max([started] + [path.stat().st_mtime for path in paths if path.exists()])


def run_subject(subject, root, workers, timeout_seconds):
    destination = root / subject
    previous = destination / 'done.json'
    if previous.exists():
        return {'subject': subject, 'state': 'already_done', 'returncode': 0}
    # A pilot may already own this subject. Wait only in this job's worker;
    # the rest of the subject queue must remain free to progress.
    status_file = destination / 'status.json'
    if status_file.exists():
        status = json.loads(status_file.read_text(encoding='utf-8'))
        prior_pid = status.get('pid')
        pilot_deadline = time.time() + min(timeout_seconds, 2 * 3600)
        while prior_pid and not previous.exists() and time.time() < pilot_deadline:
            try:
                os.kill(prior_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(30)
        if previous.exists():
            return {'subject': subject, 'state': 'already_done', 'returncode': 0}
        if prior_pid and time.time() >= pilot_deadline:
            result = {'subject': subject, 'state': 'existing_worker_timeout', 'prior_pid': prior_pid}
            atomic_json(destination / 'queue_result.json', result)
            return result
    command = [sys.executable, '-u', str(HERE / 'runner.py'), 'subject', '--out', str(root), '--subject', subject]
    environment = dict(os.environ, KNOWLEDGE_ENDPOINT_OVERRIDE=ENDPOINT,
                       KNOWLEDGE_REVIEW_WORKERS=str(workers), KNOWLEDGE_MOUNT_WORKERS=str(workers))
    started = time.time()
    with (destination / 'independent_run.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        state = None
        while state is None:
            try:
                returncode = process.wait(timeout=60)
                state = 'completed' if returncode == 0 and previous.exists() else 'error'
            except subprocess.TimeoutExpired:
                elapsed = time.time() - started
                idle = time.time() - latest_progress_time(destination, started)
                if elapsed < timeout_seconds and idle < 45 * 60:
                    continue
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                returncode = process.returncode
                state = 'idle_timeout' if idle >= 45 * 60 else 'timeout'
    result = {'subject': subject, 'state': state, 'returncode': returncode,
              'seconds': round(time.time() - started, 1), 'workers': workers, 'endpoint': ENDPOINT}
    atomic_json(destination / 'queue_result.json', result)
    return result


def run_queue(root=NEW_ROOT, old_root=OLD_ROOT, max_parallel=2, workers=512,
              timeout_seconds=12 * 3600, wait_for_dictionary_seconds=4 * 3600):
    inventory = json.loads((root / 'inventory.json').read_text(encoding='utf-8'))
    deadline = time.time() + wait_for_dictionary_seconds
    while old_dictionary_active(old_root) and time.time() < deadline:
        atomic_json(root / 'queue_status.json', {'stage': 'waiting_for_dictionary_api_capacity',
                    'updated': time.time(), 'endpoint': ENDPOINT, 'subjects': len(inventory)})
        time.sleep(120)
    ordered = sorted(inventory, key=lambda row: row['records'])
    results = []
    atomic_json(root / 'queue_status.json', {'stage': 'running', 'updated': time.time(),
                'subjects': len(inventory), 'max_parallel': max_parallel, 'workers_per_subject': workers,
                'endpoint': ENDPOINT, 'finished': 0})
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        jobs = {pool.submit(run_subject, item['subject'], root, workers, timeout_seconds): item['subject']
                for item in ordered}
        for future in as_completed(jobs):
            subject = jobs[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {'subject': subject, 'state': 'orchestrator_error', 'error': repr(exc)}
            results.append(result)
            atomic_json(root / 'queue_status.json', {'stage': 'running', 'updated': time.time(),
                        'subjects': len(inventory), 'max_parallel': max_parallel,
                        'workers_per_subject': workers, 'endpoint': ENDPOINT,
                        'finished': len(results), 'results': results})
    atomic_json(root / 'queue_status.json', {'stage': 'finished', 'updated': time.time(),
                'subjects': len(inventory), 'max_parallel': max_parallel,
                'workers_per_subject': workers, 'endpoint': ENDPOINT,
                'finished': len(results), 'results': results,
                'states': dict(Counter(row['state'] for row in results))})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('--root', type=Path, default=NEW_ROOT)
    parser.add_argument('--max-parallel', type=int, default=2)
    parser.add_argument('--workers', type=int, default=512)
    arguments = parser.parse_args()
    if arguments.action == 'prepare':
        result = prepare(new_root=arguments.root)
        print(json.dumps({'prepared_subjects': len(result), 'records': sum(item['records'] for item in result)},
                         ensure_ascii=False), flush=True)
    else:
        if not 1 <= arguments.max_parallel <= 8 or not 1 <= arguments.workers <= 1024:
            raise ValueError('unsafe queue concurrency')
        run_queue(root=arguments.root, max_parallel=arguments.max_parallel, workers=arguments.workers)


if __name__ == '__main__':
    main()
