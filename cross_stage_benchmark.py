#!/usr/bin/env python3
"""Bounded book-level 0611 benchmark; verified admission never waits for a quota."""

import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

from batch_pipeline import Gateway, bind_model_routes, bounded_map, bounded_pipeline, execute_job
from benchmark_support import (MemoryAdmission, Recorder, ResourceMonitor, collect_approved,
    configure, now, read, save, sha, validate_sources, verify_original_rows)

ROOT = Path(__file__).resolve().parent


def compact_job_id(track, phase, identifier):
    return track + '_' + phase + '_' + hashlib.sha256(identifier.encode()).hexdigest()[:24]


def run_book_flow(specs, prepare, process, mount, prepare_workers=4, process_workers=8,
                  mount_workers=4, max_pending=32, selection=None):
    """Mount a subject when its own books finish, not when all other subjects finish."""
    if type(mount_workers) is not int or mount_workers < 1:
        raise ValueError('mount_workers must be a positive integer')
    if len({item['key'] for item in specs}) != len(specs):
        raise ValueError('Duplicate book keys')
    subjects = {item['key']: item['subject'] for item in specs}
    remaining = Counter(subjects.values())
    processed, mounted = [], []
    jobs_by_subject = defaultdict(list)
    ready, pending = deque(), set()
    seen = set()
    closed_keys = None
    finalized_subjects = set()
    with ThreadPoolExecutor(max_workers=mount_workers) as pool:
        def drain(block=False):
            nonlocal pending
            if pending:
                done, pending = wait(pending, timeout=None if block else 0,
                                     return_when=FIRST_COMPLETED)
                for future in done:
                    mounted.append(future.result())
            while ready and len(pending) < mount_workers:
                subject, jobs = ready.popleft()
                pending.add(pool.submit(mount, subject, jobs))

        for result in bounded_pipeline(specs, prepare, process, prepare_workers,
                                       process_workers, max_pending):
            key, subject = result['key'], result['subject']
            if key in seen or subjects.get(key) != subject:
                raise ValueError('Unexpected or duplicate book completion')
            seen.add(key)
            processed.append(result)
            if result.get('production_job') and result['result']['status'] == 'completed':
                jobs_by_subject[subject].append(result['production_job'])
            remaining[subject] -= 1
            ready_subjects = [subject] if remaining[subject] == 0 else []
            if selection:
                closed, keys = selection()
                if closed:
                    keys = set(keys)
                    if not keys.issubset(subjects) or (closed_keys is not None and keys != closed_keys):
                        raise ValueError('Closed production selection changed')
                    closed_keys = keys
                    expected = Counter(subjects[key] for key in keys)
                    finished = Counter(subjects[key] for key in keys & seen)
                    ready_subjects = [s for s, count in expected.items() if finished[s] == count]
                else:
                    ready_subjects = []
            for finished_subject in ready_subjects:
                if finished_subject in finalized_subjects:
                    continue
                finalized_subjects.add(finished_subject)
                if not jobs_by_subject[finished_subject]:
                    continue
                while len(ready) >= max_pending:
                    drain(True)
                ready.append((finished_subject, jobs_by_subject.pop(finished_subject)))
            drain()
        while ready or pending:
            drain(bool(pending))
    return processed, mounted


