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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    if (folder/'FINAL.jsonl').exists():
        saved = loadl(folder/'FINAL.jsonl')
        if len(saved)!=len(rows) or {r['id'] for r in saved}!={r['id'] for r in rows}:
            raise ValueError('Cached filter ID set differs')
        return saved
    def invoke(values, sub, context):
        target = folder/sub
        jsonl(target/'input.jsonl',values)
        cmd = [sys.executable,str(script),'--input',str(target/'input.jsonl'),'--out-dir',str(target),
               '--api-url',cfg['api_url'].rstrip('/')+'/v1/chat/completions','--model',cfg['model'],
               '--no-auth','--disable-thinking','--workers',str(cfg['workers']),'--batch-size','8',
               '--retries','1','--timeout','240','--context-chars',str(context),'--resume']
        if stage=='name': cmd += ['--retry-review-rounds','0']
        else: cmd += ['--scope-config',str(scope_path),'--retry-error-rounds','0']
        with (target/'RUN.log').open('a',encoding='utf-8') as log:
            subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
        result = loadl(target/(stem+'_all_full.jsonl'))
        if len(result)!=len(values) or {r['id'] for r in result}!={r['id'] for r in values}:
            raise ValueError('Model filter returned wrong record ID set')
        return result
    if not rows:
        jsonl(folder/'FINAL.jsonl',[])
        return []
    started = time.monotonic()
    initial = invoke(rows,'initial',0)
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
