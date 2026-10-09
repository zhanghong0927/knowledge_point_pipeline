import argparse,json,hashlib
from collections import Counter
from pathlib import Path
from mount_pilot_v3 import api_view,combine

def load(path): return [json.loads(l) for l in path.read_text(encoding='utf-8-sig').splitlines() if l.strip()]
def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--previous',required=True);a=p.parse_args()
    out=Path(a.run);samples=load(out/'samples.jsonl');details=load(out/'review_details.jsonl')
    config=json.loads((out/'config.json').read_text())
    assert hashlib.sha256((out/'samples.jsonl').read_bytes()).hexdigest()==config['input_sha256']
    expected={r['request_id']:r for r in samples};actual={r['request_id']:r for r in details}
    assert len(expected)==len(samples)==len(actual)==len(details)==212 and set(expected)==set(actual)
    old={r['request_id']:r for r in load(Path(a.previous)/'review_details.jsonl')}
    for rid,r in actual.items():
        assert {k:v for k,v in r.items() if k!='review_v3'}==expected[rid]
        view=api_view(r);assert not {'audit','review_v3','review_judgment','review_reason','cohort','subject','subject_code'}&set(view)
        review=r['review_v3']; first=review['first']['result'];second=review['second']['result'] if review['second'] else None
        assert combine(first,second)==review['final']
        for result in (first,second):
            if result:
                for key in ('support_evidence','conflict_evidence'):
                    for e in result[key]: assert e['text'] in r[e['field']]
        if r['cohort']=='regression_152':
            assert old[rid]['audit']['judgment']=='unreasonable'
            assert api_view(old[rid])==view
        else: assert rid not in old
    counts=Counter(r['review_v3']['final']['judgment'] for r in details)
    summary=json.loads((out/'summary.json').read_text())
    assert dict(counts)==summary['counts'] and summary['input_unchanged']
    result=dict(records=212,unique_ids=212,regression_input_unchanged=152,unseen_overlap_with_previous=0,
                exact_evidence_verified=True,aggregation_verified=True,reference_fields_excluded=True,
                technical_failures=counts.get('technical_failure',0))
    (out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
