"""Portable entry point around the frozen current classification implementation."""
import argparse
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import multiprocessing
import shutil
import re
import threading
import time
from pathlib import Path
import sys
from contextlib import closing
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from shared_request_pool import SharedExecutor, completed as shared_completed
from types import SimpleNamespace
import run_md_primary_v5 as io
import run_structure_v6_1 as transport
import run_unseen20_classification as current
import structure_v6
import structure_v6_1


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def classification_identity(base, config_path):
    """Freeze the same native input/model/code identity used by classification."""
    cfg = io.read(config_path)
    cfg['api_url'] = cfg['api_url'].rstrip('/')
    refs = io.read(base / 'manifest.json')['targets']
    return {'base': str(base.resolve()), 'targets': refs, 'config': cfg,
            'code_sha256': {p.name: sha256(p) for p in Path(__file__).parent.glob('*.py')},
            'input_sha256': {ref: sha256(base / 'prepared' / f'{ref}.json')
                             if (base / 'prepared' / f'{ref}.json').exists() else None for ref in refs}}


def technical_retry_candidates(base, out, config_path, books_path=None):
    manifest = io.read(out / 'manifest.json')
    identity = classification_identity(base, config_path)
    for key, expected in identity.items():
        if manifest.get(key) != expected:
            raise ValueError('resume inputs/config/code changed: ' + key)
    if manifest.get('config_sha256') and manifest['config_sha256'] != sha256(config_path):
        raise ValueError('resume config bytes changed')
    if books_path is not None:
        if manifest.get('books_path') != str(books_path.resolve()) or manifest.get('books_sha256') != sha256(books_path):
            raise ValueError('resume books changed')
    pending = []
    if {p.stem for p in (out / 'results').glob('*.json')} != set(identity['targets']):
        raise ValueError('incomplete classification result set')
    for ref in identity['targets']:
        result = io.read(out / 'results' / f'{ref}.json')
        if result.get('ref') != ref:
            raise ValueError('classification result ref changed: ' + ref)
        if result.get('status') == 'technical_failed':
            if not result.get('errors'):
                raise ValueError('technical failure lacks evidence: ' + ref)
            if (identity['input_sha256'][ref] is not None and
                    (not identity['config'].get('defer_transport_retry') or
                     result.get('failure_kind') == 'transport')):
                pending.append(ref)
    return pending


def retry_technical(base, out, config_path, workers=4, books_path=None):
    """Retry only evidenced technical results under the frozen native identity."""
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be positive')
    refs = technical_retry_candidates(base, out, config_path, books_path)
    # Verify every successful result and source before sending any new request.
    verify(out)
    if not refs:
        return {'retried': [], 'remaining': []}
    cfg = io.read(config_path)
    cfg['api_url'] = cfg['api_url'].rstrip('/')
    models = io.http(cfg['api_url'] + '/v1/models')['data']
    if cfg['model'] not in [model['id'] for model in models]:
        raise ValueError('configured_model_unavailable')
    transport.core = SimpleNamespace(PROMPT=current.PROMPT,
                                     add_context=structure_v6_1.add_context,
                                     validate=current.validate)
    history_path = out / 'TECHNICAL_RETRY_HISTORY.json'
    history = io.read(history_path) if history_path.exists() else []
    round_number = len(history) + 1
    if round_number > 2:
        raise ValueError('technical retry rounds exhausted')
    def work(ref):
        archive = out / 'technical_retry_archive' / f'round_{round_number}' / ref
        if archive.exists():
            raise FileExistsError('retry archive already exists: ' + str(archive))
        archive.mkdir(parents=True)
        for folder in ('results', 'raw', 'sent', 'prepared'):
            candidates = ([out / folder / f'{ref}.json'] if folder in ('results', 'prepared') else
                          list((out / folder).glob(f'{ref}_*.json')))
            for source in candidates:
                if source.is_file():
                    dest = archive / folder / source.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(dest))
        try:
            return transport.process(ref, base, out, cfg)
        except Exception as exc:
            result = {'ref': ref, 'status': 'technical_failed', 'errors': [repr(exc)]}
            io.write(out / 'results' / f'{ref}.json', result)
            return result
    with closing(shared_completed(work, refs, workers)) as completions:
        by_ref = {ref: future.result() for ref, future in completions}
    results = [by_ref[ref] for ref in refs]
    summary = {'round': round_number, 'retried': refs,
               'remaining': [r['ref'] for r in results if r.get('status') == 'technical_failed']}
    history.append(summary)
    io.write(history_path, history)
    verify(out)
    return summary


