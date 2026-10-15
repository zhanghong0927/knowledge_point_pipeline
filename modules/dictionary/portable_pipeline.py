"""Portable adapters for the existing full-book extractor and v22/v9/v46 cleaner."""
import argparse
import bisect
import collections
import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT.parents[1]))
from round_integrity import digest_file, full_verification
NAME = 'llm_name_title_format_quality'
SCOPE = 'subject_scope_quality'
STANDARD = ('id','knowledge_point','name','definition','en_definition','description','en_description','source')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    tmp.replace(path)


def loadl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def digest(path):
    return digest_file(path)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Identifier must contain ASCII letters, digits, underscore or hyphen')


def validate_books(books):
    if not isinstance(books, list) or not books:
        raise ValueError('Expected a nonempty JSON book array')
    ids = set()
    for book in books:
        safe_id(book.get('identifier'))
        if book['identifier'] in ids:
            raise ValueError('Duplicate book identifier: ' + book['identifier'])
        ids.add(book['identifier'])
        if not isinstance(book.get('title'), str) or not book['title'].strip():
            raise ValueError('Each book needs an original title')
        md = Path(book['md_path'])
        if not md.is_absolute() or not md.is_file():
            raise ValueError('MD path must be an existing absolute file path')
        for key in ('pdf_path','pdf_evidence_path','scope_config'):
            if book.get(key):
                source = Path(book[key])
                if not source.is_absolute() or not source.is_file():
                    raise ValueError(key + ' must be an existing absolute file path')
    return books


def freeze_run(out, manifest):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / 'MANIFEST.json'
    if target.exists():
        if read(target) != manifest:
            raise ValueError('Run inputs/configuration/code changed; use a new output directory')
    else:
        write(target, manifest)


def source_hashes(books):
    return {str(Path(book[key])):digest(book[key]) for book in books
            for key in ('md_path','pdf_path','pdf_evidence_path','scope_config') if book.get(key)}


def code_hashes():
    return {str(path.relative_to(ROOT)):digest(path) for path in ROOT.rglob('*.py')
            if '__pycache__' not in path.parts and not any(part in ('outputs','runs') for part in path.relative_to(ROOT).parts)}


def read_evidence(path):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def validate_evidence(evidence, text, md_hash):
    if evidence.get('md_sha256') != md_hash:
        raise ValueError('MD and PDF evidence hash mismatch')
    actual = [r for w in evidence.get('windows',[]) for r in w.get('lines',[])]
    expected = text.splitlines()
    by_line = {}
    for row in actual:
        number = int(row['id'].split(':')[1])
        if number in by_line:
            raise ValueError('Repeated MD line in full-book PDF evidence')
        by_line[number] = row['text']
    if by_line != {i+1:line for i,line in enumerate(expected)}:
        raise ValueError('PDF evidence does not preserve all MD lines')


def prepare_sources(books, out):
    books = validate_books(books)
    out = Path(out)
    before = source_hashes(books)
    freeze_run(out, {'kind':'source_preparation','books':books,'hashes':before})
    if (out/'books.json').exists():
        result = read(out/'books.json')
        for book in result:
            validate_evidence(read_evidence(book['pdf_evidence_path']),
                              Path(book['md_path']).read_bytes().decode('utf-8-sig'),digest(book['md_path']))
        return result
    prepared, status = [], []
    for book in books:
        raw = Path(book['md_path']).read_bytes()
        text = raw.decode('utf-8-sig')
        md_hash = hashlib.sha256(raw).hexdigest()
        if book.get('pdf_evidence_path'):
            evidence = read_evidence(book['pdf_evidence_path'])
            validate_evidence(evidence, text, md_hash)
            mode = 'existing_full_md_format_cache'
        else:
            lines = text.splitlines()
            windows = [{'window_id':f'full:{start+1}', 'lines':[
                {'id':f'md:{i+1}','text':lines[i]} for i in range(start,min(start+80,len(lines)))]}
                for start in range(0,len(lines),80)]
            mode = 'md_only_no_pdf'
            if book.get('pdf_path'):
                from run_md_primary_v5 import attach_format, is_pdf
                pdf = Path(book['pdf_path'])
                if not is_pdf(pdf):
                    raise ValueError('Original file is not a PDF; no PDF format evidence generated')
                attach_format(windows, pdf)
                mode = 'full_pdf_scanned_alignment_partial_allowed'
            evidence = {'md_sha256':md_hash,'windows':windows}
        validate_evidence(evidence, text, md_hash)
        dest = out/'evidence'/(book['identifier']+'.json.gz')
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(gzip.compress(json.dumps(evidence,ensure_ascii=False).encode('utf-8'),mtime=0))
        spec = {**book,'pdf_evidence_path':str(dest.resolve()),'pdf_mode':mode}
        prepared.append(spec)
        status.append({'identifier':book['identifier'],'md_lines':len(text.splitlines()),'pdf_mode':mode,
                       'formatted_lines':sum(bool(r.get('pdf_format')) for w in evidence['windows'] for r in w['lines']),
                       'alignment':dict(collections.Counter(w.get('pdf_format_status','unavailable') for w in evidence['windows']))})
    if source_hashes(books) != before:
        raise ValueError('Source files changed during preparation')
    write(out/'books.json',prepared)
    write(out/'PREPARED.json',{'books':len(prepared),'source_hashes_verified':True,'per_book':status})
    return prepared


