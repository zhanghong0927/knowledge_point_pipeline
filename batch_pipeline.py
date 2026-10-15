#!/usr/bin/env python3
"""Bounded multi-subject execution with one shared inference request gateway."""

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import SimpleQueue
import re
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def bounded_map(items, function, workers):
    """Consume only as many input items as can run; never retain all futures."""
    if type(workers) is not int or workers <= 0:
        raise ValueError('workers must be a positive integer')
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        exhausted = False
        while pending or not exhausted:
            while not exhausted and len(pending) < workers:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                else:
                    pending.add(pool.submit(function, item))
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                yield future.result()


def bounded_pipeline(items, prepare, process, prepare_workers=4, process_workers=8,
                     max_pending=32):
    """Stream two stages through separate bounded pools, yielding completed results."""
    for name, limit in (('prepare_workers', prepare_workers),
                        ('process_workers', process_workers), ('max_pending', max_pending)):
        if type(limit) is not int or limit <= 0:
            raise ValueError(f'{name} must be a positive integer')

    iterator = iter(items)
    completed = SimpleQueue()
    ready = deque()
    preparing = set()
    processing = set()
    exhausted = False

    with ThreadPoolExecutor(max_workers=prepare_workers) as prepare_pool, \
            ThreadPoolExecutor(max_workers=process_workers) as process_pool:
        def submit_ready():
            while ready and len(processing) < process_workers:
                future = process_pool.submit(process, ready.popleft())
                processing.add(future)
                future.add_done_callback(completed.put)

        def fill_preparing():
            nonlocal exhausted
            while (not exhausted and len(preparing) < prepare_workers
                   and len(preparing) + len(ready) < max_pending):
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                future = prepare_pool.submit(prepare, item)
                preparing.add(future)
                future.add_done_callback(completed.put)

        try:
            fill_preparing()
            while preparing or processing or ready or not exhausted:
                submit_ready()
                fill_preparing()
                if not preparing and not processing and not ready:
                    break
                future = completed.get()
                if future in preparing:
                    preparing.remove(future)
                    ready.append(future.result())
                elif future in processing:
                    processing.remove(future)
                    result = future.result()
                    submit_ready()
                    fill_preparing()
                    yield result
        finally:
            for future in preparing | processing:
                future.cancel()


def bind_model_routes(config, api_url):
    """Reject untracked custom model clients instead of bypassing the global cap."""
    hooks = config.get('hooks') or {}
    if not isinstance(hooks, dict) or any(hooks.values()):
        raise ValueError('Batch scheduling does not accept custom hooks with untracked model routes')
    if config.get('track') == 'important' and config.get('important_extraction', 'rule') == 'llm':
        raise ValueError('Separate important_llm services are not yet supported by the shared gateway')
    result = deepcopy(config)
    result['model']['api_url'] = api_url
    result.setdefault('dictionary_extraction', {})['idle_scope'] = 'scheduler-job'
    result.setdefault('screening_environment', {})['API_URL'] = '{chat_url}'
    # Mounting overrides are literals, not formatted templates in pipeline.plan.
    result.setdefault('mounting', {})['api_url'] = api_url + '/v1/chat/completions'
    result['mounting'].setdefault('boundaries', {})['api_url'] = api_url + '/v1/chat/completions'
    return result