def read_books(books_path):
    books = io.read(books_path)
    if not isinstance(books, list) or any(not isinstance(b, dict) or
            not all(isinstance(b.get(k), str) and b[k] for k in ('identifier', 'title', 'md_path')) or
            (b.get('pdf_path') is not None and not isinstance(b['pdf_path'], str)) for b in books):
        raise ValueError('books must be a list of complete book objects')
    refs = [b['identifier'] for b in books]
    if len(set(refs)) != len(refs) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', r) for r in refs):
        raise ValueError('identifiers must be nonempty, unique, and filename-safe ASCII')
    return books


def prepare_one(book, out, strict_pdf=False):
    ref = book['identifier']
    try:
        md = Path(book['md_path']).resolve(strict=True)
        if md.suffix.lower() != '.md': raise ValueError('md_path requires .md extension')
        rows = md.read_text(encoding='utf-8-sig').splitlines()
        if not rows or not any(r.strip() for r in rows): raise ValueError('empty_md')
        windows = [w for w in structure_v6.sample_md(rows) if w['lines']]
        pdf = Path(book['pdf_path']).resolve(strict=True) if book.get('pdf_path') else None
        sources = [md]+([pdf] if pdf else [])
        stats = [{'path':str(p), 'size':p.stat().st_size, 'mtime_ns':p.stat().st_mtime_ns} for p in sources]
        error = None
        if pdf:
            try:
                if not io.is_pdf(pdf): raise ValueError('source_is_not_pdf')
                io.attach_format(windows, pdf)
            except Exception as exc:
                if strict_pdf: raise
                error = repr(exc)
        else: error = 'pdf_not_supplied'
        io.write(out/'prepared'/f'{ref}.json', {'ref':ref, 'title':book['title'],
            'source_stats':stats, 'md_sha256':hashlib.sha256(md.read_bytes()).hexdigest(),
            'windows':windows, 'pdf_format_error':error, 'scope':'sampled classification, not extraction'})
    except Exception as exc:
        io.write(out/'preparation_failed'/f'{ref}.json', {'ref':ref,'status':'technical_failed','errors':[repr(exc)]})


def prepare(books_path, out, strict_pdf=False):
    books = read_books(books_path)
    refs = [b['identifier'] for b in books]
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'targets': refs, 'books': books, 'classification_only': True}
    io.write(out/'manifest.json', manifest)
    for book in books:
        prepare_one(book, out, strict_pdf=strict_pdf)
    io.write(out/'PREPARED.json', {'expected':len(refs), 'prepared':len(list((out/'prepared').glob('*.json'))),
                                 'failures':len(list((out/'preparation_failed').glob('*.json')))})