def prepare_cleaning(books, extraction, out):
    books = validate_books(books)
    by_id = {b['identifier']:b for b in books}
    for book in books:
        safe_id(book.get('subject_slug'))
        if not book.get('scope_config'):
            raise ValueError('Missing verified source-subject scope configuration for ' + book['identifier'])
    extraction, out = Path(extraction), Path(out)
    accepted = sorted(extraction.rglob('accepted_entries.json'))
    if not accepted:
        raise ValueError('No finished extraction accepted_entries.json found')
    files = {str(file):digest(file) for file in accepted}
    for file in accepted:
        for name in ('SUMMARY.json','units.json','quarantined_entries.json'):
            other = file.parent/name
            if not other.exists():
                raise ValueError('Missing finalized extraction artifact: ' + str(other))
            files[str(other)] = digest(other)
    hashes = source_hashes(books)
    freeze_run(out, {'kind':'clean_preparation','books':books,'source_hashes':hashes,'extraction_files':files,'code':code_hashes()})
    if (out/'INPUT.json').exists():
        prepared = read(out/'PREPARED.json')
        if prepared.get('input_sha256') != digest(out/'INPUT.json'):
            raise ValueError('Verified cleaning snapshot changed or lacks a digest; use a new output directory')
        return read(out/'INPUT.json')
    import clean_boundary_v46 as v46
    import fullbook_v5_structure as patch
    rows, ids, seen_books, excluded = [], set(), set(), []
    for file in accepted:
        summary = read(file.parent/'SUMMARY.json')
        bid = summary['identifier']
        if bid not in by_id:
            raise ValueError('Extraction contains a book outside this manifest: ' + bid)
        if bid in seen_books:
            raise ValueError('Duplicate finalized extraction book: ' + bid)
        if summary['status'] not in ('completed','partial'):
            raise ValueError('Only finalized completed/partial books can enter cleaning')
        seen_books.add(bid)
        book = by_id[bid]
        md = Path(book['md_path'])
        text = md.read_bytes().decode('utf-8-sig')
        units = read(file.parent/'units.json')
        lines = text.splitlines(keepends=True)
        starts = [0]
        for line in lines: starts.append(starts[-1]+len(line))
        def point(pos):
            index = max(0,min(len(lines)-1,bisect.bisect_right(starts,pos)-1))
            return index+1,pos-starts[index]
        def ctx(numbers):
            return [{'line':n,'text':lines[n-1].rstrip('\r\n')} for n in sorted(set(numbers)) if 1 <= n <= len(lines)]
        scope = read(book['scope_config'])
        for entry in read(file):
            row = copy.deepcopy(entry)
            safe_id(row['id'])
            if row['id'] in ids:
                raise ValueError('Duplicate extracted record ID')
            ids.add(row['id'])
            if row.get('eligible_for_name_screening') is not True:
                excluded.append({'id':row['id'],'reason':'not_eligible','book':bid})
                continue
            source = row['source']
            if source['identifier'] != bid or Path(source['md_path']).resolve() != md.resolve() or source['md_sha256'] != digest(md):
                raise ValueError('Extraction source identity/hash mismatch')
            for key in ('head_spans','body_spans'):
                spans = source[key]
                if (not isinstance(spans,list) or any(len(v)!=2 or type(v[0]) is not int or type(v[1]) is not int
                    or not 0 <= v[0] < v[1] <= len(text) for v in spans)):
                    raise ValueError('Invalid original source spans')
                if any(a[1]>b[0] for a,b in zip(spans,spans[1:])):
                    raise ValueError('Unordered original source spans')
            if not source['head_spans']:
                raise ValueError('Missing original head span')
            if '\n\n'.join(text[a:b] for a,b in source['body_spans']) != row['raw_content']:
                raise ValueError('Body differs from original source spans')
            patch.repair_split_name(row,units,text)
            source = row['source']
            if '\n\n'.join(text[a:b] for a,b in source['body_spans']) != row['raw_content']:
                raise ValueError('Name-field adjustment broke original source spans')
            head_line = point(source['head_spans'][0][0])[0]
            locs, edges = [], []
            for a,b in source['body_spans']:
                la,ca = point(a)
                lb,cb = point(b)
                locs.append({'start_line':la,'start_column':ca,'end_line':lb,'end_column':cb})
                edges.extend(range(la-2,la+3))
                edges.extend(range(lb-2,lb+3))
            row.update(subject_slug=book['subject_slug'],scope=scope,explanation=row['raw_content'][:600],source_type='book')
            row['source'] = {**source,'book_ref':bid,'book_title':book['title'],'record_id':row['id'],
                             'head_line':head_line,'body_locations':locs,'extraction_book_status':summary['status']}
            row['source_context'] = {'head_context':ctx(range(head_line-8,head_line+4)),
                                     'body_edges':ctx(edges),'structure_evidence':[],
                                     'leading_context':row.get('leading_context',''),
                                     'trailing_context':row.get('trailing_context','')}
            rows.append(v46.enrich_source(row,text,units))
        excluded.extend({'id':e.get('id'),'book':bid,'reason':'structural_quarantine'}
                        for e in read(file.parent/'quarantined_entries.json'))
    if seen_books != set(by_id):
        raise ValueError('Manifest books missing finalized extraction: ' + ','.join(sorted(set(by_id)-seen_books)))
    if source_hashes(books) != hashes or any(digest(file)!=h for file,h in files.items()):
        raise ValueError('Source or extraction outputs changed during freezing')
    write(out/'INPUT.json',rows)
    write(out/'EXCLUDED.json',excluded)
    write(out/'PREPARED.json',{'books':len(seen_books),'records':len(rows),'excluded':len(excluded),
                             'input_sha256':digest(out/'INPUT.json'),
                             'empty_body':sum(not r['raw_content'].strip() for r in rows),
                             'partial_book_records':sum(r['source']['extraction_book_status']=='partial' for r in rows)})
    return rows


