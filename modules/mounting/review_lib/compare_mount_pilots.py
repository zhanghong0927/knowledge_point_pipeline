"""Reconcile same-sample pilot comparison without changing judged datasets."""
import sys,json,hashlib
from pathlib import Path
from collections import Counter
from mount_pilot_v2 import api_view,validate_response
from run_rule_checks import dump

def read_rows(path): return list(map(json.loads,path.read_text(encoding='utf-8').splitlines()))

def main():
    previous,current=map(Path,sys.argv[1:3])
    a=(previous/'samples.jsonl').read_bytes(); b=(current/'samples.jsonl').read_bytes()
    assert a==b,'samples changed'
    inputs=read_rows(current/'samples.jsonl'); expected={r['request_id']:r for r in inputs}
    assert len(expected)==1200
    old_rows=read_rows(previous/'results.jsonl'); new_rows=read_rows(current/'results.jsonl')
    old={r['request_id']:r for r in old_rows}; new={r['request_id']:r for r in new_rows}
    assert len(old)==len(old_rows) and len(new)==len(new_rows)
    assert set(old)<=set(expected) and set(new)<=set(expected)
    for key,r in new.items(): validate_response({'results':[r]},[expected[key]])
    changed=[]; transitions=Counter(); labels={r['subject_code']:r['subject'] for r in inputs}
    for key,item in expected.items():
        before=old.get(key,{}).get('judgment','api_failed'); after=new.get(key,{}).get('judgment','api_failed')
        transitions[(before,after)]+=1
        if before!=after: changed.append(dict(request_id=key,subject=item['subject'],name=item['name'],main_tags=item['main_tags'],previous=old.get(key),current=new.get(key)))
    with (current/'changed_decisions.jsonl').open('w',encoding='utf-8') as f:
        for row in changed: f.write(json.dumps(row,ensure_ascii=False)+'\n')
    with (current/'api_inputs.jsonl').open('w',encoding='utf-8') as f:
        for row in inputs: f.write(json.dumps(api_view(row),ensure_ascii=False)+'\n')
    totals_old=Counter(old.get(k,{}).get('judgment','api_failed') for k in expected)
    totals_new=Counter(new.get(k,{}).get('judgment','api_failed') for k in expected)
    verification=json.loads((current/'verification.json').read_text())
    assert verification['valid']==len(new) and verification['failed']==1200-len(new)
    summary=json.loads((current/'summary.json').read_text())
    for row in summary:
        counts=Counter(new.get(k,{}).get('judgment','api_failed') for k,item in expected.items() if item['subject_code']==row['subject'])
        assert dict(counts)==row['counts'] and sum(counts.values())==100
    comparison=dict(samples_identical=True,samples_sha256=hashlib.sha256(a).hexdigest(),previous=dict(totals_old),current=dict(totals_new),changed=len(changed),transitions={a+' -> '+b:c for (a,b),c in transitions.items()},no_outside_subject=True)
    dump(current/'comparison.json',comparison)
    lines=['# 挂载合理性试跑v2：以完整节点路径为准','',
        '与v1使用完全相同的1200条样本（12学科各100条）、Qwen3.8-27B、并发16。输入样本字节一致，模型返回ID、证据摘录及计数已核验。',
        '本轮去掉独立学科范围判断及学科标签，不允许outside_subject。分类路径视为有效，只判断词条是否适合当前节点；结合上级节点消歧，不要求最优位置。',
        '下表为模型样本判定，不是人工确认的挂载准确率。未改动交付数据；技术失败不计作内容错误。','',
        '| 判定 | v1 | v2 |','|---|---:|---:|']
    for code,label in [('reasonable','合理'),('unreasonable','不合理'),('uncertain','不确定'),('api_failed','技术失败')]: lines.append(f'| {label} | {totals_old[code]} | {totals_new[code]} |')
    lines+=['','## 分学科结果','', '| 学科 | v1不合理 | v2合理 | v2不合理 | v2不确定 | 技术失败 |','|---|---:|---:|---:|---:|---:|']
    for row in summary:
        old_bad=sum(old.get(k,{}).get('judgment')=='unreasonable' for k,item in expected.items() if item['subject_code']==row['subject'])
        lines.append('| '+' | '.join(map(str,[row['label'],old_bad]+[row['counts'].get(k,0) for k in ('reasonable','unreasonable','uncertain','api_failed')]))+' |')
    translations={'reasonable':'合理','unreasonable':'不合理','uncertain':'不确定','api_failed':'技术失败'}
    lines+=['','## 针对上一轮案例的回看','']
    names={'薛瑞红','等级赛','国家冰球联赛','砂子炉','旋锤式碎石机','条带','廉锦枫'}
    for key,item in expected.items():
        if item['name'] not in names: continue
        before=translations[old.get(key,{}).get('judgment','api_failed')]; after=translations[new.get(key,{}).get('judgment','api_failed')]
        lines+=['- '+item['subject']+'｜'+item['name']+'：'+before+' → '+after+'。'+new.get(key,{}).get('reason','未取得有效结论')]
    lines+=['','## 边界与明细','',
        '判定变化不自动代表正确率提升；应回看理由是否符合既定节点边界。机械、土木仍缺对应分类树，主要依据路径文本。定义缺失、名称多义、节点边界不明确时仍可能误判。',
        '完整输入与判定：review_details.jsonl；变化明细：changed_decisions.jsonl；仅模型输入字段：api_inputs.jsonl；计数对比：comparison.json；原始API返回：raw_api.jsonl、single_retry_raw.jsonl。']
    (current/'v2试跑对比报告.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(comparison,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
