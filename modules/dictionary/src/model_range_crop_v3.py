"""Conservative structural crop plus deterministic Markdown/whitespace cleanup."""
import argparse
import concurrent.futures
import hashlib
import json
import random
import re
import shutil
import time
from collections import Counter
from pathlib import Path
import model_range_crop as base
import model_range_crop_v2 as v2

PROMPT=v2.PROMPT+'''
Additional boundary checks, mandatory before KEEP:
- Treat head typography hierarchically: a title, its author byline, occupation subtitle,
  epigraph attribution and rhetorical subtitle are NOT a compound head. Join two lines
  only if they are genuine continuation of the same lexical name (or literal bilingual
  equivalents), never simply because both are adjacent or bold. Use the true main head
  alone and exclude bylines/subtitles from head_parts. When undecidable, DROP.
- A date in a person's chronology is not an independent dictionary entry.
- At each body endpoint, inspect the next nonempty source line. A copula such as 'are',
  'is', 'was', a dangling hyphen, unfinished Chinese word/clause, or semicolon-delimited
  cross-reference list demands continuation. Continue only within the SAME entry, or
  return an earlier complete sentence/paragraph. Do not emit an incomplete excerpt.
- Preserve every item in a selected cross-reference statement, including wrapped items.
- Skip recurring page headers and author credits using separate spans. Do not delete
  substantive sentences just because they contain a person's name.
- For literal head_parts prefer the text after Markdown ##, without the marker.
No name relevance filtering and no definition/description splitting in this stage.
'''


def strip_markers(text):
    return re.sub(r'(?m)^\s{0,3}#{1,6}[ \t]+','',text)


def clean_format(text):
    text=strip_markers(text)
    rows=[re.sub(r'[ \t]+',' ',r).strip() for r in text.splitlines() if r.strip()]
    if not rows:return ''
    out=rows[0]
    for row in rows[1:]:
        left,right=out[-1],row[0]
        cjk=lambda c:'\u3400'<=c<='\u9fff'
        if (cjk(left) and cjk(right)) or (left=='-' and right.isalpha()):sep=''
        elif left not in '.!?。！？:：;；' and right.islower():sep=' '
        else:sep='\n\n'
        out+=sep+row
    assert re.sub(r'\s','',out)==re.sub(r'\s','',text)
    return out


def validate_result(packet,result):
    checked=v2.validate_result(packet,result)
    if checked['decision']=='DROP':return checked
    e=checked['entries'][0]
    e['selected_head']=e['head']
    e['head']=' '.join(strip_markers(p['text']).strip() for p in e['head_parts']).strip()
    if not e['head']:raise ValueError('Empty cleaned head')
    if re.fullmatch(r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',e['head'],re.I):
        return dict(sample_id=packet['sample_id'],decision='DROP',reason='Conservative gate: standalone chronology date, not a lexical entry',entries=[],model_decision='KEEP')
    raw=e['raw_content'].rstrip()
    if raw and (re.search(r'\b(?:is|are|was|were|be|been|being|has|have|had|will|would|can|could|should|must)\s*$',raw,re.I) or raw.endswith(('-', ';','；',':','：'))):
        raise ValueError('Incomplete final body boundary; select continuation or earlier closed excerpt')
    chunks=[]
    byline={r['line']:r['text'] for r in packet['lines']}
    for s in e['body_locations']:
        a,b=s['start_line'],s['end_line'];c,d=s['start_column'],s['end_column']
        text=byline[a][c:d] if a==b else '\n'.join([byline[a][c:]]+[byline[n] for n in range(a+1,b)]+[byline[b][:d]])
        chunks.append(clean_format(text))
    e['formatted_content']='\n\n'.join(chunks)
    e['format_changes']={'head_markdown_removed':e['head']!=e['selected_head'],
        'body_format_changed':e['formatted_content']!=raw}
    return checked


def selection(root):
    prior=base.read(root/'20260922_model_range_crop_v2_compare100/MANIFEST.json')['sample_ids']
    samples=base.read(root/'20260922_structure_guard_v3_audit2000/judgments_final.json')
    pool=sorted(s['sample_id'] for s in samples if s['sample_id'] not in prior)
    return {'regression100':prior,'new900':random.Random(20260923).sample(pool,900)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--workers',type=int,default=64)
    a=p.parse_args();a.out.mkdir(exist_ok=False);cfg=base.read(a.config)['config'];started=time.time()
    packets=base.prepare(a.root,a.out,selection(a.root))
    for module in [__file__,v2.__file__]:shutil.copyfile(module,a.out/'code'/Path(module).name)
    manifest=base.read(a.out/'MANIFEST.json');manifest.update(prompt=PROMPT,
        selection='Previous100 regression plus seeded900 new IDs from remaining audited pool; not corpus-uniform',
        model_version='v3',model_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        comparison='frozen pure rule v4c versus rule-localized model v3, NOT v2 versus v3')
    base.write(a.out/'MANIFEST.json',manifest)
    base.write(a.out/'CONFIG.json',dict(api_url=cfg['api_url'],model=cfg['model'],workers=a.workers,max_attempts=2))
    base.PROMPT=PROMPT;base.validate_result=validate_result
    first=base.request_one(packets[0],cfg,a.out)
    if first['status']=='technical_failure':raise RuntimeError('Actual probe failed twice, batch not dispatched')
    results=[first]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures=[pool.submit(base.request_one,p,cfg,a.out) for p in packets[1:]]
        for f in concurrent.futures.as_completed(futures):
            r=f.result();results.append(r)
            base.write(a.out/'PROGRESS.json',dict(stage='crop',expected=len(packets),finished=len(results),technical_failures=sum(x['status']=='technical_failure' for x in results)))
            print(r['sample_id'],r['status'],r.get('decision'),flush=True)
    summary=dict(expected=len(packets),finished=len(results),statuses=dict(Counter(r['status'] for r in results)),
        decisions=dict(Counter(r.get('decision','TECHNICAL_FAILURE') for r in results)),requests=sum(r['attempts'] for r in results),
        elapsed_seconds=round(time.time()-started,2),semantic_quality_approved=False)
    base.write(a.out/'SUMMARY.json',summary);print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
