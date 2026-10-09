"""Read-only extraction of all first-layer drop/review records for a workbook."""
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT=Path('/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922/name_cleaning')
OUT=Path('/home/wangqiyuan/work/name_cleaning_all_decisions_20260923')
SUBJECTS=[('mechanical_engineering','机械'),('architecture','建筑'),('history','历史学'),
          ('literature','文学'),('philosophy','哲学'),('economy','经济学'),('military','军事学'),
          ('art','艺术学'),('civil_engineering','土木'),('management','管理学'),
          ('sociology','社会学'),('education','教育学')]
FIELDS=[('definition','定义'),('description','描述'),('en_definition','英文定义'),('en_description','英文描述')]

def explanation(record):
    return '\n'.join(label+'：'+value for key,label in FIELDS
                     if isinstance((value:=record.get(key)),str) and value.strip())

def main():
    OUT.mkdir(exist_ok=False)
    report={}
    for decision,filename in [('drop','剔除.jsonl'),('review','待复核.jsonl')]:
        count=Counter();maximum=0;largest=None
        with (OUT/filename).open('w',encoding='utf-8') as dest:
            for subject,zh in SUBJECTS:
                source=ROOT/subject/f'llm_name_title_format_{decision}_full.jsonl'
                expected=json.loads((ROOT/subject/'verified.json').read_text())['counts'][decision]
                with source.open(encoding='utf-8') as stream:
                    for line in stream:
                        if not line.strip():continue
                        r=json.loads(line);q=r['llm_name_title_format_quality']
                        if q['decision']!=decision:raise ValueError((subject,r['id'],'decision mismatch'))
                        row=dict(subject=zh,id=str(r['id']),name=r.get('name',''),
                                 knowledge_point=r.get('knowledge_point',''),reason=q.get('reason',''),
                                 definition_explanation=explanation(r))
                        size=len(row['definition_explanation'])
                        if size>maximum:maximum=size;largest=(subject,row['id'])
                        dest.write(json.dumps(row,ensure_ascii=False)+'\n');count[subject]+=1
                if count[subject]!=expected:raise ValueError((subject,decision,count[subject],expected))
        data=(OUT/filename).read_bytes()
        report[decision]=dict(rows=sum(count.values()),by_subject=dict(count),max_explanation_chars=maximum,
                              longest_record=largest,sha256=hashlib.sha256(data).hexdigest())
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