def reuse_dictionary_front(root, previous, candidates):
    """Recompute admission from frozen evidence without changing old artifacts or calling a model."""
    from adapters.approve_dictionary_books import convert
    previous = previous.resolve(strict=True)
    status = read(previous / 'STATUS.json')
    if status['status'] != 'failed' or not status.get('error', '').startswith('ValueError: Only '):
        raise ValueError('Only the ended cohort-shortfall run can be continued here')
    if (previous / 'gateway/READY.json').exists() or (previous / 'dictionary/production').exists():
        raise ValueError('Previous run is active or has already started production')
    if sha(candidates) != sha(previous / 'inputs/dictionary_candidates_verified.json'):
        raise ValueError('Continuation candidate input differs from the original front run')
    scripts = ROOT / 'modules/dictionary/classification/scripts'
    restored, proofs = [], []
    for config_path in sorted((previous / 'dictionary/front').glob('*/*/pipeline.json')):
        home = config_path.parent
        result = read(home / 'JOB_RESULT.json')
        if result['status'] != 'completed':
            raise ValueError('Cannot reuse a failed front job')
        provenance = read(home / 'INPUT_PROVENANCE.json')
        if sha(config_path) != provenance['configuration_sha256']:
            raise ValueError('Old front configuration changed')
        classified = home / 'run/02_structure/classification'
        manifest = read(classified / 'manifest.json')
        if manifest['code_sha256'] != {p.name: sha(p) for p in scripts.glob('*.py')}:
            raise ValueError('Classification code differs from the frozen evidence')
        cfg = read(config_path)
        if read(Path(cfg['paths']['classification_model_config'])) != manifest['config']:
            raise ValueError('Frozen model configuration changed')
        base = Path(manifest['base'])
        for ref in manifest['targets']:
            packet = base / 'prepared' / (ref + '.json')
            if packet.exists():
                validate_sources(read(packet))
        books_path = home / 'run/02_structure/book_manifest/books.json'
        books, _, _, report = convert(books_path, classified, ['entry_prose'], allow_empty=True)
        approval = home / 'run/02_structure/dictionary_approval'
        if report['technical_failed'] or books != read(approval / 'approved_books.json'):
            raise ValueError('Recomputed dictionary admission differs from frozen approval')
        proof = approval / 'report.json'
        if read(proof)['approved'] != len(books):
            raise ValueError('Approval count changed')
        parent = {'job_id': result['job_id'], 'home': str(home),
                  'subject': cfg['subject']['slug'], 'config': str(config_path)}
        restored.extend({'book': book, 'subject': parent['subject'], 'parent_job': parent,
                         'approval_proof': str(proof), 'approval_sha256': sha(proof)} for book in books)
        proofs.append({'job_id': result['job_id'], 'configuration_sha256': sha(config_path),
                       'approval_sha256': sha(proof), 'approved': len(books),
                       'source_and_sample_evidence_revalidated': True})
    if len({r['book']['identifier'] for r in restored}) != len(restored):
        raise ValueError('Duplicate reused book IDs')
    if len(restored) != read(previous / 'dictionary/FRONT_SUMMARY.json')['admitted']:
        raise ValueError('Reused cohort is incomplete')
    save(root / 'FRONT_REUSE_VERIFICATION.json', {'source_run': str(previous),
         'actual_approved_books': len(restored), 'jobs': proofs, 'model_calls': 0,
         'old_outputs_modified': False, 'quality_thresholds_relaxed': False})
    return restored


def revalidate_admission(admission):
    if sha(admission['approval_proof']) != admission['approval_sha256']:
        raise ValueError('Approval evidence changed before extraction')
    home = Path(admission['parent_job']['home'])
    manifest = read(home / 'run/02_structure/classification/manifest.json')
    ref = admission['book']['identifier']
    base = Path(manifest['base'])
    packet = base / 'prepared' / (ref + '.json')
    if 'input_sha256' in manifest:
        if sha(packet) != manifest['input_sha256'][ref]:
            raise ValueError('Approved preparation evidence changed before extraction')
        validate_sources(read(packet))
    else:
        if sha(base / 'manifest.json') != manifest['base_manifest_sha256']:
            raise ValueError('Important preparation manifest changed before extraction')
        if sha(packet) != read(base / 'manifest.json')['prepared_hashes'][ref]:
            raise ValueError('Important prepared evidence changed before extraction')
        prep = read(packet)
        for kind in ('md', 'pdf'):
            if prep.get(kind + '_path') and sha(prep[kind + '_path']) != prep[kind + '_sha256']:
                raise ValueError('Important source changed before extraction: ' + kind)


