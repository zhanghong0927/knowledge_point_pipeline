"""152 known negatives plus 5 previously unseen source rows per subject."""
import argparse,json,random
from pathlib import Path
from run_rule_checks import fingerprint,load_records,dump

def main():
    p=argparse.ArgumentParser();p.add_argument('--previous',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    old=Path(a.previous);out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    previous=[json.loads(l) for l in (old/'review_details.jsonl').read_text(encoding='utf-8').splitlines()]
    excluded={r['request_id'] for r in previous}
    fields=('id','request_id','name','knowledge_point','definition','en_definition','description','en_description','main_tags','source','taxonomy_context','truncated_fields','subject','subject_code','source_row')
    inputs=[dict({k:r[k] for k in fields if k in r},cohort='regression_152') for r in previous if r.get('audit') and r['audit']['judgment']=='unreasonable']
    assert len(inputs)==152
    manifest=[];rng=random.Random(20260923)
    for m in json.loads((old/'manifest.json').read_text(encoding='utf-8')):
        source=Path(m['source']['path']);before=fingerprint(source);records,fmt=load_records(source)
        # The holdout is for an initial cross-subject smoke test, not a prevalence estimate.
        candidates=[i for i in range(len(records)) if m['subject']+':'+str(i+1) not in excluded]
        selected=sorted(rng.sample(candidates,5))
        for i in selected:
            r=records[i]
            x={k:r.get(k,'') for k in fields if k not in ('request_id','taxonomy_context','truncated_fields','subject','subject_code','source_row')}
            x.update(request_id=m['subject']+':'+str(i+1),subject=m['label'],subject_code=m['subject'],source_row=i+1,
                     cohort='unseen_60',taxonomy_context={'available':False,'exact_path_exists':None,'ancestor_chain':[]},truncated_fields=[])
            for k in ('definition','en_definition','description','en_description'):
                if isinstance(x[k],str) and len(x[k])>2500: x[k]=x[k][:2500];x['truncated_fields'].append(k)
            inputs.append(x)
        assert fingerprint(source)==before
        manifest.append(dict(subject=m['subject'],source=before,source_rows=[i+1 for i in selected]))
        print('PREPARED',m['subject'],flush=True)
    assert len(inputs)==212 and len({x['request_id'] for x in inputs})==212
    assert not ({x['request_id'] for x in inputs if x['cohort']=='unseen_60'} & excluded)
    (out/'samples.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in inputs),encoding='utf-8')
    dump(out/'holdout_manifest.json',manifest)
    dump(out/'verification.json',dict(regression=152,unseen=60,overlap_with_prior_1200=0,delivery_modified=False))

if __name__=='__main__':main()