def classify(base, out, config_path, workers):
    import fcntl
    if workers < 1: raise ValueError('workers must be positive')
    cfg = io.read(config_path)
    for key in ('api_url','model','context_limit'):
        if key not in cfg: raise ValueError('missing config: '+key)
    cfg['api_url'] = cfg['api_url'].rstrip('/')
    refs = io.read(base/'manifest.json')['targets']
    out.mkdir(parents=True, exist_ok=True)
    (out/'results').mkdir(exist_ok=True)
    with (out/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        signature = {'base':str(base.resolve()), 'targets':refs, 'config':cfg,
                     'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},
                     'input_sha256':{ref:hashlib.sha256((base/'prepared'/f'{ref}.json').read_bytes()).hexdigest()
                                     if (base/'prepared'/f'{ref}.json').exists() else None for ref in refs}}
        if (out/'manifest.json').exists():
            if io.read(out/'manifest.json') != signature: raise ValueError('resume inputs/config/code changed; use a new output directory')
        else: io.write(out/'manifest.json', signature)
        models = io.http(cfg['api_url']+'/v1/models')['data']
        if cfg['model'] not in [m['id'] for m in models]: raise ValueError('configured_model_unavailable')
        transport.core = SimpleNamespace(PROMPT=current.PROMPT, add_context=structure_v6_1.add_context, validate=current.validate)
        def work(ref):
            if (out/'results'/f'{ref}.json').exists(): return io.read(out/'results'/f'{ref}.json')
            try:
                if not (base/'prepared'/f'{ref}.json').exists():
                    raise ValueError('preparation_failed; inspect preparation_failed/'+ref+'.json')
                return transport.process(ref, base, out, cfg)
            except Exception as exc:
                result = {'ref':ref,'status':'technical_failed','errors':[repr(exc)]}
                io.write(out/'results'/f'{ref}.json',result)
                return result
        pending = [ref for ref in refs if not (out/'results'/f'{ref}.json').exists()]
        if pending:
            probe = next((r for r in pending if (base/'prepared'/f'{r}.json').exists()), None)
            if probe:
                with closing(shared_completed(work, [probe], 1)) as completions:
                    first = next(completions)[1].result()
                pending.remove(probe)
                print(first['ref'], first['status'], flush=True)
        with closing(shared_completed(work, pending, workers)) as completions:
            for _, future in completions:
                result = future.result()
                print(result['ref'], result['status'], flush=True)
                io.write(out/'PROGRESS.json', {'expected':len(refs),
                         'finished':len(list((out/'results').glob('*.json')))})
        verify(out)


def prepare_run(books_path, prepared_dir, classified_dir, config_path, workers=4,
                prepare_workers=2, max_pending=32):
    books = read_books(books_path)
    if workers < 1 or prepare_workers < 1 or max_pending < prepare_workers:
        raise ValueError('workers and prepare-workers must be positive; max-pending must cover prepare-workers')
    if prepared_dir.resolve() == classified_dir.resolve():
        raise ValueError('prepared and classification directories must differ')
    if prepared_dir.exists() or classified_dir.exists():
        if not (prepared_dir / 'manifest.json').is_file() or not (classified_dir / 'manifest.json').is_file():
            raise ValueError('incomplete_streaming_output_requires_native_recovery')
        technical_retry_candidates(prepared_dir, classified_dir, config_path, books_path)
        verify(classified_dir)
        return
    cfg = io.read(config_path)
    if not isinstance(cfg, dict) or any(k not in cfg for k in ('api_url', 'model', 'context_limit')):
        raise ValueError('missing model config')
    if (not isinstance(cfg['api_url'], str) or not cfg['api_url'] or
        not isinstance(cfg['model'], str) or not cfg['model'] or
        isinstance(cfg['context_limit'], bool) or not isinstance(cfg['context_limit'], int) or
        cfg['context_limit'] < 502):
        raise ValueError('invalid model config')
    output_budget = cfg.get('max_tokens', 16000)
    if (isinstance(output_budget, bool) or not isinstance(output_budget, int) or
        output_budget < 1 or output_budget + 500 >= cfg['context_limit']):
        raise ValueError('invalid_output_budget')
    cfg['api_url'] = cfg['api_url'].rstrip('/')
    models = io.http(cfg['api_url']+'/v1/models')['data']
    if cfg['model'] not in [m['id'] for m in models]:
        raise ValueError('configured_model_unavailable')
    books_hash = hashlib.sha256(books_path.read_bytes()).hexdigest()
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    code_hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in Path(__file__).parent.glob('*.py')}
    source_snapshot = {}
    for book in books:
        for field in ('md_path', 'pdf_path'):
            if book.get(field):
                path = Path(book[field]).resolve()
                try:
                    stat = path.stat()
                    source_snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    source_snapshot[str(path)] = None
    started = time.time()
    refs = [b['identifier'] for b in books]
    prepared_dir.mkdir(parents=True, exist_ok=False)
    classified_dir.mkdir(parents=True, exist_ok=False)
    (classified_dir/'results').mkdir()
    io.write(prepared_dir/'manifest.json', {'targets':refs, 'books':books, 'classification_only':True})
    transport.core = SimpleNamespace(PROMPT=current.PROMPT, add_context=structure_v6_1.add_context,
                                     validate=current.validate)
    measurements = {'started_at':started, 'first_model_started_at':None,
                    'last_preparation_finished_at':None, 'peak_pending':0,
                    'peak_preparing':0, 'peak_model':0}
    measurement_lock = threading.Lock()

    def work(ref):
        if not (prepared_dir/'prepared'/f'{ref}.json').exists():
            failure = io.read(prepared_dir/'preparation_failed'/f'{ref}.json')
            result = {'ref':ref, 'status':'technical_failed', 'errors':failure['errors']}
            io.write(classified_dir/'results'/f'{ref}.json', result)
            return result
        try:
            with measurement_lock:
                if measurements['first_model_started_at'] is None:
                    measurements['first_model_started_at'] = time.time()
            return transport.process(ref, prepared_dir, classified_dir, cfg)
        except Exception as exc:
            result = {'ref':ref, 'status':'technical_failed', 'errors':[repr(exc)]}
            io.write(classified_dir/'results'/f'{ref}.json', result)
            return result

    next_book = 0
    finished = 0
    prepared_hashes = {}
    preparing = {}
    modeling = {}
    with ProcessPoolExecutor(max_workers=prepare_workers,
                             mp_context=multiprocessing.get_context('spawn')) as prep_pool, \
         SharedExecutor(max_workers=workers) as model_pool:
        while next_book < len(books) or preparing or modeling:
            while (next_book < len(books) and len(preparing) < prepare_workers and
                   len(preparing) + len(modeling) < max_pending):
                book = books[next_book]
                preparing[prep_pool.submit(prepare_one, book, prepared_dir, True)] = book['identifier']
                next_book += 1
            measurements['peak_preparing'] = max(measurements['peak_preparing'], len(preparing))
            measurements['peak_model'] = max(measurements['peak_model'], len(modeling))
            measurements['peak_pending'] = max(measurements['peak_pending'], len(preparing)+len(modeling))
            done, _ = wait(set(preparing) | set(modeling), return_when=FIRST_COMPLETED)
            for future in done:
                if future in modeling:
                    ref = modeling.pop(future)
                    result = future.result()
                    finished += 1
                    print(ref, result['status'], flush=True)
                    io.write(classified_dir/'PROGRESS.json',
                             {'expected':len(refs), 'finished':finished})
            for future in done:
                if future in preparing:
                    ref = preparing.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        io.write(prepared_dir/'preparation_failed'/f'{ref}.json',
                                 {'ref':ref, 'status':'technical_failed', 'errors':[repr(exc)]})
                    packet = prepared_dir/'prepared'/f'{ref}.json'
                    prepared_hashes[ref] = hashlib.sha256(packet.read_bytes()).hexdigest() if packet.exists() else None
                    measurements['last_preparation_finished_at'] = time.time()
                    modeling[model_pool.submit(work, ref)] = ref
    io.write(prepared_dir/'PREPARED.json',
             {'expected':len(refs), 'prepared':len(list((prepared_dir/'prepared').glob('*.json'))),
              'failures':len(list((prepared_dir/'preparation_failed').glob('*.json')))})
    for path_string, original in source_snapshot.items():
        path = Path(path_string)
        try:
            stat = path.stat()
            observed_stats = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            observed_stats = None
        if observed_stats != original:
            raise ValueError('source_changed_during_run: '+path_string)
    models = io.http(cfg['api_url']+'/v1/models')['data']
    if cfg['model'] not in [m['id'] for m in models]:
        raise ValueError('configured_model_unavailable')
    signature = {'base':str(prepared_dir.resolve()), 'targets':refs, 'config':cfg,
                 'books_path':str(books_path.resolve()), 'config_path':str(config_path.resolve()),
                 'books_sha256':books_hash, 'config_sha256':config_hash,
                 'code_sha256':code_hashes,
                 'input_sha256':prepared_hashes,
                 'streaming':True}
    io.write(classified_dir/'manifest.json', signature)
    measurements['finished_at'] = time.time()
    measurements['elapsed_seconds'] = measurements['finished_at']-started
    io.write(classified_dir/'METRICS.json', measurements)
    verify(classified_dir)