def collect_413_recovery(previous, admissions):
    summary = read(previous / 'dictionary/SUMMARY.json')
    identifiers = set()
    for failure in summary['failed_book_jobs']:
        job = failure.get('production_job')
        if not job:
            continue
        path = Path(job['home']) / 'run/03_extraction/raw/SUMMARY.json'
        if path.is_file() and any('HTTP Error 413' in str(book.get('error', '')) for book in read(path)['books']):
            identifiers.add(failure['key'])
    selected = [item for item in admissions if item['book']['identifier'] in identifiers]
    if {item['book']['identifier'] for item in selected} != identifiers:
        raise ValueError('Recovery identifiers do not match verified original admissions')
    return selected


def merge_recovered_results(root, previous, recorder):
    """Combine audited pass-only benchmark exports, not formal delivery directories."""
    groups = defaultdict(list)
    parents = {}
    for source in (previous, root):
        summary = read(source / 'dictionary/SUMMARY.json')
        for job in summary['mounting_jobs']:
            path = Path(job['home']) / 'run/06_dedup/retained.jsonl'
            marker = path.parent / 'VERIFICATION.json'
            if not path.is_file() or not marker.is_file():
                continue
            subject = job['subject']
            groups[subject].append(path)
            parents[subject] = job
    results = []
    for subject, inputs in groups.items():
        home = root / 'combined_dictionary' / subject
        config = read(parents[subject]['config'])
        config['paths']['run'] = str(home / 'run')
        config['dedup_inputs'] = [str(p) for p in inputs]
        save(home / 'pipeline.json', config)
        save(home / 'INPUTS.json', {'inputs': [{'path': str(p), 'sha256': sha(p)} for p in inputs],
             'old_outputs_modified': False, 'quality_approved': False})
        result = execute_job({'job_id': 'recovery_merge_' + subject, 'home': str(home),
            'config': str(home / 'pipeline.json'), 'stages': ['06']}, recorder.event)
        results.append({'subject': subject, 'result': result, 'retained': str(home / 'run/06_dedup/retained.jsonl')})
    save(root / 'COMBINED_DICTIONARY_RESULTS.json', {'subjects': results, 'source_run': str(previous),
         'not_formal_delivery': True, 'quality_approved': False})
    if any(item['result']['status'] != 'completed' for item in results):
        raise RuntimeError('Recovered dictionary merge failed; previous results preserved')


def live_service_check(gateway, service):
    with urlopen(gateway.api_url + '/v1/models', timeout=20) as response:
        models = json.load(response)['data']
    model = next((m for m in models if m['id'] == service['model']), None)
    if model is None:
        raise ValueError('Configured model is not currently served')
    context = model.get('max_model_len')
    if context is not None and context < service['configured_context']:
        raise ValueError('Current model context is smaller than the configuration')
    messages = [{'role': 'user', 'content': 'Reply OK only.'}]
    payloads = [('tokenize', {'model': service['model'], 'messages': messages, 'add_generation_prompt': True}),
                ('v1/chat/completions', {'model': service['model'], 'messages': messages,
                 'max_tokens': 8, 'chat_template_kwargs': {'enable_thinking': False}})]
    answers = {}
    for route, body in payloads:
        request = Request(gateway.api_url + '/' + route,
                          data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=60) as response:
            answers[route] = json.load(response)
    if answers['tokenize']['count'] + 508 > service['configured_context']:
        raise ValueError('Tokenize context check failed')
    if answers['v1/chat/completions']['choices'][0]['finish_reason'] != 'stop':
        raise ValueError('Live chat completion probe did not finish')
    return {'at': now(), 'model': model, 'tokenize_count': answers['tokenize']['count'],
            'inference_probe': answers['v1/chat/completions']['choices'][0]['message']['content'],
            'all_probes_used_global_gateway': True}


