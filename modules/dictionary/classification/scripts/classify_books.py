"""Portable entry point around the frozen current classification implementation."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
import run_md_primary_v5 as io
import run_structure_v6_1 as transport
import run_unseen20_classification as current
import structure_v6
import structure_v6_1


def prepare(books_path, out):
    books = io.read(books_path)
    refs = [b['identifier'] for b in books]
    if not refs or len(set(refs)) != len(refs) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', r) for r in refs):
        raise ValueError('identifiers must be nonempty, unique, and filename-safe ASCII')
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'targets': refs, 'books': books, 'classification_only': True}
    io.write(out/'manifest.json', manifest)
    for book in books:
        ref = book['identifier']
        try:
            md = Path(book['md_path']).resolve(strict=True)
            if md.suffix.lower() != '.md': raise ValueError('md_path requires .md extension')
            rows = md.read_text(encoding='utf-8-sig').splitlines()
            if not rows or not any(r.strip() for r in rows): raise ValueError('empty_md')
            windows = structure_v6.sample_md(rows)
            windows = [w for w in windows if w['lines']]
            pdf = Path(book['pdf_path']).resolve(strict=True) if book.get('pdf_path') else None
            sources = [md]+([pdf] if pdf else [])
            stats = [{'path':str(p), 'size':p.stat().st_size, 'mtime_ns':p.stat().st_mtime_ns} for p in sources]
            error = None
            if pdf:
                try:
                    if not io.is_pdf(pdf): raise ValueError('source_is_not_pdf')
                    io.attach_format(windows, pdf)
                except Exception as exc: error = repr(exc)
            else: error = 'pdf_not_supplied'
            io.write(out/'prepared'/f'{ref}.json', {'ref':ref, 'title':book['title'],
                'source_stats':stats, 'md_sha256':hashlib.sha256(md.read_bytes()).hexdigest(),
                'windows':windows, 'pdf_format_error':error, 'scope':'sampled classification, not extraction'})
        except Exception as exc:
            io.write(out/'preparation_failed'/f'{ref}.json', {'ref':ref,'status':'technical_failed','errors':[repr(exc)]})
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
                first = work(probe)
                pending.remove(probe)
                if first['status']=='technical_failed':
                    io.write(out/'FAILED.json', {'reason':'preflight_failed','ref':probe})
                    return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, ref) for ref in pending]
            for future in as_completed(futures):
                result = future.result()
                print(result['ref'], result['status'], flush=True)
                io.write(out/'PROGRESS.json', {'expected':len(refs),'finished':len(list((out/'results').glob('*.json')))})
        verify(out)


def verify(out):
    refs = io.read(out/'manifest.json')['targets']
    actual = {p.stem for p in (out/'results').glob('*.json')}
    if actual != set(refs): raise ValueError('incomplete output ID set')
    results = [io.read(out/'results'/f'{ref}.json') for ref in refs]
    for result in results:
        if result.get('status') == 'technical_failed': continue
        ref = result['ref']
        prep = io.read(out/'prepared'/f'{ref}.json')
        paths = sorted((out/'raw').glob(ref+'_*.json'))
        if not paths: raise ValueError('missing raw response: '+ref)
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
    run = sub.add_parser('run'); run.add_argument('--base',type=Path,required=True); run.add_argument('--out',type=Path,required=True)
    run.add_argument('--config',type=Path,required=True); run.add_argument('--workers',type=int,default=4)
    check = sub.add_parser('verify'); check.add_argument('--out',type=Path,required=True)
    a = p.parse_args()
    if a.command=='prepare': prepare(a.books,a.out)
    elif a.command=='run': classify(a.base,a.out,a.config,a.workers)
    else: verify(a.out)