class Gateway:
    def __init__(self, out, upstream, limit=1024, max_pending=2048, tokenize_limit=64):
        self.out = Path(out)
        self.upstream, self.limit = upstream, limit
        self.max_pending, self.tokenize_limit = max_pending, tokenize_limit
        self.process = self.log = None
        self.api_url = None
        self.previous_environment = None

    def __enter__(self):
        if self.out.exists():
            raise FileExistsError(f'Use a new gateway directory: {self.out}')
        self.out.mkdir(parents=True)
        self.log = (self.out / 'gateway.log').open('w', encoding='utf-8')
        command = [sys.executable, '-B', '-u', str(ROOT / 'adapters/inference_gateway.py'),
                   '--upstream', self.upstream, '--host', '127.0.0.1', '--port', '0',
                   '--limit', str(self.limit), '--max-pending', str(self.max_pending),
                   '--tokenize-limit', str(self.tokenize_limit), '--out', str(self.out)]
        self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f'Gateway failed to start; see {self.out / "gateway.log"}')
                ready = self.out / 'READY.json'
                if ready.is_file():
                    data = json.loads(ready.read_text(encoding='utf-8'))
                    self.api_url = data['api_url'].rstrip('/')
                    with urlopen(self.api_url + '/_scheduler/health', timeout=2) as response:
                        if response.status != 200:
                            raise RuntimeError('Gateway not healthy')
                    self.previous_environment = {key: os.environ.get(key) for key in
                                                 ('PIPELINE_SHARED_POOL_URL', 'PYTHONPATH')}
                    os.environ['PIPELINE_SHARED_POOL_URL'] = self.api_url
                    os.environ['PYTHONPATH'] = str(ROOT) + (os.pathsep + os.environ['PYTHONPATH']
                                                           if os.environ.get('PYTHONPATH') else '')
                    return self
                time.sleep(.1)
            raise TimeoutError('Gateway readiness timeout')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def stats(self):
        with urlopen(self.api_url + '/_scheduler/stats', timeout=5) as response:
            return json.load(response)

    def job_url(self, job_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
            raise ValueError('Unsafe job ID')
        return self.api_url + '/_jobs/' + job_id

    def __exit__(self, *_):
        if self.previous_environment is not None:
            for key, value in self.previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.previous_environment = None
        if self.process and self.process.poll() is None:
            try:
                save(self.out / 'FINAL_OBSERVED_STATS.json', self.stats())
            except Exception as exc:
                save(self.out / 'FINAL_STATS_ERROR.json', {'error': str(exc)})
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()


def execute_job(job, event=None):
    """Execute frozen native stage plans; report failures without forging handoffs."""
    home = Path(job['home'])
    home.mkdir(parents=True, exist_ok=True)
    result = {'job_id': job['job_id'], 'started_at': now(), 'stages': [], 'status': 'running'}
    begun = time.monotonic()
    try:
        for stage in job['stages']:
            if event:
                event({'job_id': job['job_id'], 'stage': stage, 'status': 'running', 'at': now()})
            command = [sys.executable, '-B', str(ROOT / 'pipeline.py'), 'run',
                       '--config', str(job['config']), '--stages', stage, '--resume']
            if job.get('tasks'):
                command += ['--tasks', ','.join(job['tasks'])]
            if job.get('retry_failed'):
                command.append('--retry-failed')
            start = time.monotonic()
            at = now()
            with (home / 'pipeline.log').open('a', encoding='utf-8') as log:
                run = subprocess.run(command, cwd=ROOT, env=os.environ.copy(),
                                     stdout=log, stderr=subprocess.STDOUT)
            timing = {'stage': stage, 'started_at': at, 'ended_at': now(),
                      'wall_seconds': time.monotonic() - start, 'returncode': run.returncode}
            result['stages'].append(timing)
            if run.returncode:
                raise RuntimeError(f'Stage {stage} failed; see {home / "pipeline.log"}')
        result['status'] = 'completed'
    except Exception as exc:
        result.update(status='failed', error=str(exc))
    result.update(ended_at=now(), wall_seconds=time.monotonic() - begun)
    save(home / 'JOB_RESULT.json', result)
    if event:
        event({**result, 'at': now()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=Path, required=True, help='JSONL: {job_id, config, stages}')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--limit', type=int, default=1024)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Batch output must be a new directory')
    args.out.mkdir(parents=True)
    with Gateway(args.out / 'gateway', args.upstream, args.limit) as gateway:
        def jobs():
            seen, runs = set(), set()
            with args.jobs.open(encoding='utf-8-sig') as stream:
                for line in stream:
                    original = json.loads(line)
                    job_id = original['job_id']
                    api_url = gateway.job_url(job_id)
                    if job_id in seen:
                        raise ValueError('Duplicate job ID')
                    seen.add(job_id)
                    source = Path(original['config']).resolve()
                    import pipeline
                    cfg, paths = pipeline.load_config(source)
                    if paths['run'] in runs:
                        raise ValueError('Two jobs cannot share one native run directory')
                    runs.add(paths['run'])
                    cfg['paths'].update({k: paths[k] for k in cfg['paths']})
                    cfg = bind_model_routes(cfg, api_url)
                    stage_workers = max(1, args.limit // args.workers)
                    cfg.update(screening_workers=stage_workers, classification_workers=stage_workers,
                               cleaning_workers=stage_workers)
                    cfg['structure_streaming'] = {'enabled': True, 'prepare_workers': 2, 'max_pending': 32}
                    cfg.setdefault('dictionary_extraction', {})['round_workers'] = [
                        stage_workers, max(1, stage_workers // 4), max(1, stage_workers // 16)]
                    cfg['mounting']['workers'] = stage_workers
                    cfg['mounting'].setdefault('boundaries', {})['workers'] = stage_workers
                    home = args.out / 'jobs' / job_id
                    model_path = home / 'classification_model.json'
                    model = json.loads(Path(paths['classification_model_config']).read_text(encoding='utf-8-sig'))
                    model['api_url'] = api_url
                    save(model_path, model)
                    cfg['paths']['classification_model_config'] = str(model_path.resolve())
                    save(home / 'pipeline.json', cfg)
                    save(home / 'CONFIG_PROVENANCE.json', {'source': str(source),
                         'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                         'shared_gateway': api_url, 'global_limit': args.limit})
                    yield {'job_id': job_id, 'home': str(home), 'config': str(home / 'pipeline.json'),
                           'stages': original.get('stages', ['01', '02', '03', '04', '05', '06'])}

        failures = 0
        with (args.out / 'RESULTS.jsonl').open('w', encoding='utf-8') as output:
            for result in bounded_map(jobs(), execute_job, args.workers):
                failures += result['status'] != 'completed'
                output.write(json.dumps(result, ensure_ascii=False) + '\n')
                output.flush()
        save(args.out / 'SUMMARY.json', {'failed_jobs': failures, 'gateway': gateway.stats(),
             'quality_verified': False, 'finished_at': now()})
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
