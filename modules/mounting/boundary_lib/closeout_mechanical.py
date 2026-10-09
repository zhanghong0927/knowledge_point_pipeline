"""Resume only failed review calls; preserve immutable previous run and audit trail."""
import json,sys,time,shutil,hashlib,subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import mechanical_expansion as m
import ab_workflow as ab
from accepted_review import metrics
sys.path.insert(0,'/home/wangqiyuan/work/knowledge_rule_checks_20260922')
import mount_pilot_v3 as reviewer
OLD=m.ROOT/'mechanical_full_20260922'
OUT=m.ROOT/'mechanical_full_closeout_20260922'
BASE=m.BASE
def key(x):return json.dumps({k:v for k,v in x.items() if k!='request_id'},sort_keys=True,ensure_ascii=False)
def batch(items,out,unused):
    out.mkdir(exist_ok=False)
    phase=out.name;cached={key(r['item']):r for r in m.lines(OLD/phase/'final.jsonl') if r['review']['judgment']!='technical_failure'}
    results={x['request_id']:dict(request_id=x['request_id'],final=cached[key(x)]['review'],reused=True) for x in items if key(x) in cached}
    pending=[x for x in items if x['request_id'] not in results]
    ab.write_lines(out/'input.jsonl',items)
    ab.write_json(out/'config.json',dict(base=BASE,workers=1024,reused=len(results),pending=len(pending),source=str(OLD/phase)))
    (out/'prompt.txt').write_text(reviewer.PROMPT);(out/'counter_prompt.txt').write_text(reviewer.COUNTER_PROMPT)
    with (out/'responses.jsonl').open('w') as log:
        for round_no in range(3):
            if not pending:break
            with ThreadPoolExecutor(max_workers=1024) as pool:
                futures=[pool.submit(reviewer.review_one,BASE,ab.MODEL,x) for x in pending]
                for f in as_completed(futures):
                    r=f.result();r['closeout_round']=round_no;results[r['request_id']]=r
                    log.write(json.dumps(r,ensure_ascii=False)+'\n');log.flush()
            pending=[x for x in items if results[x['request_id']]['final']['judgment']=='technical_failure']
            print(phase,'round',round_no,'remaining',len(pending),flush=True)
    assert set(results)=={x['request_id'] for x in items}
    ab.write_lines(out/'final.jsonl',[dict(item=x,review=results[x['request_id']]['final']) for x in items])
    ab.write_json(out/'summary.json',metrics([r['final']['judgment'] for r in results.values()]))
    return results
def main():
    assert not OUT.exists();shutil.copytree(OLD,OUT)
    for phase in ['baseline','effect_review']:(OUT/phase).rename(OUT/(phase+'_before_closeout'))
    status=OUT.with_suffix('.state.json')
    def state(stage):ab.write_json(status,dict(stage=stage,updated=time.time(),out=str(OUT)))
    state('baseline_retry')
    reviewer.PROMPT=(OLD/'baseline/prompt.txt').read_text();reviewer.COUNTER_PROMPT=(OLD/'baseline/counter_prompt.txt').read_text()
    results=batch(m.lines(OLD/'baseline/input.jsonl'),OUT/'baseline',reviewer)
    provenance=json.loads((OUT/'provenance.json').read_text())
    selected=[dict(r,cohort=m.cohort(results[r['request_id']]['final']['judgment'])) for r in provenance if m.cohort(results[r['request_id']]['final']['judgment'])]
    ab.write_json(OUT/'remount_cohort.json',selected)
    ab.write_lines(OUT/'remount/input.jsonl',[r['record'] for r in selected])
    state('supplement_mount')
    def supplement(v):
        rem=OUT/'remount';done=m.lines(rem/(v+'.jsonl'));ids={r['record_id'] for r in done}
        pending=[r['record'] for r in selected if r['request_id'] not in ids]
        if not pending:return
        inp=rem/(v+'.additional_input.jsonl');dest=rem/(v+'.additional.jsonl');ab.write_lines(inp,pending)
        cmd=json.loads((rem/(v+'_command.json')).read_text())
        for flag,val in [('--input',str(inp)),('--output',str(dest)),('--workers','512'),('--retry-failed-workers','512')]:cmd[cmd.index(flag)+1]=val
        with (rem/(v+'.additional.log')).open('w') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
        ab.write_lines(rem/(v+'.jsonl'),done+m.lines(dest))
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(supplement,['with_cards','without_cards']))
    state('evaluate_retry')
    # main adds the standard prompt suffix; reset to unmodified import values first.
    reviewer.PROMPT=original_prompt;reviewer.COUNTER_PROMPT=original_counter
    m.review_batch=batch
    sys.argv=['mechanical_expansion.py','evaluate','--out',str(OUT)];m.main()
    state('verifying')
    baseline=m.lines(OUT/'baseline/final.jsonl');details=m.lines(OUT/'effect_details.jsonl');ids={r['request_id'] for r in selected}
    assert len(baseline)==23748 and len({r['item']['request_id'] for r in baseline})==23748
    for v in ['with_cards','without_cards']:
        rr=m.lines(OUT/'remount'/(v+'.jsonl'));assert len(rr)==len(ids) and {r['record_id'] for r in rr}==ids
    for v in ['original','with_cards','without_cards']:
        rr=[r for r in details if r['variant']==v];assert len(rr)==len(ids) and {r['record_id'] for r in rr}==ids
    manifest=json.loads((OUT/'manifest.json').read_text())
    assert hashlib.sha256(m.SOURCE.read_bytes()).hexdigest()==manifest['source_sha256']
    failures=sum(r['review']['judgment']=='technical_failure' for r in baseline)+sum(r['review']['judgment']=='technical_failure' for r in details)
    failures+=sum(r['knowledge_labeling']['status']!='ok' for v in ['with_cards','without_cards'] for r in m.lines(OUT/'remount'/(v+'.jsonl')))
    ab.write_json(OUT/'coverage_verification.json',dict(source_records=len(baseline),remount_records=len(ids),input_coverage_ok=True,current_source_unchanged=True,technical_failure_occurrences=failures))
    summary=json.loads((OUT/'effect_summary.json').read_text())
    report=['# 机械辞海全量审核与重挂对照结果','',f'初审 {len(baseline):,} 条；语义未通过 {len(ids):,} 条进入两种重挂。','',str(json.loads((OUT/'baseline/summary.json').read_text())['counts']),'','| 方案 | 合理 | 不合理 | 不确定 | 未挂载 | 技术失败 |','| --- | ---: | ---: | ---: | ---: | ---: |']
    for v,label in [('original','原路径再次复核'),('with_cards','有语义卡片'),('without_cards','无语义卡片')]:
        c=summary['all'][v]['counts'];report.append('| '+label+' | '+' | '.join(str(c.get(k,0)) for k in ['reasonable','unreasonable','uncertain','not_mounted','technical_failure'])+' |')
    report+=['',f'记录覆盖及唯一性校验通过；源交付文件未变化；技术失败记录次数 {failures}。','原接口历史502造成失败，收尾复用有效结果，只补失败及其新增下游任务。','结果为模型复核，不是专家金标准；原路径复核波动不能直接算作修复收益。未覆盖正式交付文件。']
    (OUT/'全量审核与重挂对照结果.md').write_text('\n'.join(report),encoding='utf-8')
    state('completed' if not failures else 'completed_with_technical_failures')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
original_prompt=reviewer.PROMPT;original_counter=reviewer.COUNTER_PROMPT
if __name__=='__main__':main()
