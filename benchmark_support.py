#!/usr/bin/env python3
"""Shared provenance, native configuration and resource helpers for benchmarks."""

import argparse
from contextlib import contextmanager
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

PRIORITY = ['mechanical_engineering', 'electricengineering', 'medicine', 'civil_engineering',
            'transportation', 'automation', 'ic', 'education', 'aerospace', 'electronicinformation',
            'biomedical_engineering', 'architecture', 'ocean_engineering']
SOURCE_0611 = Path('/mnt/nas3/anna_book/book_meta/corpus_es_book_dedup_1451w_deduped_version0611.csv')


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_original_rows(candidates_file, source_path=SOURCE_0611):
    rows = read(candidates_file)
    proofs = []
    paths = {row['match_evidence']['corpus_path'] for row in rows}
    if len(paths) != 1:
        raise ValueError('Expected one original 0611 source corpus')
    source = source_path.resolve(strict=True)
    if Path(next(iter(paths))).resolve(strict=True) != source:
        raise ValueError('Candidate evidence points to a different original source')
    before = source.stat()
    with source.open('rb') as stream:
        header_bytes = stream.readline()
        fields = next(csv.reader([header_bytes.decode('utf-8-sig').replace('\x00', '')]))
        if len(fields) != 31 or len(set(fields)) != 31:
            raise ValueError('Original 0611 header must have exactly 31 unique fields')
        for row in sorted(rows, key=lambda r: r['match_evidence']['source_byte_offset']):
            evidence = row['match_evidence']
            offset = evidence['source_byte_offset']
            if type(offset) is not int or not 0 < offset < before.st_size:
                raise ValueError('Invalid original record byte offset')
            stream.seek(offset)
            raw_parts = []

            def physical_lines():
                size = 0
                while True:
                    raw = stream.readline()
                    if not raw:
                        return
                    size += len(raw)
                    if size > 2 * 1024 * 1024:
                        raise ValueError('Original CSV record exceeds verification safety limit')
                    raw_parts.append(raw)
                    yield raw.decode('utf-8').replace('\x00', '')

            values = next(csv.reader(physical_lines()))
            actual = dict(zip(fields, values))
            raw = b''.join(raw_parts)
            if len(values) != len(fields) or actual != row['original_0611_record'] or actual['identifier'] != row['identifier']:
                raise ValueError('Original 0611 row does not match candidate: ' + row['identifier'])
            raw_hash = hashlib.sha256(raw).hexdigest()
            if evidence.get('source_raw_record_sha256') and raw_hash != evidence['source_raw_record_sha256']:
                raise ValueError('Original raw-record hash changed')
            proofs.append({'identifier': row['identifier'], 'byte_offset': offset, 'raw_record_sha256': raw_hash,
                           'raw_record_bytes': len(raw), 'all_fields_equal': True})
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source corpus changed during verification')
    return {'source': str(source), 'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns,
            'header_sha256': hashlib.sha256(header_bytes).hexdigest(), 'field_count': len(fields),
            'candidate_file_sha256': sha(candidates_file), 'records': proofs,
            'full_18GB_corpus_hashed': False, 'verification': 'each referenced original byte-range and every parsed field'}


def chunks(rows, size=16):
    if type(size) is not int or size < 1:
        raise ValueError('chunk size must be positive')
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


class MemoryAdmission:
    def __init__(self, floor, reservation, available):
        self.floor, self.reservation, self.available = floor, reservation, available
        self.condition = threading.Condition()
        self.reserved = 0

    @contextmanager
    def slot(self):
        with self.condition:
            while self.available() - self.reserved < self.floor + self.reservation:
                self.condition.wait(timeout=1)
            self.reserved += self.reservation
        try:
            yield
        finally:
            with self.condition:
                self.reserved -= self.reservation
                self.condition.notify_all()


def select_cohort(approved, target=50, seed=20261012):
    if type(target) is not int or target < 1:
        raise ValueError('target must be a positive integer')
    ids = [row['book']['identifier'] for row in approved]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate admitted identifiers')
    grouped = defaultdict(list)
    for row in approved:
        grouped[row['subject']].append(row)
    selected = []
    for subject in sorted(grouped, key=lambda s: (PRIORITY.index(s) if s in PRIORITY else len(PRIORITY), s)):
        rows = sorted(grouped[subject], key=lambda r: r['book']['identifier'])
        random.Random(seed).shuffle(rows)
        selected.extend(rows[:target - len(selected)])
        if len(selected) == target:
            break
    return selected


