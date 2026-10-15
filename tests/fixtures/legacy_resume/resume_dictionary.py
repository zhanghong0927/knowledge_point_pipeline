"""Isolated checkpoint continuation. The original attempt remains read-only."""
import concurrent.futures
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'pipeline_staged_30books_16card_20261012_01'
sys.path.insert(0,str(ROOT/'code'))
sys.path.insert(0,str(ROOT/'code/modules/dictionary'))
import staged_benchmark as bench
import portable_pipeline as native
from batch_pipeline import Gateway
from adapters.gateway_control import transition_phase
from stage_scheduling import ResourceProfile,run_simple_phase

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()
def save(p,d):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');q.replace(p)
def now():return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
def state(**kwargs):
    p=ROOT/'STATUS.json';d=read(p) if p.exists() else {}
    save(p,{**d,**kwargs,'updated_at_beijing':now()})

def assert_original_idle():
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            cmd=p.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode('utf-8','replace')
            if str(SOURCE/'code') in cmd:
                raise RuntimeError('Original attempt owns a live process: '+p.name)
        except (FileNotFoundError,ProcessLookupError,PermissionError):pass

def verified_read(p,proof,source):
    key=Path(p).relative_to(source).as_posix();data=Path(p).read_bytes()
    if proof.get(key)!=hashlib.sha256(data).hexdigest():raise ValueError('Source checkpoint changed: '+key)
    return data

def verify_original(source,status):
    expected={k:v for k,v in status['checkpoint_hashes'].items() if Path(k).name!='.running'}
    actual={str(p.relative_to(source)).replace('\\','/') for p in source.rglob('*')
      if p.is_file() and p.name not in {'STATE.json','ORCHESTRATION_STATUS.json','PROGRESS.json','.running'}
      and p.suffix not in {'.log','.tmp','.lock'}}
    if actual!=set(expected):raise ValueError('Stable checkpoint file set changed')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        checks=pool.map(lambda item:(item[0],sha(source/item[0])==item[1]),expected.items())
        bad=[p for p,ok in checks if not ok]
    if bad:raise ValueError('Stable checkpoint changed: '+repr(bad[:8]))
    return expected

def error404_ids(stage,proof,source):
    result=set()
    stem='llm_name_title_format_judgments.jsonl' if stage.name=='name' else 'subject_scope_judgments.jsonl'
    for p in sorted(stage.glob('*/'+stem)):
        data=verified_read(p,proof,source)
        for line in data.decode('utf-8-sig').splitlines():
            d=json.loads(line)
            if d.get('api_status')=='api_error_after_retries' and re.search(r'(?:<HTTPError\s+404:|HTTPError\(code=404\b)',d.get('api_error','')):
                result.add(d['id'])
    return result

def seed_filter(src,dst,annotation,proof,source):
    ids=error404_ids(src,proof,source);changed=0
    for fn in ['FIRST.jsonl','FINAL.jsonl']:
        p=src/fn
        if not p.exists():continue
        data=verified_read(p,proof,source);dst.mkdir(parents=True,exist_ok=True)
        with (dst/fn).open('w',encoding='utf-8') as f:
            for line in data.decode('utf-8-sig').splitlines():
                row=json.loads(line);v=row[annotation]
                if row['id'] in ids and v.get('api_status')=='api_error_after_retries' and v.get('failure_kind')=='content_or_format':
                    v['failure_kind']='transport';changed+=1
                f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return changed

