"""Exact same-node OR-name dedup, longest Chinese definition first."""
import sys,json,hashlib
from pathlib import Path
from run_rule_checks import load_records,dump

def length(r):
    value=r.get('definition')
    return len(value.strip()) if isinstance(value,str) else 0

def keys(r):
    path=r.get('main_tags')
    if not isinstance(path,str) or not path.strip(): return []
    return [(path,f,r[f]) for f in ('name','knowledge_point') if isinstance(r.get(f),str) and r[f].strip()]

def select_records(records):
    seen={}; keep=[]; removed=[]
    for i in sorted(range(len(records)),key=lambda i:(-length(records[i]),i)):
        matches=[(k,seen[k]) for k in keys(records[i]) if k in seen]
        if matches:
            removed.append(dict(removed_index=i,kept_index=matches[0][1],matching_field=matches[0][0][1],definition_length=length(records[i]),kept_definition_length=length(records[matches[0][1]])))
        else:
            keep.append(i)
            for k in keys(records[i]): seen[k]=i
    return sorted(keep),sorted(removed,key=lambda r:r['removed_index'])

def sha(raw): return hashlib.sha256(raw).hexdigest()

def prepare(summary,stage):
    stage=Path(stage); stage.mkdir(parents=True,exist_ok=False)
    reports=[]
    for r in json.loads(Path(summary).read_text()):
        if r['subject'] not in ('sociology','architecture','literature'): continue
        source=Path(r['input']['path']); raw=source.read_bytes()
        assert sha(raw)==r['input']['sha256'], 'source changed since audit'
        folder=stage/r['subject']; folder.mkdir()
        backup=folder/('original_'+source.name); backup.write_bytes(raw)
        assert sha(backup.read_bytes())==sha(raw)
        records,fmt=load_records(backup)
        assert all(isinstance(x,dict) for x in records)
        kept,removed=select_records(records); new=[records[i] for i in kept]
        assert len(new)+len(removed)==len(records)
        seen=set()
        for record in new:
            for key in keys(record):
                assert key not in seen
                seen.add(key)
        for item in removed:
            a,b=item['removed_index'],item['kept_index']
            assert b in kept and length(records[b])>=length(records[a])
            assert set(keys(records[a])) & set(keys(records[b]))
        candidate=folder/source.name
        dump(candidate,new)
        verified=json.loads(candidate.read_text())
        assert verified==new
        dump(folder/'removed.json',[dict(**x,removed_id=records[x['removed_index']].get('id'),kept_id=records[x['kept_index']].get('id')) for x in removed])
        info=dict(subject=r['subject'],label=r['label'],source=str(source),key=str(source).split('/deliver/',1)[1],backup=str(backup),candidate=str(candidate),before=len(records),after=len(new),removed=len(removed),old_sha256=sha(raw),new_sha256=sha(candidate.read_bytes()))
        reports.append(info); print(r['label'],info['before'],info['removed'],info['after'],flush=True)
    assert len(reports)==3
    dump(stage/'prepared.json',reports)

def apply(stage):
    import boto3
    from botocore.config import Config
    stage=Path(stage); reports=json.loads((stage/'prepared.json').read_text())
    aid=sys.stdin.readline().strip(); secret=sys.stdin.readline().strip()
    assert aid and secret
    s=boto3.client('s3',endpoint_url='https://unist-si002991c1cm-00051200-899hp-data.stor.unistor.zhejianglab.org',aws_access_key_id=aid,aws_secret_access_key=secret,config=Config(signature_version='s3v4',s3={'addressing_style':'path'},request_checksum_calculation='when_required',response_checksum_validation='when_required'))
    # Check all exact objects before changing any object.
    for r in reports:
        current=s.get_object(Bucket='deliver',Key=r['key'])['Body'].read()
        assert sha(current) in (r['old_sha256'],r['new_sha256']), 'remote changed: '+r['subject']
        assert sha(Path(r['backup']).read_bytes())==r['old_sha256']
        assert sha(Path(r['candidate']).read_bytes())==r['new_sha256']
    for r in reports:
        obj=s.get_object(Bucket='deliver',Key=r['key']); current=obj['Body'].read()
        assert sha(current) in (r['old_sha256'],r['new_sha256'])
        if sha(current)!=r['new_sha256']:
            s.put_object(Bucket='deliver',Key=r['key'],Body=Path(r['candidate']).read_bytes(),ContentType='application/json')
        raw=s.get_object(Bucket='deliver',Key=r['key'])['Body'].read()
        assert sha(raw)==r['new_sha256']
        records=json.loads(raw); assert len(records)==r['after']
        seen=set()
        for record in records:
            for key in keys(record):
                assert key not in seen
                seen.add(key)
        r['s3_verified']=True
        dump(stage/'applied.json',reports)
        print('VERIFIED',r['subject'],r['after'],'removed',r['removed'],flush=True)

if __name__=='__main__':
    if sys.argv[1]=='prepare': prepare(sys.argv[2],sys.argv[3])
    elif sys.argv[1]=='apply': apply(sys.argv[2])
    else: raise ValueError('prepare or apply required')