def verify(out):
    (out/'DONE.json').unlink(missing_ok=True)
    manifest = io.read(out/'manifest.json')
    refs = manifest['targets']
    if manifest.get('streaming'):
        base = Path(manifest['base'])
        for key in ('books', 'config'):
            path = Path(manifest[key+'_path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[key+'_sha256']:
                raise ValueError(key+'_changed')
        for name, expected_hash in manifest['code_sha256'].items():
            if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != expected_hash:
                raise ValueError('code_changed: '+name)
        for ref in refs:
            packet = base/'prepared'/f'{ref}.json'
            expected_hash = manifest['input_sha256'][ref]
            if packet.exists() != (expected_hash is not None):
                raise ValueError('prepared ID set changed: '+ref)
            if packet.exists():
                if hashlib.sha256(packet.read_bytes()).hexdigest() != expected_hash:
                    raise ValueError('prepared changed: '+ref)
                prep = io.read(packet)
                for source in prep['source_stats']:
                    path = Path(source['path'])
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (source['size'], source['mtime_ns']):
                        raise ValueError('source_changed: '+ref)
                md = next(Path(s['path']) for s in prep['source_stats'] if s['path'].lower().endswith('.md'))
                if hashlib.sha256(md.read_bytes()).hexdigest() != prep['md_sha256']:
                    raise ValueError('md_hash_changed: '+ref)
            elif not (base/'preparation_failed'/f'{ref}.json').exists():
                raise ValueError('missing preparation failure: '+ref)
    actual = {p.stem for p in (out/'results').glob('*.json')}
    if actual != set(refs): raise ValueError('incomplete output ID set')
    results = [io.read(out/'results'/f'{ref}.json') for ref in refs]
    for expected_ref, result in zip(refs, results):
        if result.get('ref') != expected_ref or not isinstance(result.get('status'), str):
            raise ValueError('malformed result')
        if result.get('status') == 'technical_failed':
            if not result.get('errors'): raise ValueError('missing technical failure evidence')
            continue
        ref = result['ref']
        prep = io.read(out/'prepared'/f'{ref}.json')
        paths = sorted((out/'raw').glob(ref+'_*.json'))
        if not paths: raise ValueError('missing raw response: '+ref)
        if not list((out/'sent').glob(ref+'_*.json')):
            raise ValueError('missing sent request: '+ref)
        value = io.read(paths[-1])['choices'][0]['message']['content']
        import json
        validated = current.validate(json.loads(value), prep['windows'])
        if validated['status'] != result['status']: raise ValueError('validation drift: '+ref)
    summary = {'expected':len(refs),'finished':len(results),'id_sets_equal':True,
               'statuses':dict(Counter(r['status'] for r in results)),
               'semantic_audit_complete':False,'extraction_performed':False}
    io.write(out/'DONE.json',summary)
    print(summary)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare'); prep.add_argument('--books',type=Path,required=True); prep.add_argument('--out',type=Path,required=True)
    prep.add_argument('--strict-pdf', action='store_true')
    run = sub.add_parser('run'); run.add_argument('--base',type=Path,required=True); run.add_argument('--out',type=Path,required=True)
    run.add_argument('--config',type=Path,required=True); run.add_argument('--workers',type=int,default=4)
    check = sub.add_parser('verify'); check.add_argument('--out',type=Path,required=True)
    stream = sub.add_parser('prepare-run')
    stream.add_argument('--books',type=Path,required=True)
    stream.add_argument('--out',type=Path,required=True)
    stream.add_argument('--classification',type=Path,required=True)
    stream.add_argument('--config',type=Path,required=True)
    stream.add_argument('--workers',type=int,default=4)
    stream.add_argument('--prepare-workers',type=int,default=2)
    stream.add_argument('--max-pending',type=int,default=32)
    retry = sub.add_parser('retry-technical')
    retry.add_argument('--base',type=Path,required=True)
    retry.add_argument('--out',type=Path,required=True)
    retry.add_argument('--config',type=Path,required=True)
    retry.add_argument('--books',type=Path)
    retry.add_argument('--workers',type=int,default=4)
    a = p.parse_args()
    if a.command=='prepare': prepare(a.books,a.out,strict_pdf=a.strict_pdf)
    elif a.command=='run': classify(a.base,a.out,a.config,a.workers)
    elif a.command=='prepare-run': prepare_run(a.books,a.out,a.classification,a.config,
                                               a.workers,a.prepare_workers,a.max_pending)
    elif a.command=='retry-technical': print(json.dumps(retry_technical(a.base,a.out,a.config,a.workers,a.books)))
    else: verify(a.out)