class BoundaryWarmup:
    """Prepare independent taxonomy assets with a bounded background producer."""
    def __init__(self, root, subjects, service, gateway, memory, recorder):
        self.root, self.service, self.gateway = root, service, gateway
        self.memory, self.recorder = memory, recorder
        self.subjects = list(dict.fromkeys(subjects))
        from project_taxonomy import DEFAULT_LOCK
        self.cache = DEFAULT_LOCK.parent
        self.results = {}
        self.condition = threading.Condition()
        self.error = None
        self.thread = threading.Thread(target=self.run, name='boundary-prewarm')

    def url(self, subject):
        return self.gateway.job_url(compact_job_id('taxonomy', 'boundary', subject))

    def generate(self, subject):
        job_id = compact_job_id('taxonomy', 'boundary', subject)
        home = self.root / 'boundary_prewarm' / subject
        home.mkdir(parents=True)
        registry_path = ROOT / 'configs/taxonomy_registry.json'
        registry = read(registry_path)
        from project_taxonomy import subject as project_subject,DEFAULT_LOCK
        pin=project_subject(subject)
        tree=Path(pin['taxonomy'])
        out = home / 'asset_preparation'
        bridge = ROOT / 'adapters/mounting_bridge.py'
        common = ['--out', str(out), '--project-assets-lock', str(DEFAULT_LOCK)]
        commands = [
            [sys.executable, '-B', str(bridge), 'prepare', '--boundary-only', '--require-boundaries',
             '--subject-slug', subject, '--taxonomy', str(tree), '--registry', str(registry_path), *common],
            [sys.executable, '-B', str(bridge), 'activate-project-assets', *common],
            [sys.executable, '-B', str(bridge), 'verify-boundaries', *common],
        ]
        save(home / 'PLAN.json', {'subject': subject, 'taxonomy_sha256': sha(tree), 'commands': commands,
             'global_request_limit': 1024, 'boundary_review_mode': 'off', 'no_knowledge_records_created': True})
        start = time.monotonic()
        result = {'subject': subject, 'job_id': job_id, 'started_at': now()}
        self.recorder.event({**result, 'status': 'boundary_prewarm_running', 'at': now()})
        try:
            with self.memory.slot(), (home / 'boundary.log').open('w', encoding='utf-8') as log:
                for command in commands:
                    subprocess.run(command, cwd=ROOT, env=os.environ.copy(), stdout=log,
                                   stderr=subprocess.STDOUT, check=True)
            report = read(out / 'BOUNDARIES_VERIFIED.json')
            generated = read(out / 'BOUNDARIES_GENERATED.json')
            result.update(status='completed', verification=report,
                          cache_reused=all(g['cache_reused'] for g in generated['groups'].values()))
        except Exception as exc:
            result.update(status='failed', error=str(exc), log=str(home / 'boundary.log'))
        result.update(ended_at=now(), wall_seconds=time.monotonic()-start)
        save(home / 'RESULT.json', result)
        self.recorder.event({**result, 'at': now()})
        return result

    def run(self):
        try:
            for result in bounded_map(self.subjects, self.generate, 2):
                with self.condition:
                    self.results[result['subject']] = result
                    save(self.root / 'BOUNDARY_PREWARM_RESULTS.json', self.results)
                    self.condition.notify_all()
        except BaseException as exc:
            with self.condition:
                self.error = exc
                self.condition.notify_all()

    def wait(self, subject):
        if subject not in self.subjects:
            raise ValueError('Boundary subject was not configured')
        with self.condition:
            while subject not in self.results and self.error is None:
                self.condition.wait()
            if self.error:
                raise RuntimeError('Boundary warmup producer failed') from self.error
            return self.results[subject]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.thread.join()