def validate_sources(prep):
    for source in prep['source_stats']:
        path = Path(source['path'])
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (source['size'], source['mtime_ns']):
            raise ValueError('Source changed since classification: ' + str(path))
        if path.suffix.lower() == '.md' and sha(path) != prep['md_sha256']:
            raise ValueError('MD hash changed since classification: ' + str(path))


def screening_scope(code, subject, inputs):
    aliases = {'electricengineering': 'electrical_engineering', 'ic': 'integrated_circuits',
               'aerospace': 'aerospace_engineering', 'electronicinformation': 'electronic_information'}
    source = code / 'modules/book_screening/configs/audit_compatible' / (aliases.get(subject, subject) + '.json')
    if source.is_file():
        scope = read(source)
        scope['subject_slug'] = subject
    else:
        names = {'architecture': 'architecture_scope_config.json', 'ic': 'integrated_circuit.scope.json'}
        source = code / 'modules/dictionary/scope_configs' / names.get(subject, subject + '.scope.json')
        native = read(source)
        definition = '\n'.join(str(native.get(key) or '').strip()
                               for key in ('subject_definition', 'subject_boundary')).strip()
        scope = {'subject_name': native['subject'], 'subject_slug': subject,
            'boundary': {'core_scope': definition, 'direct_subject_labels': [native['subject']],
                         'aliases': [native['subject']],
                         'strong_terms': [node['name'] for node in native.get('l1_nodes', [])]},
            'l1_nodes': [{'name': node['name'], 'path': native['subject'] + '/' + node['name'],
                         'node_definition': node.get('definition', ''), 'node_boundary': node.get('boundary', '')}
                        for node in native.get('l1_nodes', [])]}
    boundary = scope.get('boundary', {})
    if not any(boundary.get(k) for k in ('direct_subject_labels', 'aliases', 'strong_terms')):
        raise ValueError(f'Incomplete existing screening scope: {subject}')
    path = inputs / 'screening_scope.json'
    save(path, scope)
    return path, scope, str(source)