def prepare_book(original,gateway,service):
    cfg=read(original/'pipeline.json');slug=original.parent.name
    home=ROOT/'dictionary/production'/slug/original.name
    out=home/'run/04_cleaning/dictionary';out.mkdir(parents=True)
    source=original/'run/04_cleaning/dictionary'
    input_path=original/'run/03_extraction/verified/INPUT.json'
    books=original/'run/03_extraction/source_prepared/books.json'
    if not input_path.exists() or not books.exists():return None
    api=gateway.job_url('dictionary_resume_'+hashlib.sha256(original.name.encode()).hexdigest()[:24])
    command=[sys.executable,'-B',str(ROOT/'code/adapters/dictionary_cleaning.py'),
      '--input',str(input_path),'--books',str(books),'--out',str(out),'--export',str(home/'run/04_cleaning/export'),
      '--subject',cfg['subject']['name'],'--slug',slug,'--api-url',api,'--model',service['model'],'--context-limit','32768']
    semantic={'api_url':api,'model':service['model'],'workers':64,'context_limit':32768,'no_auth':True}
    prior=source/'ORCHESTRATION_STATUS.json';seeded=False;baseline=0;retagged=0
    if prior.exists():
        old=read(prior);proof=verify_original(source,old)
        if old.get('round')!=1:raise ValueError('Unexpected original completed cleaning round')
        manifest=read(source/'MANIFEST.json')
        if manifest['input_sha256']!=sha(input_path):raise ValueError('Input changed since original cleaning')
        if manifest['config'].get('model')!=service['model'] or manifest['config'].get('context_limit')!=32768:
            raise ValueError('Model/context changed since original cleaning')
        prov=out/'SOURCE_CACHE.json';save(prov,{'source':str(source),'files':proof,'original_manifest':manifest,
          'original_status_sha256':sha(prior),'migration':'read-only source; only evidenced HTTP404 receives transport recovery'})
        semantic.update(recovery_source=str(source),recovery_provenance=str(prov))
        command+=['--recovery-source',str(source),'--recovery-provenance',str(prov)]
        retagged+=seed_filter(source/'name',out/'name',native.NAME,proof,source)
        for group in sorted((source/'scope').glob('group_*')):
            retagged+=seed_filter(group,out/'scope'/group.name,native.SCOPE,proof,source)
        for fn in ['FINAL_RECORDS.jsonl','STANDARD_RECORDS.json','SUMMARY.json','TECHNICAL_FAILURES.jsonl','DISPOSITIONS.jsonl','REVIEW.jsonl']:
            p=source/fn
            if p.exists():(out/fn).write_bytes(verified_read(p,proof,source))
        baseline=read(source/'SUMMARY.json').get('final_keep',0)
        validated_books=native.validate_books(read(books))
        revised_manifest=native.staged_manifest(input_path,semantic,validated_books)
        if revised_manifest['sources']!=manifest['sources']:
            raise ValueError('Book/evidence/scope sources changed since original cleaning')
        save(out/'MANIFEST.json',revised_manifest)
        retry_count=old.get('retryable_technical_failures',0)
        # Evidenced 404 in content cache is detected lazily and verified before reuse.
        content404=set()
        for rel in proof:
            if rel.startswith('content/calls/') and rel.endswith('.json'):
                value=read(source/rel)
                if value.get('status')=='failed' and any(re.search(r'(?:<HTTPError\s+404:|HTTPError\(code=404\b)',str(a.get('error',''))) for a in value.get('attempts',[]) if isinstance(a,dict)):
                    content404.add(Path(rel).stem)
        needs_retry=bool(retry_count or retagged or content404)
        value={k:v for k,v in old.items() if k!='checkpoint_hashes'}
        resumed_state=('awaiting_retry' if needs_retry else
                       'technical_failure' if old.get('technical_failures') else 'ready_to_finalize')
        value.update(state=resumed_state,status=resumed_state,next_round=2 if needs_retry else None)
        save(out/'MIGRATION.json',{'original_source':str(source),'stable_files_verified':len(proof),
          'excluded_volatile_files':['.running'],'original_manifest_sha256':sha(source/'MANIFEST.json'),
          'new_manifest_sha256':sha(out/'MANIFEST.json'),'original_input_sha256':manifest['input_sha256'],
          'original_sources':manifest['sources'],'model_unchanged':True,'context_unchanged':True,
          'original_code':manifest['code'],'new_code':revised_manifest['code'],
          'allowed_identity_changes':['isolated code with operational fixes','loopback gateway URL','read-only cache provenance'],
          'name_scope_404_vote_changes':retagged,'content_404_ids':sorted(content404)})
        value['checkpoint_hashes']=native.staged_checkpoint_hashes(out)
        save(out/'ORCHESTRATION_STATUS.json',value);save(out/'STATE.json',{'status':'round_complete','round':1})
        seeded=True
    cfg=bench.bind_model_routes(cfg,api);cfg['paths']['run']=str(home/'run')
    save(home/'pipeline.json',cfg)
    result={'home':str(home),'config':str(home/'pipeline.json'),'subject':slug,'job_id':'resume_'+original.name,
      'source':str(original),'input':str(input_path),'books':str(books),'out':str(out),'command':command,'seeded':seeded,
      'retained_cleaned_records':baseline,'retagged_votes':retagged}
    save(home/'RECOVERY_INPUT.json',result);return result