def is_passed(vote):
    return vote.get('decision') == 'keep' and vote.get('api_status') in ('ok','local')


def filter_stage(rows, stage, cfg, out, scope_path=None):
    folder = Path(out)
    annotation = NAME if stage=='name' else SCOPE
    stem = 'llm_name_title_format' if stage=='name' else 'subject_scope'
    script = ROOT/'name_filters'/('01_name_title_quality.py' if stage=='name' else '02_subject_scope_filter.py')
    if stage=='scope':
        spec = importlib.util.spec_from_file_location('scope_preflight',script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load_scope_config(Path(scope_path))
    staged = bool(cfg.get('staged_round'))
    compensating = bool(cfg.get('compensation_mode'))
    group = folder.relative_to(Path(cfg['compensation_cache'])).as_posix() if compensating else None
    first_by_id, by_id = {}, {}
    if (folder/'FINAL.jsonl').exists() and not staged:
        saved = loadl(folder/'FINAL.jsonl')
        if len(saved)!=len(rows) or {r['id'] for r in saved}!={r['id'] for r in rows}:
            raise ValueError('Cached filter ID set differs')
        return saved
    def invoke(values, sub, context):
        target = folder/sub
        if cfg.get('resume_current_round') and (target/'input.jsonl').exists():
            from resume_integrity import filter_journal_records
            cached, pending = filter_journal_records(target, script, values, stem, scope_path)
            if cached or (target/(stem+'_judgments.jsonl')).exists():
                revised = invoke(pending, sub+'/continuation', context) if pending else []
                cached.update({row['id']:row for row in revised})
                return [cached[row['id']] for row in values]
        deferred = {}
        if compensating:
            from resume_integrity import claim_filter_compensation
            phase = 'context' if context else 'initial'
            permitted = []
            for value in values:
                saved = (by_id if context else first_by_id).get(value['id'], value)
                if claim_filter_compensation(cfg, stage, group, saved, phase):
                    permitted.append(value)
                else:
                    deferred[value['id']] = saved
            values = permitted
            if not values:
                return list(deferred.values())
        jsonl(target/'input.jsonl',values)
        cmd = [sys.executable,str(script),'--input',str(target/'input.jsonl'),'--out-dir',str(target),
               '--api-url',cfg['api_url'].rstrip('/')+'/v1/chat/completions','--model',cfg['model'],
               '--no-auth','--disable-thinking','--workers',str(cfg['workers']),'--batch-size','8',
               '--retries','0' if staged else '1','--timeout','240','--context-chars',str(context),'--resume']
        if stage=='name': cmd += ['--retry-review-rounds','0']
        else: cmd += ['--scope-config',str(scope_path),'--retry-error-rounds','0']
        with (target/'RUN.log').open('a',encoding='utf-8') as log:
            subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
        result = loadl(target/(stem+'_all_full.jsonl'))
        if len(result)!=len(values) or {r['id'] for r in result}!={r['id'] for r in values}:
            raise ValueError('Model filter returned wrong record ID set')
        result.extend(deferred.values())
        return result
    if not rows:
        jsonl(folder/'FINAL.jsonl',[])
        return []
    started = time.monotonic()
    if staged and (folder/'FINAL.jsonl').exists():
        old = loadl(folder/'FINAL.jsonl')
        first_path = folder/'FIRST.jsonl'
        if not first_path.exists():
            raise ValueError('Missing first-judgment checkpoint')
        first = loadl(first_path)
        wanted = {r['id'] for r in rows}
        if (len({r['id'] for r in old}) != len(old) or not {r['id'] for r in old} <= wanted
                or len({r['id'] for r in first}) != len(first)
                or {r['id'] for r in first} != {r['id'] for r in old}):
            raise ValueError('Cached filter ID set differs')
        by_id = {r['id']:r for r in old}
        first_by_id = {r['id']:r for r in first}
        if any(saved[r['id']].get('source') != r.get('source') or
               saved[r['id']].get('raw_content') != r.get('raw_content')
               for saved in (by_id,first_by_id) for r in rows if r['id'] in saved):
            raise ValueError('Cached filter provenance differs')
        def retryable(value, phase):
            vote = value[annotation]
            healthy = vote.get('api_status') not in ('ok','local') and vote.get('failure_kind')=='transport'
            if compensating and healthy:
                from resume_integrity import compensation_filter_eligible
                return compensation_filter_eligible(cfg, stage, group, value['id'], phase)
            return healthy
        pending_first = [r for r in rows if r['id'] not in first_by_id or retryable(first_by_id[r['id']], 'initial')]
        pending_first_ids = {r['id'] for r in pending_first}
        if pending_first:
            revised = invoke(pending_first,'compensation_'+cfg['compensation_token']['sha256'][:16]+'/initial'
                             if compensating else 'round_'+str(cfg['staged_round']),0)
            first_by_id.update({r['id']:r for r in revised})
            by_id.update({r['id']:r for r in revised})
        review = [first_by_id[r['id']] for r in rows
                  if first_by_id[r['id']][annotation].get('decision')=='review'
                  and first_by_id[r['id']][annotation].get('api_status') in ('ok','local')
                  and (r['id'] not in by_id or retryable(by_id[r['id']], 'context')
                       or r['id'] in pending_first_ids)]
        context = invoke(review,'compensation_'+cfg['compensation_token']['sha256'][:16]+'/context'
                         if compensating else 'round_'+str(cfg['staged_round'])+'_review_context',600) if review else []
        by_id.update({r['id']:r for r in context})
        jsonl(first_path,[first_by_id[r['id']] for r in rows])
        final = [by_id[r['id']] for r in rows]
        jsonl(folder/'FINAL.jsonl',final)
        write(folder/'SUMMARY.json',{'input':len(rows),'review_with_context':len(review),
              'decisions':dict(collections.Counter(r[annotation]['decision'] for r in final)),
              'api_status':dict(collections.Counter(r[annotation]['api_status'] for r in final)),
              'elapsed_seconds':time.monotonic()-started})
        return final
    initial = invoke(rows,'initial',0)
    if staged:
        jsonl(folder/'FIRST.jsonl',initial)
    review = [r for r in initial if r[annotation]['decision']=='review' and r[annotation]['api_status'] in ('ok','local')]
    revised = {r['id']:r for r in invoke(review,'review_context',600)} if review else {}
    final = [revised.get(r['id'],r) for r in initial]
    jsonl(folder/'FINAL.jsonl',final)
    write(folder/'SUMMARY.json',{'input':len(rows),'review_with_context':len(review),
          'decisions':dict(collections.Counter(r[annotation]['decision'] for r in final)),
          'api_status':dict(collections.Counter(r[annotation]['api_status'] for r in final)),
          'elapsed_seconds':time.monotonic()-started})
    return final


def run_cleaning(input_path, cfg, out, books):
    if cfg.get('no_auth',True) is not True:
        raise ValueError('Existing v46 HTTP layer supports no-auth endpoints only')
    cfg = {**cfg,'workers':int(cfg.get('workers',1024)),'api_url':cfg['api_url'].rstrip('/')}
    if cfg['workers']<1 or cfg['api_url'].endswith('/v1') or not cfg.get('model'):
        raise ValueError('Use service root api_url, explicit model ID and positive workers')
    rows, out = read(input_path),Path(out)
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate cleaning IDs')
    hashes = source_hashes(books)
    freeze_run(out,{'kind':'cleaning','input_sha256':digest(input_path),'config':cfg,'code':code_hashes(),'sources':hashes})
    with urllib.request.urlopen(cfg['api_url']+'/v1/models',timeout=30) as response:
        models = json.load(response)
    if cfg['model'] not in {m['id'] for m in models['data']}:
        raise ValueError('Requested model ID is not advertised by service')
    from fullbook_llm_v2 import book_lock
    import clean_boundary_v46 as content
    scope_map = {b['identifier']:Path(b['scope_config']) for b in books}
    started = time.monotonic()
    with book_lock(out):
        timings = {}
        def state(stage):
            write(out/'STATE.json',{'status':'running','stage':stage})
        state('v22_name')
        t = time.monotonic()
        names = filter_stage(rows,'name',cfg,out/'name')
        timings['name'] = time.monotonic()-t
        kept = [r for r in names if is_passed(r[NAME])]
        state('v9_scope')
        t = time.monotonic()
        scoped = []
        groups = collections.defaultdict(list)
        for row in kept: groups[str(scope_map[row['source']['identifier']])].append(row)
        for index,(scope_path,group) in enumerate(sorted(groups.items())):
            scoped.extend(filter_stage(group,'scope',cfg,out/'scope'/f'group_{index:03d}',scope_path))
        timings['scope'] = time.monotonic()-t
        passed = [r for r in scoped if is_passed(r[SCOPE])]
        for row in passed:
            row['head'] = ' '.join(x for x in (row.get('knowledge_point'),row.get('name')) if x)
        state('v46_content_definition_alignment')
        t = time.monotonic()
        content_out = out/'content'
        content_out.mkdir(exist_ok=True)
        reports = content.Runner(content_out,cfg).batch(passed,'results',content.Runner(content_out,cfg).content)
        timings['content_definition_alignment'] = time.monotonic()-t
        final = [r['record'] for r in reports if r['status']=='keep']
        jsonl(out/'FINAL_RECORDS.jsonl',final)
        write(out/'STANDARD_RECORDS.json',[{key:r.get(key,'') for key in STANDARD} for r in final])
        nm = {r['id']:r[NAME] for r in names}
        sm = {r['id']:r[SCOPE] for r in scoped}
        cm = {r['id']:r for r in reports}
        ledger = [{'id':r['id'],'name':nm[r['id']],'scope':sm.get(r['id']),
                   'content_status':cm.get(r['id'],{}).get('status','not_reached')} for r in rows]
        jsonl(out/'DISPOSITIONS.jsonl',ledger)
        technical = [r for r in ledger if r['name'].get('api_status') not in ('ok','local')
                     or (r['scope'] and r['scope'].get('api_status') not in ('ok','local')) or r['content_status']=='failed']
        review = [r for r in ledger if r['name']['decision']=='review'
                  or (r['scope'] and r['scope']['decision']=='review') or r['content_status']=='review']
        jsonl(out/'TECHNICAL_FAILURES.jsonl',technical)
        jsonl(out/'REVIEW.jsonl',review)
        if source_hashes(books)!=hashes:
            raise ValueError('Sources changed during cleaning; results cannot be finalized')
        summary = {'input':len(rows),'name_keep':len(kept),'scope_keep':len(passed),'final_keep':len(final),
                   'content_statuses':dict(collections.Counter(r['status'] for r in reports)),
                   'name_only':sum(not any(r.get(k) for k in STANDARD[3:7]) for r in final),
                   'technical_failures':len(technical),'review':len(review),'stage_seconds':timings,
                   'elapsed_seconds':time.monotonic()-started,'semantic_quality_approved':False,
                   'source_hashes_verified':True,'workers_configured':cfg['workers']}
        write(out/'SUMMARY.json',summary)
        write(out/'STATE.json',{'status':'finished_with_pending' if technical or review else 'finished',**summary})
        return summary


def staged_manifest(input_path, cfg, books):
    """Freeze semantic identity while permitting a per-round worker limit."""
    semantic = {key:value for key,value in cfg.items() if key not in ('workers','staged_round')}
    semantic['api_url'] = semantic['api_url'].rstrip('/')
    return {'kind':'cleaning_staged','input_sha256':digest(input_path),
            'integrity_code_sha256':digest(ROOT.parents[1]/'round_integrity.py'),
            'config':semantic,'code':code_hashes(),'sources':source_hashes(books)}


def staged_checkpoint_hashes(out, *, force_full=False):
    from concurrent.futures import ThreadPoolExecutor
    from itertools import islice

    out = Path(out)
    volatile = {'STATE.json','ORCHESTRATION_STATUS.json','PROGRESS.json','.running'}
    paths = (path for path in sorted(out.rglob('*'))
             if path.name not in volatile and path.suffix not in ('.log','.tmp','.lock'))
    def inspect(path):
        if path.is_file():
            return path.relative_to(out).as_posix(), digest_file(path, force_full=force_full)
    hashes = {}
    with ThreadPoolExecutor(max_workers=32) as pool:
        while True:
            batch = list(islice(paths, 128))
            if not batch:
                break
            for value in pool.map(inspect, batch):
                if value is not None:
                    hashes[value[0]] = value[1]
    return hashes


def verify_staged_checkpoint(out, status, *, force_full=False):
    hashes = status.get('checkpoint_hashes')
    if not isinstance(hashes,dict) or hashes != staged_checkpoint_hashes(out, force_full=force_full):
        raise ValueError('Dictionary cleaning checkpoint changed after round')


def validate_staged_run(input_path, cfg, out, books, create=False):
    if cfg.get('no_auth',True) is not True or int(cfg.get('workers',0)) < 1:
        raise ValueError('Staged dictionary cleaning requires no-auth and positive workers')
    if not cfg.get('model') or cfg['api_url'].rstrip('/').endswith('/v1'):
        raise ValueError('Use service root api_url and explicit model ID')
    source = read(input_path)
    if not isinstance(source,list) or len({r['id'] for r in source}) != len(source):
        raise ValueError('Duplicate or invalid cleaning IDs')
    manifest = staged_manifest(input_path,cfg,books)
    if not create and not (Path(out)/'MANIFEST.json').exists():
        raise ValueError('Missing staged cleaning manifest')
    freeze_run(out,manifest)
    return source,manifest


def run_cleaning_round(input_path, cfg, out, books, round_number, resume_receipt=None):
    """Advance a whole cleaning run by one transport retry cohort."""
    if round_number not in (1,2,3):
        raise ValueError('Dictionary cleaning round must be 1, 2 or 3')
    out = Path(out)
    rows,manifest = validate_staged_run(input_path,cfg,out,books,create=round_number==1)
    previous = read(out/'ORCHESTRATION_STATUS.json') if (out/'ORCHESTRATION_STATUS.json').exists() else None
    native_state = read(out/'STATE.json') if (out/'STATE.json').exists() else None
    resuming = native_state and native_state.get('status') == 'round_running'
    if resuming:
        if resume_receipt is None:
            raise ValueError('Interrupted dictionary round needs an offline inspection receipt before retry')
        import resume_integrity
        resume_integrity.verify_resume(out, resume_receipt, input_path, cfg, books,
                                       sys.modules[__name__], round_number)
    elif resume_receipt is not None:
        import resume_integrity
        resume_integrity.verify_resume(out, resume_receipt, input_path, cfg, books,
                                       sys.modules[__name__], round_number)
    if round_number == 1:
        if previous is not None:
            raise ValueError('Dictionary cleaning round is out of order')
    elif previous is None or previous.get('state') != 'awaiting_retry' or previous.get('next_round') != round_number:
        raise ValueError('Dictionary cleaning round is out of order')
    if previous is not None and not resuming:
        verify_staged_checkpoint(out,previous)
    cfg = {**cfg,'api_url':cfg['api_url'].rstrip('/'),'workers':int(cfg['workers']),
           'staged_round':round_number}
    # Only round execution may contact the service. Finalization reads checkpoints exclusively.
    import resume_integrity
    lock_receipt = Path(resume_receipt) if resume_receipt is not None else out.parent / (out.name + '_RESUME_RECEIPT.json')
    if lock_receipt.exists():
        resume_integrity.verify_writer_idle(out, lock_receipt, clear_stale=True)
    resume_integrity.models_preflight(cfg, out.parent / (out.name + '_SERVICE_PAUSE.json'),
                                     round_number, open_fn=urllib.request.urlopen)
    if resuming:
        cfg['resume_current_round'] = True
    import clean_boundary_v46 as content
    scope_map = {b['identifier']:Path(b['scope_config']) for b in books}
    started = time.monotonic()
    if lock_receipt.exists():
        lock_context = resume_integrity.resumed_book_lock(out, lock_receipt)
    else:
        from fullbook_llm_v2 import book_lock
        lock_context = book_lock(out)
    with lock_context:
        round_baseline = native_state.get('round_baseline') if resuming else None
        if not resuming and previous is not None and lock_receipt.exists():
            round_baseline = resume_integrity.capture_round_baseline(out, round_number, previous, lock_receipt)
        running = {'status':'round_running','round':round_number}
        if round_baseline:
            running['round_baseline'] = round_baseline
        write(out/'STATE.json',running)
        names = filter_stage(rows,'name',cfg,out/'name')
        kept = [r for r in names if is_passed(r[NAME])]
        scoped = []
        groups = collections.defaultdict(list)
        for row in kept:
            groups[str(scope_map[row['source']['identifier']])].append(row)
        for scope_path,group in sorted(groups.items()):
            group_key = hashlib.sha256(scope_path.encode()).hexdigest()[:12]
            scoped.extend(filter_stage(group,'scope',cfg,out/'scope'/('group_'+group_key),scope_path))
        passed = [r for r in scoped if is_passed(r[SCOPE])]
        for row in passed:
            row['head'] = ' '.join(x for x in (row.get('knowledge_point'),row.get('name')) if x)
        content_out = out/'content'
        content_out.mkdir(exist_ok=True)
        runner = content.Runner(content_out,cfg)
        reports = runner.batch(passed,'results',runner.content)
        final = [r['record'] for r in reports if r['status']=='keep']
        jsonl(out/'FINAL_RECORDS.jsonl',final)
        write(out/'STANDARD_RECORDS.json',[{key:r.get(key,'') for key in STANDARD} for r in final])
        nm,sm,cm = ({r['id']:r[NAME] for r in names},
                    {r['id']:r[SCOPE] for r in scoped},
                    {r['id']:r for r in reports})
        def content_failure_kind(sid):
            if cm.get(sid,{}).get('status')!='failed':
                return None
            calls = list((content_out/'calls').glob('*/'+sid+'.json'))
            return 'transport' if any(read(path).get('failure_kind')=='transport' for path in calls) else 'other'
        ledger = [{'id':r['id'],'name':nm[r['id']],'scope':sm.get(r['id']),
                   'content_status':cm.get(r['id'],{}).get('status','not_reached'),
                   'content_failure_kind':content_failure_kind(r['id'])} for r in rows]
        jsonl(out/'DISPOSITIONS.jsonl',ledger)
        technical = [r for r in ledger if r['name'].get('api_status') not in ('ok','local')
                     or (r['scope'] and r['scope'].get('api_status') not in ('ok','local'))
                     or r['content_status']=='failed']
        retryable = [r for r in technical if r['name'].get('failure_kind')=='transport'
                     or (r['scope'] and r['scope'].get('failure_kind')=='transport')
                     or r['content_failure_kind']=='transport']
        review = [r for r in ledger if r['name']['decision']=='review'
                  or (r['scope'] and r['scope']['decision']=='review') or r['content_status']=='review']
        jsonl(out/'TECHNICAL_FAILURES.jsonl',technical)
        jsonl(out/'REVIEW.jsonl',review)
        if source_hashes(books) != manifest['sources']:
            raise ValueError('Sources changed during cleaning; results cannot be finalized')
        summary = {'input':len(rows),'name_keep':len(kept),'scope_keep':len(passed),
                   'final_keep':len(final),'technical_failures':len(technical),'review':len(review),
                   'content_statuses':dict(collections.Counter(r['status'] for r in reports)),
                   'name_only':sum(not any(r.get(k) for k in STANDARD[3:7]) for r in final),
                   'semantic_quality_approved':False,'source_hashes_verified':True,
                   'workers_configured':cfg['workers'],'round':round_number,
                   'elapsed_seconds':time.monotonic()-started}
        write(out/'SUMMARY.json',summary)
        state = ('awaiting_retry' if retryable and round_number < 3 else
                 'technical_failure' if technical else 'ready_to_finalize')
        status = {'state':state,'status':state,'round':round_number,
                  'next_round':round_number+1 if state=='awaiting_retry' else None,
                  'technical_failures':len(technical),'retryable_technical_failures':len(retryable),
                  'review':len(review),
                  'input':len(rows),'final_keep':len(final)}
        status['checkpoint_hashes'] = staged_checkpoint_hashes(out)
        write(out/'ORCHESTRATION_STATUS.json',status)
        write(out/'STATE.json',{'status':'round_complete','round':round_number,**summary})
        return status


def run_compensation(input_path, cfg, out, books, plan_path, plan_sha256):
    """Execute the separately sealed lost-opportunity plan after regular rounds."""
    from compensation_recovery import run
    return run(input_path, cfg, out, books, plan_path, plan_sha256, sys.modules[__name__])


@full_verification
def finalize_cleaning(input_path, cfg, out, books):
    """Seal native checkpoints after the last cohort without an API/preflight call."""
    out = Path(out)
    rows,_ = validate_staged_run(input_path,cfg,out,books)
    status = read(out/'ORCHESTRATION_STATUS.json')
    verify_staged_checkpoint(out,status,force_full=True)
    if status.get('state') == 'finalized':
        if read(out/'STATE.json').get('status') not in ('finished','finished_with_pending'):
            raise ValueError('Finalized dictionary checkpoint is inconsistent')
        return read(out/'SUMMARY.json')
    if status.get('state') not in ('ready_to_finalize','technical_failure'):
        raise ValueError('Dictionary cleaning still awaits retry')
    ledger = loadl(out/'DISPOSITIONS.jsonl')
    if len(ledger)!=len(rows) or [r['id'] for r in ledger]!=[r['id'] for r in rows]:
        raise ValueError('Dictionary cleaning disposition order or coverage changed')
    summary = read(out/'SUMMARY.json')
    if summary.get('input')!=len(rows) or summary.get('technical_failures')!=len(loadl(out/'TECHNICAL_FAILURES.jsonl')):
        raise ValueError('Dictionary cleaning summary differs from checkpoint')
    resume_receipt=out.parent/(out.name+'_RESUME_RECEIPT.json')
    if resume_receipt.exists():
        import resume_integrity
        resume_integrity.verify_retained_passes(out,resume_receipt)
    write(out/'STATE.json',{'status':'finished_with_pending' if summary['technical_failures'] or summary['review'] else 'finished',
                            **summary})
    write(out/'ORCHESTRATION_STATUS.json',{**status,'outcome':status['state'],
                                           'state':'finalized','status':'finalized'})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--books',type=Path,required=True)
    prep.add_argument('--out',type=Path,required=True)
    cleanprep = commands.add_parser('clean-prepare')
    cleanprep.add_argument('--books',type=Path,required=True)
    cleanprep.add_argument('--extraction',type=Path,required=True)
    cleanprep.add_argument('--out',type=Path,required=True)
    clean = commands.add_parser('clean')
    clean.add_argument('--books',type=Path,required=True)
    clean.add_argument('--input',type=Path,required=True)
    clean.add_argument('--config',type=Path,required=True)
    clean.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    if args.command=='prepare': result = {'prepared_books':len(prepare_sources(read(args.books),args.out))}
    elif args.command=='clean-prepare': result = {'prepared_records':len(prepare_cleaning(read(args.books),args.extraction,args.out))}
    else: result = run_cleaning(args.input,read(args.config),args.out,validate_books(read(args.books)))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
