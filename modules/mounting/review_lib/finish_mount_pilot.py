"""Retry only missing IDs individually, reconcile and package pilot results."""
import sys,json,shutil
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from mount_pilot import call_batch,validate_response
from run_rule_checks import dump,fingerprint

def main():
    out=Path(sys.argv[1]); config=json.loads((out/'run_config.json').read_text())
    items=list(map(json.loads,(out/'samples.jsonl').read_text().splitlines())); by_input={x['request_id']:x for x in items}
    existing=list(map(json.loads,(out/'results.jsonl').read_text().splitlines())); completed={x['request_id']:x for x in existing}
    assert len(completed)==len(existing)
    missing=[x for x in items if x['request_id'] not in completed]
    for name in ('summary.json','初版报告.md'): shutil.copy2(out/name,out/('first_pass_'+name))
    with (out/'single_retry_raw.jsonl').open('w',encoding='utf-8') as raw,(out/'results.jsonl').open('a',encoding='utf-8') as resultfile:
        with ThreadPoolExecutor(max_workers=config['workers']) as pool:
            futures=[pool.submit(call_batch,config['base'],config['model'],[x],'single:'+x['request_id']) for x in missing]
            for future in as_completed(futures):
                response=future.result(); raw.write(json.dumps(response,ensure_ascii=False)+'\n'); raw.flush()
                if response['status']=='ok':
                    for r in response['results']:
                        assert r['request_id'] not in completed
                        completed[r['request_id']]=r; resultfile.write(json.dumps(r,ensure_ascii=False)+'\n'); resultfile.flush()
                print('RETRY',response['batch'],response['status'],flush=True)
    for key,r in completed.items(): validate_response({'results':[r]},[by_input[key]])
    manifest=json.loads((out/'manifest.json').read_text()); summary=[]
    merged=[dict(**r,audit=completed.get(r['request_id']),api_status='ok' if r['request_id'] in completed else 'failed') for r in items]
    with (out/'review_details.jsonl').open('w',encoding='utf-8') as f:
        for r in merged: f.write(json.dumps(r,ensure_ascii=False)+'\n')
    for m in manifest:
        sub=[r for r in merged if r['subject_code']==m['subject']]
        counts=Counter(r['audit']['judgment'] if r['audit'] else 'api_failed' for r in sub)
        assert len(sub)==100
        unchanged=fingerprint(m['source']['path'])==m['source']; assert unchanged
        summary.append(dict(subject=m['subject'],label=m['label'],sample_count=len(sub),counts=dict(counts),input_unchanged=unchanged))
    dump(out/'summary.json',summary)
    dump(out/'verification.json',dict(expected=len(items),valid=len(completed),failed=len(items)-len(completed),input_unchanged=True,ids_unique=True,all_evidence_exact=True,initial_failed_records=len(missing)))
    lines=['# 12学科辞海知识点挂载审核初版','',
        '本次仅审核当前main_tags完整路径，不修改任何交付数据；不审核related_tags。',
        '每学科100条，按L1比例分层抽样，固定种子；缺树/未匹配路径使用路径首段作代理分组。',
        '模型：Qwen3.8-27B；并发16；每批5条。结构或引用验证失败后逐条补跑，仍失败单列。',
        '下表为模型对样本的判定，不是经人工确认的准确率。未进行全量审核。',
        '机械、土木本轮没有匹配的分类树，仅使用完整路径文本；其他学科使用匹配树的祖先链及已有节点说明。','',
        '| 学科 | 样本数 | 合理 | 不合理 | 不确定 | 技术失败 |','|---|---:|---:|---:|---:|---:|']
    for r in summary: lines.append('| '+' | '.join(map(str,[r['label'],r['sample_count']]+[r['counts'].get(k,0) for k in ('reasonable','unreasonable','uncertain','api_failed')]))+' |')
    lines+=['','## 模型判为不合理的样例（尚未人工确认）','']
    for m in manifest:
        examples=[r for r in merged if r['subject_code']==m['subject'] and r['audit'] and r['audit']['judgment']=='unreasonable'][:3]
        for r in examples: lines+=['- '+r['subject']+'｜'+str(r['name'])+'｜`'+str(r['main_tags'])+'`：'+r['audit']['reason']]
    (out/'初版报告.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