def invoke(job,round_number,workers,finalize=False):
    cmd=job['command']+['--workers',str(workers)]+(['--finalize-only'] if finalize else ['--only-round',str(round_number)])
    if not finalize and job.get('resume_receipt'):
        receipt=read(job['resume_receipt'])
        # The native --resume entry verifies the complete cache. Selecting that
        # entry only needs its round and immutable state-receipt binding.
        files=receipt.get('files',{});out=Path(job['out'])
        if receipt.get('resume_round')==round_number and all(
                files.get(name,{}).get('sha256')==sha(out/name)
                for name in ('STATE.json','ORCHESTRATION_STATUS.json')):
            cmd+=['--resume','--resume-receipt',job['resume_receipt']]
    log=Path(job['home'])/('finalize.log' if finalize else 'round_'+str(round_number)+'.log')
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
    with log.open('w') as f:result=subprocess.run(cmd,cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,env=env)
    if finalize:
        records=Path(job['home'])/'run/04_cleaning/export/records.jsonl'
        if result.returncode or not records.exists():return {'status':'failed','returncode':result.returncode}
        if job.get('resume_receipt'):
            import resume_integrity
            proof=resume_integrity.verify_retained_passes(job['out'],job['resume_receipt'])
            return {'status':'completed','returncode':0,'records':proof['current_passed'],
                    'retained_baseline':proof['retained']}
        original=Path(job['source'])/'run/04_cleaning/dictionary/FINAL_RECORDS.jsonl'
        oldids={json.loads(line)['id'] for line in original.open(encoding='utf-8') if line.strip()} if original.exists() else set()
        newids={json.loads(line)['id'] for line in records.open(encoding='utf-8') if line.strip()}
        if not oldids<=newids:raise ValueError('A prior passed record disappeared during resume')
        return {'status':'completed','returncode':0,'records':len(newids),'retained_baseline':len(oldids)}
    p=Path(job['out'])/'ORCHESTRATION_STATUS.json';value=read(p) if p.exists() else {}
    if result.returncode not in (0,2) or value.get('round')!=round_number:
        raise RuntimeError('Cleaning round failed without a valid completed checkpoint: '+job['job_id'])
    return {'status':value.get('state'),'round':round_number,'technical_failures':value.get('technical_failures'),'retryable':value.get('retryable_technical_failures')}

