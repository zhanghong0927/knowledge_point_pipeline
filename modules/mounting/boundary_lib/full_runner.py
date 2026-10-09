"""Durable sequential full audit -> paired remount -> blind review with coverage gates."""
import argparse,hashlib,json,os,subprocess,sys,time,traceback
from pathlib import Path
from mechanical_expansion import lines,cohort
from ab_workflow import write_json

def verify_ids(rows,expected,field):
    ids=[r[field] for r in rows]
    if len(ids)!=len(expected) or set(ids)!=expected:raise ValueError('missing, duplicate or unexpected IDs')

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.out.exists():raise ValueError('new run directory required; previous runs preserved')
    state_path=a.out.with_suffix('.state.json');started=time.time()
    def status(stage,**extra):
        value=dict(stage=stage,pid=os.getpid(),out=str(a.out),started=started,updated=time.time(),**extra)
        temp=state_path.with_suffix('.tmp');write_json(temp,value);temp.replace(state_path)
    script=Path(__file__).with_name('mechanical_expansion.py')
    def run(stage):
        status(stage)
        cmd=[sys.executable,'-u',str(script),stage,'--out',str(a.out)]
        if stage=='audit':cmd.append('--all')
        subprocess.run(cmd,check=True)
    try:
        run('audit')
        manifest=json.loads((a.out/'manifest.json').read_text());n=manifest['source_records']
        records=lines(a.out/'baseline/final.jsonl');expected={'M'+str(i+1).zfill(4) for i in range(n)}
        verify_ids([r['item'] for r in records],expected,'request_id')
        provenance=json.loads((a.out/'provenance.json').read_text());assert {r['source_index'] for r in provenance}==set(range(n))
        baseline=json.loads((a.out/'baseline/summary.json').read_text())
        selected=json.loads((a.out/'remount_cohort.json').read_text());ids={r['request_id'] for r in selected}
        assert ids=={r['item']['request_id'] for r in records if cohort(r['review']['judgment'])}
        report=['# 机械辞海全量管线结果','',f'全量原挂载审核：{n}条。', '', '原挂载判定：'+json.dumps(baseline['counts'],ensure_ascii=False)+'。','']
        technical=baseline['counts'].get('technical_failure',0)
        if selected:
            run('mount')
            for v in ['with_cards','without_cards']:
                output=lines(a.out/'remount'/(v+'.jsonl'));verify_ids(output,ids,'record_id')
            run('evaluate')
            details=lines(a.out/'effect_details.jsonl')
            for v in ['original','with_cards','without_cards']:verify_ids([r for r in details if r['variant']==v],ids,'record_id')
            summary=json.loads((a.out/'effect_summary.json').read_text())
            report+=['## 同一批未通过词条的最终盲审','', '| 方案 | 条数 | 合理 | 不合理 | 不确定 | 未挂载 | 复核技术失败 |','| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
            for v,label in [('original','原路径再次复核'),('with_cards','有卡片重挂'),('without_cards','无卡片重挂')]:
                m=summary['all'][v];c=m['counts'];technical+=c.get('technical_failure',0)
                report.append('| '+label+' | '+str(m['total'])+' | '+' | '.join(str(c.get(k,0)) for k in ['reasonable','unreasonable','uncertain','not_mounted','technical_failure'])+' |')
            mapping_failures=sum(r['knowledge_labeling']['status']!='ok' for v in ['with_cards','without_cards'] for r in lines(a.out/'remount'/(v+'.jsonl')))
            technical+=mapping_failures
            report+=['',f'两组重挂阶段技术失败合计：{mapping_failures}条；如非0，对应未挂载结果不能解释为内容不通过。']
        else:report+=['无语义未通过项，未执行重挂。']
        assert hashlib.sha256((a.out/'source_snapshot.json').read_bytes()).hexdigest()==manifest['source_sha256']
        current_unchanged=hashlib.sha256(Path(manifest['source']).read_bytes()).hexdigest()==manifest['source_sha256']
        report+=['','审核标准为当前完整路径是否合理，不要求唯一最优。所有评价来自同一Qwen模型，不等于专家金标准。初审与原路径复核的差异需作为模型波动考量，不能全部算作修复收益。','', '不确定和技术失败单列；后者没有作为语义错挂进入重挂。正式交付未覆盖。', '', f'源交付文件与本次快照仍一致：{current_unchanged}。','', '逐条结果见baseline/final.jsonl、remount两组JSONL、effect_details.jsonl；汇总见effect_summary.json。']
        (a.out/'全量审核与重挂对照结果.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
        write_json(a.out/'coverage_verification.json',dict(source_records=n,baseline_records=len(records),remount_records=len(ids),input_coverage_ok=True,current_source_unchanged=current_unchanged,technical_failure_occurrences=technical))
        status('completed_with_technical_failures' if technical else 'completed',source_records=n,remount_records=len(ids),technical_failure_occurrences=technical)
    except BaseException as e:
        status('failed',error=str(e));traceback.print_exc();raise

if __name__=='__main__':main()
