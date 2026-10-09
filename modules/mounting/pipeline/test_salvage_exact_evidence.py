import json
import tempfile
import unittest
from pathlib import Path
from salvage_exact_evidence import salvage,process


def attempt(obj):
    return {'raw':{'choices':[{'finish_reason':'stop','message':{'content':json.dumps(obj,ensure_ascii=False)}}]}}


class SalvageExactEvidenceTest(unittest.TestCase):
    def test_recovers_typo_in_request_id_when_evidence_matches(self):
        item={'request_id':'a22960885c4b0d726eb84452','name':'蒋系','main_tags':'历史学/近代史'}
        obj={'request_id':'a2296085c4b0d726eb84452','judgment':'reasonable','problem_type':'none',
             'reason':'词条属于近代史','meaning_status':'clear','needs_boundary':False,
             'support_evidence':[{'field':'name','text':'蒋系'}],'conflict_evidence':[]}
        result,checks=salvage({'attempts':[attempt(obj)]},item)
        self.assertEqual(result['judgment'],'reasonable')
        self.assertEqual(checks[0]['corrected_request_id'],'a2296085c4b0d726eb84452')

    def test_does_not_rebind_unrelated_request_id(self):
        item={'request_id':'a22960885c4b0d726eb84452','name':'蒋系','main_tags':'历史学/近代史'}
        obj={'request_id':'completely-unrelated','judgment':'reasonable','problem_type':'none',
             'reason':'词条属于近代史','meaning_status':'clear','needs_boundary':False,
             'support_evidence':[{'field':'name','text':'蒋系'}],'conflict_evidence':[]}
        result,_=salvage({'attempts':[attempt(obj)]},item)
        self.assertIsNone(result)

    def test_process_reads_cli_samples_when_input_jsonl_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source';source.mkdir()
            item={'request_id':'r1','name':'蒋系','main_tags':'历史学/近代史'}
            (source/'samples.jsonl').write_text(json.dumps(item,ensure_ascii=False)+'\n',encoding='utf-8')
            obj={'request_id':'r1','judgment':'reasonable','problem_type':'none','reason':'近代史',
                 'meaning_status':'clear','needs_boundary':False,
                 'support_evidence':[{'field':'name','text':'蒋系'},{'field':'definition','text':'并不存在'}],
                 'conflict_evidence':[]}
            response={'request_id':'r1','final':{'judgment':'technical_failure'},
                      'first':{'attempts':[attempt(obj)]},'second':None}
            (source/'responses.jsonl').write_text(json.dumps(response,ensure_ascii=False)+'\n',encoding='utf-8')
            process(source,root/'out')
            report=json.loads((root/'out'/'report.json').read_text())
            self.assertEqual(report['recovered_by_verdict'],{'reasonable':1})


if __name__=='__main__':unittest.main()