def run():
    import fcntl
    lock=(ROOT/'BACKGROUND.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert_original_idle();bench.verify_code_snapshot(ROOT/'CODE_SNAPSHOT.json')
    original_snapshot=read(SOURCE/'CODE_SNAPSHOT.json')
    for rel,digest in original_snapshot['files'].items():
        if sha(SOURCE/'code'/rel)!=digest:raise ValueError('Original frozen code changed: '+rel)
    service=read(ROOT/'SERVICE_PREFLIGHT.json');profile=ResourceProfile(gpu_cards=16)
    state(state='preparing',pid=os.getpid(),started_at_beijing=now(),original_modified=False,source=str(SOURCE))
    with Gateway(ROOT/'gateway',service['api_url'],limit=512) as gateway:
        state(gateway_url=gateway.api_url)
        originals=[p for p in sorted((SOURCE/'dictionary/production').glob('*/*')) if (p/'run/03_extraction/verified/INPUT.json').exists()]
        fresh_sources=[p for p in originals if not (p/'run/04_cleaning/dictionary/ORCHESTRATION_STATUS.json').exists()]
        seeded_sources=[p for p in originals if p not in fresh_sources]
        fresh=[prepare_book(p,gateway,service) for p in fresh_sources]
        if any(j is None for j in fresh):raise ValueError('Fresh source lacks its verified cleaning inputs')
        save(ROOT/'FRESH_JOBS.json',fresh)
        state(prepared_books=len(fresh),expected_books=len(originals))
        def prepare_seeded():
            seeded=[]
            for original in seeded_sources:
                job=prepare_book(original,gateway,service)
                if job is None:raise ValueError('Prior source lacks its verified cleaning inputs')
                seeded.append(job)
                save(ROOT/'PREPARATION_PROGRESS.json',{'state':'verifying_and_migrating',
                    'prepared_seeded_books':len(seeded),'expected_seeded_books':len(seeded_sources),
                    'verified_cleaned_baseline':sum(j['retained_cleaned_records'] for j in seeded),
                    'updated_at_beijing':now()})
            return seeded
        # Cache migration does no inference. The three unstarted books may finish
        # their first round while the completed first-round caches are verified.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as preparation_pool:
            pending_preparation=preparation_pool.submit(prepare_seeded)
            if fresh:
                transition_phase(gateway.api_url,512,'dictionary_resume_cleaning_round_1')
                state(state='running',phase='cleaning',round=1,current_limit=512,round_books=len(fresh))
                outcomes=run_simple_phase(fresh,lambda j:invoke(j,1,64),8)
                save(ROOT/'ROUND_1.json',{'round':1,'limit':512,'results':outcomes})
            state(state='preparing',phase='verifying_prior_checkpoints')
            seeded=pending_preparation.result()
        jobs=sorted(fresh+seeded,key=lambda j:j['source'])
        save(ROOT/'JOBS.json',jobs);state(retained_cleaned_records=sum(j['retained_cleaned_records'] for j in jobs))
        for number,limit,workers in [(2,128,16),(3,32,4)]:
            cohort=[j for j in jobs
                if read(Path(j['out'])/'ORCHESTRATION_STATUS.json').get('state')=='awaiting_retry'
                and read(Path(j['out'])/'ORCHESTRATION_STATUS.json').get('next_round')==number]
            if not cohort:continue
            assert_original_idle();transition_phase(gateway.api_url,limit,'dictionary_resume_cleaning_round_'+str(number))
            state(state='running',phase='cleaning',round=number,current_limit=limit,round_books=len(cohort))
            outcomes=run_simple_phase(cohort,lambda j:invoke(j,number,workers),8)
            save(ROOT/('ROUND_'+str(number)+'.json'),{'round':number,'limit':limit,'results':outcomes})
        state(phase='cleaning_finalize')
        finals=run_simple_phase(jobs,lambda j:invoke(j,0,64,True),8)
        save(ROOT/'CLEANING_FINAL.json',finals)
        cleaned=[j for j,r in zip(jobs,finals) if r['status']=='completed']
        state(phase='mounting',cleaned_books=len(cleaned))
        # Mounting entry is supplied and validated independently before publication.
        from resume_mounting import finish_dictionary
        summary=finish_dictionary(ROOT,cleaned,service,gateway,profile)
        save(ROOT/'dictionary/SUMMARY.json',summary)
        state(state='finished_with_unresolved' if summary.get('unresolved_stages') else 'finished',phase='finished',summary=summary,finished_at_beijing=now())

def _job_config(job):
    manifest=read(Path(job['out'])/'MANIFEST.json')
    return {**manifest['config'],'workers':1}


def prepare_controller_resume(source, target, freeze_receipt, book_checkpoints, source_code=None,
                              inspection_manifest=None, inspection_manifest_sha256=None,
                              code_migration_proof=None, code_migration_sha256=None):
    """Offline only: import verified native checkpoints into a prepared new code root."""
    import resume_integrity as integrity
    source,target=Path(source).resolve(strict=True),Path(target).resolve(strict=True)
    if source==target or source in target.parents or target in source.parents:
        raise ValueError('Controller migration requires an independent attempt root')
    bench.verify_code_snapshot(target/'CODE_SNAPSHOT.json')
    code_transition=None
    if code_migration_proof:
        if not code_migration_sha256 or sha(code_migration_proof)!=code_migration_sha256:
            raise ValueError('Code migration proof hash binding differs')
        code_transition={'path':str(Path(code_migration_proof).resolve(strict=True)),
                         'sha256':code_migration_sha256}
    old_jobs=read(source/'JOBS.json')
    service=read(target/'SERVICE_PREFLIGHT.json')
    original_service=read(source/'SERVICE_PREFLIGHT.json')
    if any(service.get(key)!=original_service.get(key) for key in ('api_url','model','configured_context')):
        raise ValueError('Controller migration upstream/model/context identity changed')
    inspections={}
    if inspection_manifest:
        if not inspection_manifest_sha256 or sha(inspection_manifest)!=inspection_manifest_sha256:
            raise ValueError('Inspection manifest hash binding differs')
        inspected=read(inspection_manifest)
        if inspected.get('schema')!=1 or inspected.get('source')!=str(source):
            raise ValueError('Inspection manifest source identity differs')
        inspections=inspected['inspections']
    jobs=[]
    for old in old_jobs:
        old_out=Path(old['out']).resolve(strict=True)
        if not old_out.is_relative_to(source):
            raise ValueError('Original job output escapes frozen dictionary attempt')
        destination=target/old_out.relative_to(source)
        old_home=Path(old['home']).resolve(strict=True)
        home=target/old_home.relative_to(source)
        config=_job_config(old)
        if config.get('recovery_provenance'):
            config['recovery_provenance']=str(destination/'SOURCE_CACHE.json')
        books=native.validate_books(read(old['books']))
        receipt_path=destination.parent/(destination.name+'_RESUME_RECEIPT.json')
        if destination.exists():
            receipt=read(receipt_path)
            if receipt.get('source')!=str(old_out) or receipt.get('files')!=integrity.snapshot_files(destination):
                raise ValueError('Existing migration is incomplete or changed; use offline inspection')
        else:
            prior_inspection=inspections.get(old['job_id'])
            receipt_path=integrity.migrate_checkpoint(old_out,destination,old['input'],config,books,native,
                source_code=source_code or source/'code/modules/dictionary',
                freeze_receipt=freeze_receipt,book_checkpoints=book_checkpoints,
                inspection_receipt=prior_inspection['path'] if prior_inspection else None,
                inspection_sha256=prior_inspection['sha256'] if prior_inspection else None)
            receipt=read(receipt_path)
        current_code=native.code_hashes()
        if receipt['new_manifest']['code']!=current_code:
            if code_transition is None:
                raise ValueError('Existing copied cache requires explicit old/new CODE_MIGRATION_PROOF')
            integrity.verify_code_migration(code_transition,receipt['new_manifest']['code'],current_code)
        command=[sys.executable,'-B',str(target/'code/adapters/dictionary_cleaning.py'),
            '--input',old['input'],'--books',old['books'],'--out',str(destination),
            '--export',str(home/'run/04_cleaning/export'),'--subject',read(old['config'])['subject']['name'],
            '--slug',old['subject'],'--api-url',config['api_url'],'--model',config['model'],
            '--context-limit',str(config.get('context_limit',32768))]
        if config.get('recovery_source'):
            command+=['--recovery-source',config['recovery_source'],
                      '--recovery-provenance',config['recovery_provenance']]
        pipeline=read(old['config']);pipeline['paths']['run']=str(home/'run')
        save(home/'pipeline.json',pipeline)
        job={**old,'home':str(home),'config':str(home/'pipeline.json'),'out':str(destination),
            'command':command,'migrated_from':str(old_out),'resume_receipt':str(receipt_path),
            'resume_receipt_sha256':sha(receipt_path),'initial_resume_round':receipt['resume_round'],
            'retained_cleaned_records':receipt['retained_passed']['count']}
        if code_transition:
            job['code_migration_proof']=code_transition
        jobs.append(job)
        save(target/'MIGRATION_PROGRESS.json',{'prepared_books':len(jobs),'expected_books':len(old_jobs),
             'inference_executed':False,'semantic_quality_approved':False})
        save(target/'JOBS.json',jobs)
    save(target/'RESUME_PLAN.json',{'schema':1,'source':str(source),'jobs':len(jobs),
        'freeze_receipt_sha256':sha(freeze_receipt),'book_checkpoints_sha256':sha(book_checkpoints),
        'inspection_manifest_sha256':inspection_manifest_sha256,
        'code_migration_proof':code_transition,
        'round_limits':[1024,256,64],'inference_executed':False,'semantic_quality_approved':False})
    return jobs


def _json_hash(value):
    data=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    return hashlib.sha256(data).hexdigest(),len(data)


def _restore_gateway_rebind(job,persist=None):
    if not job.get('gateway_rebind_transaction'):
        return False
    import resume_integrity as integrity
    with integrity.writer_guard(job['out']):
        return _restore_gateway_rebind_locked(job,persist)


def _restore_gateway_rebind_locked(job,persist=None):
    """Finish only a write-ahead migration whose hash was saved in JOBS first."""
    token=job.get('gateway_rebind_transaction')
    if not token:
        return False
    import resume_integrity as integrity
    integrity.verify_writer_idle(job['out'],job['resume_receipt'],clear_stale=True)
    path=Path(token['path'])
    if sha(path)!=token['sha256']:
        raise ValueError('Gateway migration transaction hash changed')
    transaction=read(path)
    out=Path(job['out']);receipt=Path(job['resume_receipt'])
    if (transaction.get('schema')!=1 or transaction.get('job_id')!=job['job_id'] or
            transaction.get('out')!=str(out) or transaction.get('receipt')!=str(receipt) or
            job['resume_receipt_sha256'] not in (transaction['old_receipt_sha256'],transaction['new_receipt_sha256']) or
            _json_hash(transaction['new_receipt'])[0]!=transaction['new_receipt_sha256']):
        raise ValueError('Gateway migration transaction identity differs')
    if token.get('state')=='committed':
        if sha(receipt)!=transaction['new_receipt_sha256']:
            raise ValueError('Committed gateway migration receipt changed')
        return False
    for relative,change in transaction['mutations'].items():
        if relative not in ('MANIFEST.json','ORCHESTRATION_STATUS.json'):
            raise ValueError('Gateway migration attempted an unauthorized artifact')
        actual=sha(out/relative)
        if actual not in (change['old_sha256'],change['new_sha256']) or _json_hash(change['value'])[0]!=change['new_sha256']:
            raise ValueError('Gateway migration artifact changed outside its transaction')
    if sha(receipt) not in (transaction['old_receipt_sha256'],transaction['new_receipt_sha256']):
        raise ValueError('Controller resume receipt changed outside its gateway transaction')
    for relative,change in transaction['mutations'].items():
        if sha(out/relative)!=change['new_sha256']:
            save(out/relative,change['value'])
    if sha(receipt)!=transaction['new_receipt_sha256']:
        save(receipt,transaction['new_receipt'])
    command=list(job['command']);command[command.index('--api-url')+1]=transaction['api_url']
    job.update(command=command,resume_receipt_sha256=transaction['new_receipt_sha256'])
    token['state']='committed'
    if persist:
        persist()
    return True


def _rebind_resumed_job(job,gateway,persist=None):
    import resume_integrity as integrity
    with integrity.writer_guard(job['out']):
        return _rebind_resumed_job_locked(job,gateway,persist)


def _rebind_resumed_job_locked(job,gateway,persist=None):
    """Record a gateway-only manifest migration before starting an inspected job."""
    import resume_integrity as integrity
    out=Path(job['out']);receipt_path=Path(job['resume_receipt'])
    integrity.verify_writer_idle(out,receipt_path,clear_stale=True)
    receipt=read(receipt_path)
    current_files=integrity.snapshot_files(out)
    if receipt.get('files')!=current_files:
        integrity.verify_retained_passes(out,receipt_path)
        inspected=integrity.inspect_checkpoint(out,job['input'],native)
        receipt.update({key:inspected[key] for key in ('completed_round','resume_round','round_complete',
                        'stable_completed_prefix_verified','journal_records','retained_passed')})
        receipt['historical_seed_proofs']=inspected['historical_seed_proofs']
        receipt.update(new_manifest=read(out/'MANIFEST.json'),files=inspected['files'])
        receipt['native_completion_pending']=inspected.get('native_completion_pending',False)
    if receipt.get('native_completion_pending'):
        completed=read(out/'ORCHESTRATION_STATUS.json')
        old_state=read(out/'STATE.json')
        if (old_state.get('status')!='round_running' or old_state.get('round')!=completed.get('round') or
                integrity.stable_checkpoint_hashes(receipt['files'])!=completed.get('checkpoint_hashes')):
            raise ValueError('Native completed receipt does not authorize STATE reconciliation')
        save(out/'STATE.json',{'status':'round_complete',**read(out/'SUMMARY.json'),'round':completed['round']})
        receipt['files']['STATE.json']={'sha256':sha(out/'STATE.json'),'size':(out/'STATE.json').stat().st_size}
        receipt['native_completion_pending']=False
        receipt.setdefault('state_reconciliations',[]).append({'from':'round_running','to':'round_complete',
            'round':completed['round'],'evidence':'complete native ORCH and exact stable checkpoint hashes'})
    config=_job_config(job)
    config['api_url']=gateway.job_url(job['job_id'])
    manifest=native.staged_manifest(job['input'],config,native.validate_books(read(job['books'])))
    old_manifest=read(out/'MANIFEST.json')
    if integrity._semantic_identity(manifest)!=integrity._semantic_identity(old_manifest):
        raise ValueError('Gateway rebind changed semantic identity')
    code_transition=None
    if manifest['code']!=old_manifest['code']:
        code_transition=job.get('code_migration_proof')
        if not code_transition:
            raise ValueError('Gateway rebind needs an explicit operational CODE_MIGRATION_PROOF')
        integrity.verify_code_migration(code_transition,old_manifest['code'],manifest['code'])
    mutations={'MANIFEST.json':{'old_sha256':sha(out/'MANIFEST.json'),'value':manifest,
                               'new_sha256':_json_hash(manifest)[0]}}
    current_files=receipt.get('files',current_files)
    checksum,size=_json_hash(manifest)
    current_files['MANIFEST.json']={'sha256':checksum,'size':size}
    if receipt['round_complete']:
        previous=read(out/'ORCHESTRATION_STATUS.json')
        previous['checkpoint_hashes']=integrity.stable_checkpoint_hashes(current_files)
        checksum,size=_json_hash(previous)
        mutations['ORCHESTRATION_STATUS.json']={'old_sha256':sha(out/'ORCHESTRATION_STATUS.json'),
            'value':previous,'new_sha256':checksum}
        current_files['ORCHESTRATION_STATUS.json']={'sha256':checksum,'size':size}
    receipt.update(new_manifest=manifest,files=current_files)
    receipt.setdefault('gateway_migrations',[]).append({'from':old_manifest['config']['api_url'],
        'to':config['api_url'],'at':now(),'code_migration_proof':code_transition})
    transaction={'schema':1,'job_id':job['job_id'],'out':str(out),'receipt':str(receipt_path),
        'old_receipt_sha256':sha(receipt_path),'new_receipt_sha256':_json_hash(receipt)[0],
        'new_receipt':receipt,'mutations':mutations,'api_url':config['api_url']}
    transaction_path=receipt_path.parent/(receipt_path.stem+'_GATEWAY_REBIND_'+uuid.uuid4().hex+'.json')
    save(transaction_path,transaction)
    job.setdefault('resume_receipt_sha256',transaction['old_receipt_sha256'])
    job['gateway_rebind_transaction']={'path':str(transaction_path),'sha256':sha(transaction_path)}
    if persist:
        persist()
    _restore_gateway_rebind(job,persist)


def run_resumed(target):
    """Resume a migrated controller from native receipts, including its current cohort."""
    global ROOT
    target=Path(target).resolve(strict=True)
    if Path(__file__).resolve().parent!=target:
        raise ValueError('Execute the frozen resume controller inside the new attempt root')
    ROOT=target
    import fcntl
    with (ROOT/'BACKGROUND.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run_resumed_locked()


def _prepare_compensation_job(job,service):
    import resume_integrity as integrity
    from compensation_recovery import home
    out=Path(job['out'])
    if not (out/'SOURCE_CACHE.json').exists():
        return None
    current=read(out/'STATE.json')
    if current.get('status') not in ('round_complete','finished','finished_with_pending'):
        raise ValueError('Compensation requires completed regular cohorts; a native round is still running')
    token=job.get('compensation_plan')
    if token:
        plan=integrity.load_compensation_plan(token)
        if Path(plan['current']).resolve()!=out.resolve():
            raise ValueError('Controller compensation plan names another job cache')
        return plan
    status=read(out/'ORCHESTRATION_STATUS.json')
    if status.get('round')!=3:
        # A terminal earlier cohort has no remaining transport request to retry.
        if status.get('state') not in ('ready_to_finalize','finalized') and not (
                status.get('state')=='technical_failure' and status.get('retryable_technical_failures')==0):
            raise ValueError('Compensation was selected before regular cohorts completed')
        return None
    proof=integrity.deployment_revocation_proof(out,ROOT/'EVIDENCE.json',ROOT/'READONLY_CAPTURE.json',service['api_url'])
    plan=integrity.compensation_plan(proof['source'],out,proof)
    path=home(out)/'PLAN.json'
    if path.exists() and read(path)!=plan:
        raise ValueError('Orphaned compensation plan differs; inspect its immutable evidence')
    if not path.exists():
        save(path,plan)
    job['compensation_plan']={'path':str(path),'sha256':sha(path)}
    return plan


def _invoke_compensation(job):
    from compensation_recovery import home
    token=job['compensation_plan']
    command=job['command']+['--workers','64','--compensation-plan',token['path'],
                            '--compensation-sha256',token['sha256']]
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
    with (Path(job['home'])/'compensation.log').open('a') as log:
        result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=env)
    if result.returncode:
        raise RuntimeError('Isolated lost-opportunity compensation did not commit: '+job['job_id'])
    value=read(home(job['out'])/token['sha256']/'RESULT.json')
    if value.get('plan_sha256')!=token['sha256']:
        raise ValueError('Compensation result names another sealed plan')
    return value


def _compensate_jobs(jobs,service,gateway):
    from compensation_recovery import home
    cohort=[];results=[]
    for job in jobs:
        plan=_prepare_compensation_job(job,service)
        save(ROOT/'JOBS.json',jobs)  # The plan hash must survive before a worker may consume any credit.
        if not plan or not plan['eligible']:
            results.append({'job_id':job['job_id'],'status':'no_eligible_lost_opportunity'})
            continue
        token=job['compensation_plan'];done=home(job['out'])/token['sha256']/'RESULT.json'
        if done.exists():
            results.append({'job_id':job['job_id'],**read(done)})
        else:
            cohort.append(job)
    if cohort:
        transition_phase(gateway.api_url,64,'dictionary_evidenced_retirement_compensation')
        state(state='running',phase='retirement_compensation',current_limit=64,books=len(cohort))
        results+=run_simple_phase(cohort,_invoke_compensation,min(8,len(cohort)))
    save(ROOT/'COMPENSATION_RESULTS.json',{'results':results,'round_numbers_rewritten':False,
                                          'semantic_quality_approved':False})
    return results


def _run_resumed_locked():
    bench.verify_code_snapshot(ROOT/'CODE_SNAPSHOT.json')
    jobs=read(ROOT/'JOBS.json');plan=read(ROOT/'RESUME_PLAN.json')
    if len(jobs)!=plan['jobs']:
        raise ValueError('Controller migration has not completed all books')
    service=read(ROOT/'SERVICE_PREFLIGHT.json')
    state(state='resuming',pid=os.getpid(),source=plan['source'],semantic_quality_approved=False)
    profile=ResourceProfile(gpu_cards=32,limit=1024,front_limit=1024,
                            extraction_limit=1024,model_limit=1024,front_workers=1024,
                            cpu_workers=8,production_workers=8,mount_workers=8)
    epoch=uuid.uuid4().hex
    with Gateway(ROOT/'gateway'/epoch,service['api_url'],limit=1024) as gateway:
        state(gateway_epoch=epoch,gateway_url=gateway.api_url)
        for job in jobs:
            import resume_integrity as integrity
            integrity.verify_writer_idle(job['out'],job['resume_receipt'],clear_stale=True)
            from compensation_recovery import recover_commit
            recover_commit(job['out'])  # Complete authorized backfill before native hash inspection/rebind.
            _restore_gateway_rebind(job,lambda:save(ROOT/'JOBS.json',jobs))
            if sha(job['resume_receipt'])!=job['resume_receipt_sha256']:
                raise ValueError('Controller resume receipt changed')
            _rebind_resumed_job(job,gateway,persist=lambda:save(ROOT/'JOBS.json',jobs))
            save(ROOT/'JOBS.json',jobs)
            state(phase='verifying_migrated_books',last_verified_book=job['job_id'])
        save(ROOT/'JOBS.json',jobs)
        for number,limit in ((1,1024),(2,256),(3,64)):
            cohort=[]
            for job in jobs:
                out=Path(job['out']);value=read(out/'ORCHESTRATION_STATUS.json');current=read(out/'STATE.json')
                if current.get('status')=='round_running' and current.get('round')==number or (
                        value.get('state')=='awaiting_retry' and value.get('next_round')==number):
                    cohort.append(job)
            if not cohort:
                continue
            transition_phase(gateway.api_url,limit,'dictionary_resume_cleaning_round_'+str(number))
            state(state='running',phase='cleaning',round=number,current_limit=limit,round_books=len(cohort))
            parallel=min(8,len(cohort));workers=limit
            results=run_simple_phase(cohort,lambda job:invoke(job,number,workers),parallel)
            save(ROOT/('ROUND_'+str(number)+'.json'),{'round':number,'limit':limit,'results':results})
        _compensate_jobs(jobs,service,gateway)
        finals=run_simple_phase(jobs,lambda job:invoke(job,0,64,True),min(8,len(jobs)))
        save(ROOT/'CLEANING_FINAL.json',finals)
        cleaned=[job for job,result in zip(jobs,finals) if result['status']=='completed']
        from resume_mounting import finish_dictionary
        summary=finish_dictionary(ROOT,cleaned,service,gateway,profile)
        save(ROOT/'dictionary/SUMMARY.json',summary)
        state(state='finished_with_unresolved' if summary.get('unresolved_stages') else 'finished',
              phase='finished',summary=summary,semantic_quality_approved=False,finished_at_beijing=now())
    return 0


def main(argv=None):
    import resume_integrity as integrity
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--inspect-book',type=Path)
    modes.add_argument('--migrate-book',type=Path)
    modes.add_argument('--migrate-source',type=Path,help='Offline preparation of a frozen controller attempt')
    modes.add_argument('--resume',action='store_true',help='Continue a fully migrated independent controller')
    modes.add_argument('--plan-compensation',type=Path,help='Original cache with evidenced lost HTTP404 opportunity')
    modes.add_argument('--prepare-code-migration',type=Path,help='Immutable QA3 native code to bind to final operational code')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--input',type=Path);parser.add_argument('--books',type=Path)
    parser.add_argument('--config',type=Path);parser.add_argument('--out',type=Path)
    parser.add_argument('--report-out',type=Path);parser.add_argument('--source-code',type=Path)
    parser.add_argument('--freeze-receipt',type=Path);parser.add_argument('--book-checkpoints',type=Path)
    parser.add_argument('--current',type=Path);parser.add_argument('--revocation-proof',type=Path)
    parser.add_argument('--inspection-receipt',type=Path);parser.add_argument('--inspection-sha256')
    parser.add_argument('--inspection-manifest',type=Path);parser.add_argument('--inspection-manifest-sha256')
    parser.add_argument('--code-migration-proof',type=Path);parser.add_argument('--code-migration-sha256')
    args=parser.parse_args(argv)
    if args.prepare_code_migration:
        if not args.source_code or not args.report_out:
            parser.error('--prepare-code-migration requires --source-code and --report-out')
        save(args.report_out,integrity.code_migration_proof(args.prepare_code_migration,
            args.root/'code/modules/dictionary',args.source_code))
        return 0
    if args.inspect_book:
        if not args.input or not args.report_out:
            parser.error('--inspect-book requires --input and --report-out')
        save(args.report_out,integrity.inspect_checkpoint(args.inspect_book,args.input,native));return 0
    if args.migrate_book:
        if not all((args.input,args.books,args.config,args.out,args.freeze_receipt,args.book_checkpoints)):
            parser.error('--migrate-book requires input/books/config/out and both frozen source receipts')
        receipt=integrity.migrate_checkpoint(args.migrate_book,args.out,args.input,read(args.config),
            native.validate_books(read(args.books)),native,source_code=args.source_code,
            freeze_receipt=args.freeze_receipt,book_checkpoints=args.book_checkpoints,
            inspection_receipt=args.inspection_receipt,inspection_sha256=args.inspection_sha256)
        print(json.dumps({'resume_receipt':str(receipt),'inference_executed':False}));return 0
    if args.migrate_source:
        if not args.freeze_receipt or not args.book_checkpoints:
            parser.error('--migrate-source requires both frozen source receipts')
        prepare_controller_resume(args.migrate_source,args.root,args.freeze_receipt,args.book_checkpoints,args.source_code,
            args.inspection_manifest,args.inspection_manifest_sha256,args.code_migration_proof,args.code_migration_sha256)
        return 0
    if args.plan_compensation:
        if not all((args.current,args.revocation_proof,args.report_out)):
            parser.error('--plan-compensation requires --current, --revocation-proof and --report-out')
        save(args.report_out,integrity.compensation_plan(args.plan_compensation,args.current,read(args.revocation_proof)))
        return 0
    if args.resume:
        return run_resumed(args.root)
    return run()


if __name__=='__main__':
    try:raise SystemExit(main())
    except SystemExit:raise
    except BaseException as e:
        state(state='failed',error=repr(e));raise
