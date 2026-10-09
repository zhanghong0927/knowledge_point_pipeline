"""Structure-aware, source-preserving whole-book LLM extraction."""
import argparse
from bisect import bisect_right
from collections import deque
import concurrent.futures
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import time
import threading
import uuid

from markdown_it import MarkdownIt
import fullbook_llm_extract as legacy
from model_range_crop import read, write

VERSION = 'fullbook-structured-v2'
PROMPT = legacy.PROMPT + '''
Each unit is an intact Markdown structure with kind and heading_path metadata.
Heading paths are context, not additional named entries. A table/code/formula is
not automatically a headword. name_evidence and body_evidence are separate concepts:
head selections establish the original name; body selections must independently
support its explanation. A name alone never proves a definition. Do not transform
contents/index references into a main entry or borrow their nearby prose.
Do not summarize across the book or merge different occurrences of a name.
Select only explicit entry heads and their original explanatory spans.
Pagination may repeat a title INSIDE a continuing sentence. That is a running
header, NOT a new entry, even if Markdown marks it as a heading. Never use a later
repeated header to label preceding body. If the genuine opening head is absent
from the supplied source, omit that incomplete entry and continue with later ones.
'''


def validate_checked(data,part,text,book):
    for index,item in enumerate(data.get('entries',[])):
        heads=item.get('head',[])
        bodies=item.get('body',[])
        if heads and bodies and bodies[0]['start']['unit'] < heads[-1]['unit']:
            raise ValueError(f'Entry {index}: head unit {heads[-1]["unit"]} is AFTER body '
                             f'start unit {bodies[0]["start"]["unit"]}. Do not use a running '
                             'header as an opening head. Omit this entry if its genuine head '
                             'is absent; retain other valid entries.')
    return legacy.validate(data,part,text,book)