def run_track(root, track, candidates, service, gateway, recorder, reused=None, boundaries=None, memory=None):
    import psutil
    start = time.monotonic()
    rows = read(candidates)
    by_id = {row['identifier']: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('Duplicate candidate IDs')
    if reused is not None:
        specs = [{'key': item['book']['identifier'], 'subject': item['subject'],
                  'row': by_id[item['book']['identifier']], 'reused': item} for item in reused]
        specs.sort(key=lambda s: Path(s['row']['md_path']).stat().st_size, reverse=True)
    else:
        specs = [{'key': row['identifier'], 'subject': row['subject_slug'], 'row': row} for row in rows]
    cap = recorder.state['capacity']
    stage_workers = 128
    memory = memory or MemoryAdmission(cap['minimum_available_memory_bytes'],
        cap['memory_reservation_per_job_bytes'], lambda: psutil.virtual_memory().available)
    lock = threading.Lock()
    state = {'candidate_books': len(rows), 'books_in_this_attempt': len(specs), 'admitted': 0,
             'selected': 0, 'reserve': 0, 'front_finished': 0, 'production_finished': 0,
             'front_not_run_quota_satisfied': 0, 'selection_decisions': 0,
             'front_model_jobs_reused': reused is not None}
    selected = []

    def progress(**increments):
        with lock:
            for key, value in increments.items():
                state[key] += value
            save(root / track / 'PROGRESS.json', {**state, 'updated_at': now()})

    def native(job):
        with memory.slot():
            return execute_job(job, recorder.event)

    def prepare(spec):
        if 'reused' in spec:
            item = spec['reused']
            if sha(item['approval_proof']) != item['approval_sha256']:
                raise ValueError('Reused approval changed before dispatch')
            prepared = {**spec, 'admitted': [item], 'front_result': {'status': 'reused_verified'},
                        'classification_technical_failed': 0}
        else:
            with lock:
                if len(selected) >= 50:
                    state['front_finished'] += 1
                    state['front_not_run_quota_satisfied'] += 1
                    save(root / track / 'PROGRESS.json', {**state, 'updated_at': now()})
                    return {**spec, 'admitted': [], 'front_result': {'status': 'not_run_quota_satisfied'},
                            'classification_technical_failed': 0}
            job_id = compact_job_id(track, 'front', spec['key'])
            home = root / track / 'front' / spec['subject'] / spec['key']
            job = configure(root, service, track, spec['subject'], [spec['row']], home,
                            job_id, gateway.job_url(job_id), workers=stage_workers)
            job['stages'] = ['01', '02']
            result = native(job)
            save(home / 'FRONT_JOB_RESULT.json', result)
            admitted = collect_approved(job, track) if result['status'] == 'completed' else []
            technical = 0
            if result['status'] == 'completed':
                if track == 'dictionary':
                    technical = read(home / 'run/02_structure/dictionary_approval/report.json')['technical_failed']
                else:
                    technical = sum(r['status'] == 'technical_failed' for r in
                        read(home / 'run/02_structure/classification/verification.json')['results'])
            prepared = {**spec, 'admitted': admitted, 'front_result': result,
                        'classification_technical_failed': technical}
        if len(prepared['admitted']) > 1:
            raise ValueError('Book-level front job admitted multiple books')
        progress(front_finished=1, admitted=len(prepared['admitted']))
        return prepared

    def process(item):
        result = {k: item[k] for k in ('key', 'subject', 'front_result', 'classification_technical_failed')}
        if not item['admitted']:
            with lock:
                state['selection_decisions'] += 1
            return {**result, 'disposition': 'not_admitted'}
        admission = item['admitted'][0]
        with lock:
            if len(selected) >= 50:
                state['selection_decisions'] += 1
                state['reserve'] += 1
                save(root / track / 'PROGRESS.json', {**state, 'updated_at': now()})
                return {**result, 'disposition': 'verified_reserve'}
            revalidate_admission(admission)
            selected.append(admission)
            state['selection_decisions'] += 1
            state['selected'] = len(selected)
            save(root / track / 'COHORT_PROGRESS.json', {'count': len(selected), 'target_maximum': 50,
                 'selected': selected, 'policy': 'first_verified_ready_books; no minimum-count gate'})
        book = admission['book']
        if track == 'dictionary':
            job_id = compact_job_id('dictionary', 'extract', item['key'])
            home = root / track / 'production' / item['subject'] / item['key']
            job = configure(root, service, track, item['subject'], [item['row']], home, job_id,
                            gateway.job_url(job_id), workers=stage_workers, selected=[book])
        else:
            job = dict(admission['parent_job'])
            home = Path(job['home'])
            save(home / 'inputs/selected_books.json', [book])
        save(home / 'FRESH_APPROVAL_BINDING.json', {'approval_proof': admission['approval_proof'],
             'approval_sha256': admission['approval_sha256'], 'selected_book': book,
             'source_row': item['row']['match_evidence'], 'reused_current_run_front': reused is not None})
        job['stages'] = ['03', '04']
        revalidate_admission(admission)
        job_result = native(job)
        progress(production_finished=1)
        return {**result, 'disposition': 'processed', 'production_job': job, 'result': job_result}

    def mount(subject, jobs):
        inputs = [str(Path(job['home']) / 'run/04_cleaning/export/records.jsonl') for job in jobs]
        if not any(Path(p).stat().st_size for p in inputs):
            return {'subject': subject, 'status': 'empty', 'reason': 'No cleaned records to mount', 'jobs': jobs}
        boundary_result = boundaries.wait(subject) if boundaries else None
        if boundary_result and boundary_result['status'] != 'completed':
            return {'subject': subject, 'status': 'boundary_failed',
                    'boundary_result': boundary_result, 'jobs': jobs}
        job_id = track + '_mount_' + subject
        home = root / track / 'mounting' / subject
        config = bind_model_routes(read(jobs[0]['config']), gateway.job_url(job_id))
        config['paths']['run'] = str(home / 'run')
        config['mounting']['inputs'] = inputs
        config['mounting']['workers'] = 256
        config['mounting']['boundaries']={'source':'project','enabled':True}
        save(home / 'pipeline.json', config)
        save(home / 'CLEAN_INPUTS.json', {'subject': subject,
             'inputs': [{'path': p, 'sha256': sha(p)} for p in inputs],
             'configuration_sha256': sha(home / 'pipeline.json'),
             'waited_for_same_subject_only': True, 'failed_books_are_not_inputs': True})
        job = {'job_id': job_id, 'subject': subject, 'home': str(home),
               'config': str(home / 'pipeline.json'), 'stages': ['05', '06']}
        return {'subject': subject, 'status': 'processed', 'job': job, 'result': native(job),
                'boundary_prewarm': boundary_result}

    recorder.update(status='running', track=track, phase='book_level_cross_stage_flow',
                    actual_admitted_books=len(reused) if reused is not None else None)
    def selection():
        with lock:
            if reused is not None:
                return True, {spec['key'] for spec in specs}
            closed = len(selected) >= 50 or state['selection_decisions'] == len(specs)
            return closed, {item['book']['identifier'] for item in selected}

    results, mounts = run_book_flow(specs, prepare, process, mount,
        prepare_workers=cap['front_workers'], process_workers=cap['production_workers'],
        mount_workers=cap['mount_workers'], max_pending=cap['ready_queue_limit'], selection=selection)
    save(root / track / 'BOOK_RESULTS.json', results)
    save(root / track / 'MOUNT_RESULTS.json', mounts)
    save(root / track / 'COHORT.json', {'count': len(selected), 'target_maximum': 50,
         'shortfall': max(0, 50 - len(selected)), 'selected': selected,
         'no_review_promoted': True, 'no_minimum_count_gate': True})
    handoffs = []
    for result in results:
        if result.get('production_job'):
            base = Path(result['production_job']['home']) / 'run/03_extraction'
            handoff = base / ('raw/HANDOFF.json' if track == 'dictionary' else 'summary.json')
            if handoff.is_file():
                handoffs.append(read(handoff))
    completed_mounts = [m for m in mounts if m.get('result', {}).get('status') == 'completed']
    failures = [r for r in results if r['front_result']['status'] == 'failed' or
                r.get('result', {}).get('status') == 'failed']
    mount_failures = [m for m in mounts if m.get('result', {}).get('status') == 'failed' or
                      m['status'] == 'boundary_failed']
    summary = {**state, 'selected_books': len(selected), 'sample_shortfall': max(0, 50-len(selected)),
        'status': 'completed_native_stages' if not failures and not mount_failures else 'partial_technical',
        'completed_cleaning_books': sum(r.get('result', {}).get('status') == 'completed' for r in results),
        'front_technical_failed_books': sum(r['classification_technical_failed'] for r in results),
        'failed_book_jobs': failures, 'failed_mounting_jobs': mount_failures,
        'partial_extraction_books': sum(h.get('partial_books', 0) for h in handoffs),
        'extraction_unresolved_items': sum(h.get('unresolved_items', 0) for h in handoffs),
        'final_retained_count': sum(sum(1 for line in (Path(m['job']['home']) /
            'run/06_dedup/retained.jsonl').open(encoding='utf-8') if line.strip()) for m in completed_mounts),
        'mounting_jobs': [m['job'] for m in mounts if 'job' in m],
        'wall_seconds': time.monotonic()-start, 'finished_at': now(),
        'semantic_quality_approved': False, 'quality_sample_pending': True}
    save(root / track / 'SUMMARY.json', summary)
    return summary


def worker(args):
    import fcntl
    import psutil
    root = args.root.resolve(strict=True)
    with (root / 'BENCHMARK.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / 'STATUS.json').exists():
            raise FileExistsError('Use a new attempt directory; never overwrite old run status')
        recorder = Recorder(root)
        start = time.monotonic()
        try:
            if args.recover_413_from:
                while True:
                    parent_status = read(args.recover_413_from / 'STATUS.json')
                    if parent_status['status'] in ('finished', 'finished_with_unresolved'):
                        break
                    if parent_status['status'] not in ('starting', 'running'):
                        raise ValueError('Parent run did not finish normally; automatic recovery withheld')
                    recorder.update(status='waiting_for_parent', parent_run=str(args.recover_413_from))
                    time.sleep(60)
            service = read(root / 'SERVICE_PREFLIGHT.json')
            cpus = len(os.sched_getaffinity(0))
            capacity = {'available_cpus': cpus, 'front_workers': min(4, max(1, cpus // 8)),
                'production_workers': 8, 'mount_workers': 4, 'ready_queue_limit': 32,
                'global_request_limit': 1024, 'pdf_prepare_workers_per_front': 2,
                'minimum_available_memory_bytes': 16 * 1024 ** 3,
                'memory_reservation_per_job_bytes': 4 * 1024 ** 3,
                'memory_available_start_bytes': psutil.virtual_memory().available}
            save(root / 'SCHEDULING_CAPACITY.json', capacity)
            recorder.update(capacity=capacity, scheduling='cross_stage_bounded_flow')
            for track, path in [('dictionary', args.dictionary), ('important', args.important)]:
                save(root / 'inputs' / (track + '_SOURCE_0611_VERIFIED.json'), verify_original_rows(path))
            reused = reuse_dictionary_front(root, args.reuse_dictionary_front, args.dictionary) if args.reuse_dictionary_front else None
            if args.recover_413_from:
                if reused is None:
                    raise ValueError('Recovery requires original revalidated dictionary admissions')
                reused = collect_413_recovery(args.recover_413_from, reused)
                save(root / 'RECOVERY_SELECTION.json', {'count': len(reused), 'books': reused,
                     'criterion': 'failed extraction job with explicit HTTP413; other failures not silently retried',
                     'parent_run': str(args.recover_413_from), 'old_outputs_modified': False})
            with Gateway(root / 'gateway', service['api_url'], limit=1024) as gateway, ResourceMonitor(root, gateway):
                save(root / 'LIVE_SERVICE_CHECK.json', live_service_check(gateway, service))
                subjects = ([item['subject'] for item in reused] if reused is not None else
                            [row['subject_slug'] for row in read(args.dictionary)])
                if not args.recover_413_from:
                    subjects += [row['subject_slug'] for row in read(args.important)]
                memory = MemoryAdmission(capacity['minimum_available_memory_bytes'],
                    capacity['memory_reservation_per_job_bytes'], lambda: psutil.virtual_memory().available)
                with BoundaryWarmup(root, subjects, service, gateway, memory, recorder) as boundaries:
                    recorder.update(boundary_prewarm_started=True, boundary_subjects=list(dict.fromkeys(subjects)))
                    tracks = [('dictionary', args.dictionary)]
                    if not args.recover_413_from:
                        tracks.append(('important', args.important))
                    for track, path in tracks:
                        summary = run_track(root, track, path, service, gateway, recorder,
                            reused=reused if track == 'dictionary' else None, boundaries=boundaries, memory=memory)
                        recorder.state['tracks'][track] = summary
                        recorder.update(last_completed_track=track)
            if args.recover_413_from:
                merge_recovered_results(root, args.recover_413_from, recorder)
            unresolved = any(s['status'] != 'completed_native_stages' or s['partial_extraction_books'] or
                s['front_technical_failed_books'] or s['extraction_unresolved_items']
                for s in recorder.state['tracks'].values())
            recorder.update(status='finished_with_unresolved' if unresolved else 'finished',
                            ended_at=now(), wall_seconds=time.monotonic()-start)
        except BaseException as exc:
            recorder.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                            ended_at=now(), wall_seconds=time.monotonic()-start)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('launch', 'worker', 'check-reuse'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--dictionary', type=Path, required=True)
    parser.add_argument('--important', type=Path, required=True)
    parser.add_argument('--reuse-dictionary-front', type=Path)
    parser.add_argument('--recover-413-from', type=Path, help='Wait for parent completion; retry only explicit HTTP413 books and merge')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if args.action == 'worker':
        worker(args)
        return
    if args.action == 'check-reuse':
        books = reuse_dictionary_front(root, args.reuse_dictionary_front, args.dictionary)
        print(json.dumps({'revalidated_approved_books': len(books), 'model_calls': 0}))
        return
    if (root / 'LAUNCH.json').exists() or (root / 'STATUS.json').exists():
        raise FileExistsError('Refusing duplicate launch')
    command = [sys.executable, '-B', '-u', str(Path(__file__).resolve()), 'worker', '--root', str(root),
               '--dictionary', str(args.dictionary.resolve()), '--important', str(args.important.resolve())]
    if args.reuse_dictionary_front:
        command += ['--reuse-dictionary-front', str(args.reuse_dictionary_front.resolve())]
    if args.recover_413_from:
        command += ['--recover-413-from', str(args.recover_413_from.resolve())]
    for name in ('tmp', 'cache/huggingface'):
        (root / name).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1',
               TMPDIR=str(root / 'tmp'), HF_HOME=str(root / 'cache/huggingface'))
    with (root / 'benchmark.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    save(root / 'LAUNCH.json', {'pid': process.pid, 'at': now(), 'command': command,
         'controller_sha256': sha(__file__), 'dictionary_input_sha256': sha(args.dictionary),
         'important_input_sha256': sha(args.important), 'global_request_limit': 1024,
         'reused_front_run': str(args.reuse_dictionary_front) if args.reuse_dictionary_front else None,
         'track_order': ['dictionary'] if args.recover_413_from else ['dictionary', 'important'],
         'recovery_parent': str(args.recover_413_from) if args.recover_413_from else None, 'quality_approved': False})
    print(json.dumps({'pid': process.pid, 'root': str(root), 'global_request_limit': 1024}))


if __name__ == '__main__':
    main()