def configure(root, service, track, subject, rows, home, job_id, api_url,
              workers=128, selected=None, global_limit=1024, round_workers=None,
              defer_transport_retry=False, structure_streaming=True,
              cleaning_workers=None, mounting_workers=None):
    code, inputs = root / 'code', home / 'inputs'
    if (home / 'pipeline.json').exists():
        raise FileExistsError('Never overwrite a frozen job configuration')
    from project_taxonomy import subject as project_subject
    project_pin=project_subject(subject,code/'project_assets/LOCK.json')
    inputs.mkdir(parents=True, exist_ok=True)
    scope_path, scope, scope_origin = screening_scope(code, subject, inputs)
    name = scope['subject_name']
    registry = read(code / 'configs/taxonomy_registry.json')
    tree = Path(project_pin['taxonomy'])
    names = {'ic': 'integrated_circuit.scope.json', 'architecture': 'architecture_scope_config.json'}
    native = code / 'modules/dictionary/scope_configs' / names.get(subject, subject + '.scope.json')
    dictionary_scope = inputs / 'dictionary_scope.json'
    if native.is_file():
        shutil.copyfile(native, dictionary_scope)
    else:
        from adapters.mounting_assets import normalize
        _, nodes, _, _ = normalize(tree, name)
        description = scope['boundary'].get('core_scope') or json.dumps(scope['boundary'], ensure_ascii=False)
        save(dictionary_scope, {'subject': name, 'subject_definition': description,
            'subject_boundary': description,
            'l1_nodes': [{'name': n['name_zh'], 'definition': json.dumps(n['existing_context'], ensure_ascii=False),
                         'boundary': 'Use the coverage of this node and its existing children.'}
                        for n in nodes.values() if n['depth'] == 1]})
    raw = [row['original_0611_record'] for row in rows]
    if not raw or any(set(item) != set(raw[0]) for item in raw):
        raise ValueError('Original 0611 field sets differ or are empty')
    catalog = inputs / 'raw_0611.csv'
    with catalog.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(raw[0]))
        writer.writeheader()
        writer.writerows(raw)
    md_root, pdf_root = inputs / 'md_mirror', inputs / 'pdf'
    md_root.mkdir()
    pdf_root.mkdir()
    for row in rows:
        identifier = row['identifier']
        if identifier != row['original_0611_record']['identifier']:
            raise ValueError('Original identifier was changed')
        parsed = row['original_0611_record']['parsed_path']
        prefix = 'oss://pipeline-result1/'
        if not parsed.startswith(prefix):
            raise ValueError('Unsupported original MD prefix')
        mapped = (md_root / parsed[len(prefix):]).resolve()
        if not mapped.is_relative_to(md_root.resolve()):
            raise ValueError('Unsafe MD reference')
        mapped.parent.mkdir(parents=True, exist_ok=True)
        mapped.symlink_to(Path(row['md_path']).resolve(strict=True))
        (pdf_root / (identifier + '.pdf')).symlink_to(Path(row['pdf_path']).resolve(strict=True))
    model_path = inputs / 'model.json'
    model_config = {'api_url': api_url, 'model': service['model'],
        'context_limit': service['configured_context'], 'max_tokens': 8192, 'timeout': 600,
        'chat_template_kwargs': {'enable_thinking': False}}
    if defer_transport_retry:
        model_config['defer_transport_retry'] = True
    save(model_path, model_config)
    from batch_pipeline import bind_model_routes
    config = bind_model_routes(read(code / 'configs/pipeline.example.json'), api_url)
    config['track'] = track
    config['subject'] = {'slug': subject, 'name': name, 'description': read(dictionary_scope)['subject_boundary']}
    config['model']['name'] = service['model']
    config['paths'].update(run=str(home / 'run'), catalog=str(catalog), screening_scope=str(scope_path),
        books=str(inputs / 'external_unused.json'), screening_result='',
        approved_books=str(inputs / 'selected_books.json') if track == 'important' else '',
        md_root='', pdf_root=str(pdf_root), dictionary_scope=str(dictionary_scope),
        taxonomy_dir=str(tree.parent), taxonomy=str(tree), classification_model_config=str(model_path),
        knowledge_input='', cleaning_books='')
    config.update(screening_workers=workers, classification_workers=workers,
                  cleaning_workers=cleaning_workers or workers,
                  book_workers=2, context_limit=service['configured_context'], run_md_quality_audit=True)
    config['screening_environment'].update(MD_PREFIX='oss://pipeline-result1/', MD_ROOT=str(md_root))
    config['book_manifest'].update(enabled=True, allow_partial=True, assume_track=False, allow_empty=True)
    config['structure_streaming'] = {'enabled': structure_streaming, 'prepare_workers': 2,
                                    'max_pending': 32, 'strict_pdf': not structure_streaming}
    if defer_transport_retry:
        config['defer_transport_retry'] = True
    config['dictionary_extraction'] = {'allow_partial': True, 'idle_scope': 'scheduler-job',
        'round_workers': round_workers or [workers, max(1, workers // 4), max(1, workers // 16)]}
    config['dictionary_approval'] = {'enabled': True, 'allowed_families': ['entry_prose']}
    config['important_selection'] = {'enabled': track == 'important'}
    config['important_extraction'] = 'rule'
    config['mounting'].update(workers=mounting_workers or workers, limit=0)
    config['mounting']['boundaries']={'source':'project','enabled':True}
    config['mounting'].pop('inputs', None)
    if selected is not None:
        save(inputs / 'selected_books.json', selected)
        config['dictionary_approval']['enabled'] = False
        config['paths'].update(approved_books=str(inputs / 'selected_books.json'), books=str(inputs / 'selected_books.json'))
    save(home / 'pipeline.json', config)
    save(home / 'INPUT_PROVENANCE.json', {'job_id': job_id, 'created_at': now(), 'track': track,
        'source_alignment_records': rows, 'original_rows_unchanged': True, 'scope_origin': scope_origin,
        'catalog_sha256': sha(catalog), 'taxonomy_sha256': sha(tree), 'project_assets':project_pin,
        'configuration_sha256': sha(home / 'pipeline.json'),
        'code_snapshot': read(root / 'CODE_SNAPSHOT.json'), 'selected': selected,
        'reuses_old_decisions': False, 'global_gateway_limit': global_limit,
        'per_job_workers': workers, 'model': service})
    return {'job_id': job_id, 'subject': subject, 'home': str(home), 'config': str(home / 'pipeline.json')}


class Recorder:
    def __init__(self, root, profile=None):
        self.root, self.lock = root, threading.Lock()
        self.jobs = {}
        self.state = {'status': 'starting', 'started_at': now(),
                      'global_request_limit': profile.global_limit if profile else 1024,
                      'capacity': read(root / 'SCHEDULING_CAPACITY.json') if (root / 'SCHEDULING_CAPACITY.json').exists() else {},
                      'target_per_track': profile.target_per_track if profile else 50,
                      'tracks': {}, 'semantic_quality_approved': False, 'quality_sample_pending': True}

    def update(self, **fields):
        with self.lock:
            self.state.update(fields, updated_at=now())
            save(self.root / 'STATUS.json', self.state)

    def event(self, value):
        with self.lock:
            self.jobs[value['job_id']] = value
            with (self.root / 'JOB_EVENTS.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps(value, ensure_ascii=False) + '\n')
            save(self.root / 'JOBS.json', self.jobs)


class ResourceMonitor:
    def __init__(self, root, gateway):
        self.root, self.gateway = root, gateway
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        import psutil
        parent = psutil.Process()
        with (self.root / 'RESOURCE_SAMPLES.jsonl').open('w', encoding='utf-8') as output:
            while not self.stop.is_set():
                values = []
                for proc in [parent, *parent.children(recursive=True)]:
                    try:
                        cpu, memory = proc.cpu_times(), proc.memory_info()
                        values.append({'pid': proc.pid, 'rss_bytes': memory.rss, 'cpu_seconds': cpu.user + cpu.system})
                    except psutil.Error:
                        pass
                sample = {'at': now(), 'processes': values, 'rss_sum_bytes': sum(p['rss_bytes'] for p in values),
                    'gpu_hardware_utilization': None, 'gpu_memory_bytes': None,
                    'gpu_source': 'Nanhu historical card-monitor snapshots; not vLLM KV cache'}
                try:
                    sample['gateway'] = self.gateway.stats()
                except Exception as exc:
                    sample['gateway_error'] = str(exc)
                try:
                    names = {'vllm:num_requests_running', 'vllm:num_requests_waiting',
                             'vllm:prompt_tokens_total', 'vllm:generation_tokens_total',
                             'vllm:gpu_cache_usage_perc', 'vllm:kv_cache_usage_perc'}
                    with urlopen(self.gateway.api_url + '/metrics', timeout=8) as response:
                        metrics = response.read().decode('utf-8')
                    sample['vllm_metrics'] = [line for line in metrics.splitlines()
                        if re.split(r'[\s{]', line)[0] in names]
                    sample['vllm_metrics_scope'] = 'one load-balanced endpoint; not proven aggregate across all replicas'
                except Exception as exc:
                    sample['metrics_error'] = str(exc)
                output.write(json.dumps(sample, ensure_ascii=False) + '\n')
                output.flush()
                self.stop.wait(30)

    def __enter__(self):
        import psutil
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=10)


def collect_approved(job, track):
    home = Path(job['home'])
    if track == 'dictionary':
        books = read(home / 'run/02_structure/dictionary_approval/approved_books.json')
        proof = home / 'run/02_structure/dictionary_approval/report.json'
    else:
        native = home / 'run/02_structure'
        verified = read(native / 'classification/verification.json')
        # verify() already rejects changed source/config/evidence; require its marker as well.
        verified_results = verified.get('results')
        targets = read(native / 'prepared/manifest.json')['targets']
        if (not isinstance(verified_results, list) or len(verified_results) != len(targets)
                or {r.get('ref') for r in verified_results} != set(targets)):
            raise ValueError('Important classification ID set not verified')
        results = {r['ref']: r for r in verified_results}
        books = []
        for book in read(native / 'book_manifest/books.json'):
            result = results[book['identifier']]
            if result['status'] == 'classified' and result.get('primary_class') in ('N1', 'N3'):
                books.append(book)
        proof = native / 'classification/verification.json'
    return [{'book': book, 'subject': job['subject'], 'parent_job': job,
             'approval_proof': str(proof), 'approval_sha256': sha(proof)} for book in books]