def structural_units(text):
    """Preserve every character while respecting top-level Markdown structures."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1]+len(line))
    tokens = MarkdownIt('commonmark', {'html':True}).enable('table').parse(text)
    protected = [t.map for t in tokens if t.type in ('fence','code_block') and t.map]
    math_ranges = []
    math_start = None
    for index, line in enumerate(lines):
        if any(a <= index < b for a,b in protected):
            continue
        value = line.strip()
        if math_start is None and value == '$$':
            math_start = index
        elif math_start is not None and value == '$$':
            math_ranges.append((offsets[math_start],offsets[index+1],'math_block'))
            math_start = None
    ranges = [(offsets[t.map[0]],offsets[t.map[1]],t.type.removesuffix('_open'))
              for t in tokens if t.level==0 and t.map is not None and t.nesting!=-1]
    ranges = [r for r in ranges if not any(a <= r[0] < b for a,b,_ in math_ranges)] + math_ranges
    units, cursor, headings = [], 0, []

    def append(start, end, kind):
        if end <= start:
            return
        value = text[start:end]
        if value.strip().startswith('$$') and value.strip().endswith('$$'):
            kind = 'math_block'
        units.append({'unit':len(units),'offset':start,'line':bisect_right(offsets,start),
                      'text':value,'kind':kind,'heading_path':list(headings)})

    for start,end,kind in sorted(ranges):
        if start < cursor:
            continue
        append(cursor,start,'gap')
        if kind == 'heading':
            heading_token = next(t for t in tokens if t.type=='heading_open' and offsets[t.map[0]]==start)
            level = int(heading_token.tag[1:])
            headings[:] = headings[:level-1]
            headings.append(text[start:end].strip())
        append(start,end,kind)
        cursor = end
    append(cursor,len(text),'gap')
    if ''.join(row['text'] for row in units) != text:
        raise ValueError('Lossless source coverage failed')
    return units


def attach_format(units, evidence, md_hash, text):
    if not evidence:
        return
    lines = [{'line':i+1} for i,_ in enumerate(text.splitlines(keepends=True))]
    legacy.add_format(lines,evidence,md_hash)
    for unit in units:
        last = unit['line'] + len(unit['text'].splitlines(keepends=True)) - 1
        annotations = []
        seen = set()
        for row in lines[unit['line']-1:last]:
            for annotation in row.get('pdf_format',[]):
                key = legacy.encode(annotation)
                if key not in seen:
                    annotations.append({'source_line':row['line'],**annotation})
                    seen.add(key)
        if annotations:
            unit['pdf_format'] = annotations


def mark_conflicts(entries):
    """Detect every selected-span collision, including long non-adjacent ranges."""
    spans = sorted((a,b,i) for i,e in enumerate(entries)
                   for a,b in e['source']['head_spans']+e['source']['body_spans'])
    active = []
    for start,end,index in spans:
        active = [(b,i) for b,i in active if b>start]
        for _,other in active:
            if other != index:
                entries[index]['structural_review_required'] = True
                entries[other]['structural_review_required'] = True
        active.append((end,index))
    for entry in entries:
        entry['eligible_for_name_screening'] = not entry.get('structural_review_required',False)


@contextmanager
def book_lock(folder):
    lock = folder/'.running'
    fd = os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump({'pid':os.getpid(),'host':os.uname().nodename if hasattr(os,'uname') else os.environ.get('COMPUTERNAME'),
                       'started':time.time()},stream)
        yield
    finally:
        lock.unlink()


def dispatch_ranges(process, ranges, executor, limit):
    """Coordinator expands split ranges; workers never wait on child futures."""
    queue = deque(ranges)
    pending = set()
    try:
        while queue or pending:
            while queue and len(pending) < limit:
                pending.add(executor.submit(process, *queue.popleft()))
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                queue.extend(future.result() or [])
    except BaseException:
        for future in pending:
            future.cancel()
        # Keep book ownership until all already-running writers have stopped.
        concurrent.futures.wait(pending)
        raise


class TelemetryError(RuntimeError):
    """A local persistence fault must not be retried as a model request."""


class Runner(legacy.Runner):
    def __init__(self,args):
        super().__init__(args)
        self.workers = getattr(args,'workers',1024)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
        self.request_lock = threading.Lock()
        self.active_requests = 0
        self.peak_requests = 0

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)

    def call_model(self,payload):
        with self.request_lock:
            self.active_requests += 1
            self.peak_requests = max(self.peak_requests,self.active_requests)
            self.report_concurrency()
        try:
            return self.api('/chat/completions',payload)
        finally:
            with self.request_lock:
                self.active_requests -= 1
                self.report_concurrency()

    def report_concurrency(self):
        try:
            write(self.args.out/'CONCURRENCY.json',{
                'configured_request_limit':self.workers,'active_requests':self.active_requests,
                'peak_active_requests_this_process':self.peak_requests,'updated_at':time.time()})
        except OSError as exc:
            raise TelemetryError('Concurrency telemetry write failed') from exc

    def fits(self, part):
        messages = [{'role':'system','content':PROMPT},{'role':'user','content':legacy.encode(part)}]
        if self.tokenizer:
            count = len(self.tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True))
        else:
            count = sum(len(m['content'].encode()) for m in messages)
        # Includes room for a short validation-error correction on the single retry.
        return count+self.args.output_tokens+4096 <= self.args.context

    def run_book(self,spec):
        folder = self.args.out/legacy.digest(spec['identifier'].encode())[:16]
        folder.mkdir(parents=True,exist_ok=True)
        with book_lock(folder):
            return self._run_book(spec,folder)

    def _run_book(self,spec,folder):
        started = time.monotonic()
        raw = Path(spec['md_path']).read_bytes()
        text = raw.decode('utf-8-sig')
        book = {k:spec[k] for k in ('identifier','title','md_path')}
        book['md_sha256'] = legacy.digest(raw)
        units = structural_units(text)
        attach_format(units,spec.get('pdf_evidence_path'),book['md_sha256'],text)
        code_hashes = {p.name:legacy.digest(p.read_bytes()) for p in
                       [Path(__file__),Path(legacy.__file__),Path(__file__).with_name('model_range_crop.py')]}
        identity = {'version':VERSION,'book':book,'code':code_hashes,
                    'prompt':legacy.digest(PROMPT.encode()),'model':self.args.model,'api_url':self.base,
                    'context':self.args.context,'output_tokens':self.args.output_tokens,
                    'overlap':self.args.overlap,'tokenizer':self.args.tokenizer,
                    'parser_version':importlib.metadata.version('markdown-it-py'),
                    'units':legacy.digest(legacy.encode(units).encode())}
        if (folder/'INPUT.json').exists() and read(folder/'INPUT.json') != identity:
            old = read(folder/'INPUT.json')
            allowed = getattr(self.args,'resume_scheduler_sha256',None)
            expected = {**old,'code':{**old.get('code',{}),Path(__file__).name:code_hashes[Path(__file__).name]}}
            if not allowed or old.get('code',{}).get(Path(__file__).name)!=allowed or expected!=identity:
                raise ValueError('Input/code/config changed; use a new run directory')
            write(folder/'SCHEDULER_MIGRATION.json',{'previous_identity':old,'new_identity':identity,
                  'reason':'Explicit scheduling-only migration; all extraction parameters identical'})
        write(folder/'INPUT.json',identity)
        write(folder/'units.json',units)
        execution_id = uuid.uuid4().hex
        execution_dir = folder/'executions'/execution_id
        stats = {'execution_id':execution_id,'requests':0,'cache_hits':0,'raw_response_reuse':0,
                 'request_seconds':0.0,'prompt_tokens':0,'completion_tokens':0,
                 'usage_unknown_requests':0,'technical_errors':0,'validation_errors':0,'splits':0}
        entries, leaves = [], []
        state_lock = threading.Lock()

        def add_stat(key,value=1):
            with state_lock:
                stats[key] += value

        def complete(built,lo,hi):
            with state_lock:
                entries.extend(built)
                leaves.append({'lo':lo,'hi':hi,'status':'completed'})

        def event(kind,**fields):
            write(execution_dir/'events'/f'{time.time_ns()}_{uuid.uuid4().hex}.json',
                  {'kind':kind,'utc':datetime.now(timezone.utc).isoformat(),**fields})

        def process(lo,hi):
            key = f'{lo:08d}_{hi:08d}'
            cache = folder/'chunks'/f'{key}.json'
            part = legacy.packet(units,lo,hi,self.args.overlap)
            # Shrink only supplementary context, never silently discard owned source.
            overlap = self.args.overlap
            while overlap and not self.fits(part):
                overlap //= 2
                part = legacy.packet(units,lo,hi,overlap)
            if cache.exists():
                saved = read(cache)
                if saved['status']=='completed':
                    built = validate_checked(saved['response'],part,text,book)
                    complete(built,lo,hi);add_stat('cache_hits')
                    return
                if saved['status']=='split':
                    mid=saved['mid']
                    return [(lo,mid),(mid,hi)]
                leaves.append({'lo':lo,'hi':hi,'status':saved['status'],'error':saved['error']})
                return
            split = not self.fits(part)
            error = 'Owned structural unit exceeds context budget'
            last_error = ''
            if not split:
                for attempt in range(2):
                    response_path = folder/'responses'/f'{key}_{attempt+1}.json'
                    failure_path = folder/'failures'/f'{key}_{attempt+1}.json'
                    if failure_path.exists():
                        last_error=read(failure_path)['error'];error=last_error
                        continue
                    if response_path.exists():
                        response = read(response_path);add_stat('raw_response_reuse')
                    else:
                        event('request_started',chunk=key,attempt=attempt+1)
                        request_started=time.monotonic();add_stat('requests')
                        messages=[{'role':'system','content':PROMPT},{'role':'user','content':legacy.encode(part)}]
                        if last_error:
                            messages.append({'role':'user','content':'Previous validation failed: '+last_error[:500]+'. Return valid source anchors.'})
                        try:
                            response=self.call_model({'model':self.args.model,'temperature':0,
                                'max_tokens':self.args.output_tokens,'chat_template_kwargs':{'enable_thinking':False},
                                'response_format':{'type':'json_object'},'messages':messages})
                        except TelemetryError:
                            raise
                        except Exception as exc:
                            elapsed=time.monotonic()-request_started
                            add_stat('request_seconds',elapsed);add_stat('technical_errors')
                            add_stat('usage_unknown_requests')
                            error=last_error=f'{type(exc).__name__}: {exc}'
                            write(failure_path,{'error':error})
                            event('request_failed',chunk=key,attempt=attempt+1,seconds=elapsed)
                            continue
                        elapsed=time.monotonic()-request_started;add_stat('request_seconds',elapsed)
                        # Persistence failures must escape, never trigger another model call.
                        write(response_path,response)
                        usage=response.get('usage') or {}
                        for field in ('prompt_tokens','completion_tokens'):
                            if isinstance(usage.get(field),int):add_stat(field,usage[field])
                        if any(not isinstance(usage.get(k),int) for k in ('prompt_tokens','completion_tokens')):
                            add_stat('usage_unknown_requests')
                        event('request_received',chunk=key,attempt=attempt+1,seconds=elapsed,usage=usage)
                    try:
                        choice=response['choices'][0]
                        if choice.get('finish_reason')=='length':
                            split=True;error='Output truncated';break
                        if choice.get('finish_reason')!='stop':raise ValueError('Unfinished response')
                        data=json.loads(choice['message']['content'])
                        if data.get('scanned_all') is False:
                            split=True;error='Model reports incomplete scan';break
                        built=validate_checked(data,part,text,book)
                    except (ValueError,KeyError,TypeError,IndexError) as exc:
                        add_stat('validation_errors')
                        error=last_error=f'{type(exc).__name__}: {exc}'
                        event('validation_failed',chunk=key,attempt=attempt+1,error_type=type(exc).__name__)
                        continue
                    write(cache,{'status':'completed','response':data})
                    complete(built,lo,hi)
                    event('chunk_completed',chunk=key,entries=len(built))
                    return
            if split and hi-lo>1:
                mid=(lo+hi)//2;add_stat('splits')
                write(cache,{'status':'split','mid':mid,'reason':error})
                return [(lo,mid),(mid,hi)]
            else:
                status='oversized_structure' if split else 'technical_failure'
                write(cache,{'status':status,'error':error})
                leaves.append({'lo':lo,'hi':hi,'status':status,'error':error})

        event('book_started',identifier=spec['identifier'])
        if units:dispatch_ranges(process,[(0,len(units))],self.pool,self.workers)
        entries.sort(key=lambda r:r['source']['head_spans'][0][0])
        mark_conflicts(entries)
        for entry in entries:
            entry['name_evidence']=entry['source']['head_spans']
            entry['body_evidence']=entry['source']['body_spans']
            entry['source']['offset_convention']='UTF-8-sig decoded original text, half-open characters'
        ordered=sorted(leaves,key=lambda r:r['lo'])
        cursor=0
        for leaf in ordered:
            if leaf['lo']!=cursor or leaf['hi']<=cursor:raise ValueError('Coverage gap or overlap')
            cursor=leaf['hi']
        if cursor!=len(units):raise ValueError('Coverage tail missing')
        completed=sum(r['hi']-r['lo'] for r in leaves if r['status']=='completed')
        stats['elapsed_seconds']=round(time.monotonic()-started,3)
        summary={'identifier':spec['identifier'],'status':'completed' if completed==len(units) else 'partial',
            'total_units':len(units),'completed_units':completed,'entries':len(entries),'chunks':leaves,
            'empty_body':sum(not r['raw_content'] for r in entries),
            'incomplete_body':sum(not r['body_complete'] for r in entries),
            'structural_review':sum(r.get('structural_review_required',False) for r in entries),
            'pdf_annotated_units':sum(bool(r.get('pdf_format')) for r in units),
            'execution':stats,'semantic_quality_approved':False}
        write(folder/'entries.json',entries)
        write(execution_dir/'SUMMARY.json',stats)
        write(folder/'SUMMARY.json',summary)
        return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--api-url',required=True);p.add_argument('--model',required=True)
    p.add_argument('--context',type=int,default=100000);p.add_argument('--output-tokens',type=int,default=16000)
    p.add_argument('--server-context',type=int);p.add_argument('--tokenizer')
    p.add_argument('--workers',type=int,default=1024);p.add_argument('--overlap',type=int,default=4)
    p.add_argument('--book-workers',type=int,default=2)
    p.add_argument('--resume-scheduler-sha256',help='Explicitly approved old code hash for scheduling-only migration')
    p.add_argument('--timeout',type=int,default=900)
    args=p.parse_args()
    if args.workers<1 or args.book_workers<1 or args.overlap<0 or args.output_tokens<1 or args.context<=args.output_tokens+4096:
        p.error('Invalid budget or worker configuration')
    specs=read(args.manifest)
    if not isinstance(specs,list) or not specs or len({s['identifier'] for s in specs})!=len(specs):
        p.error('Unique nonempty manifest required')
    runner=Runner(args);runner.check_server();args.out.mkdir(parents=True,exist_ok=True)
    summaries=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.book_workers,len(specs))) as pool:
        futures={pool.submit(runner.run_book,s):s for s in specs}
        for future in concurrent.futures.as_completed(futures):
            try:summaries.append(future.result())
            except Exception as exc:
                summaries.append({'identifier':futures[future]['identifier'],'status':'technical_failure',
                                  'error':f'{type(exc).__name__}: {exc}'})
            write(args.out/'SUMMARY.json',{'version':VERSION,'expected_books':len(specs),
                'finished_books':len(summaries),'configured_workers':args.workers,
                'maximum_active_books':min(args.book_workers,len(specs)),
                'peak_active_requests_this_process':runner.peak_requests,'books':summaries})
    runner.close()
    if any(s['status']!='completed' for s in summaries):raise SystemExit(2)


if __name__=='__main__':
    main()
