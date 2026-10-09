import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import dedup


def record(identifier,name='',english='',path='root/a',definition='',en_definition=''):
    return {'id':identifier,'name':name,'knowledge_point':english,'main_tags':path,
            'definition':definition,'en_definition':en_definition,'description':'',
            'en_description':'','related_tags':[],'source':'original'}


def partition(task,winner=None):
    ids = [r['id'] for r in task['members']]
    return {'group_id':task['group_id'],'clusters':([{'member_ids':ids,
            'canonical_record_id':winner or ids[0],'reason':'same concept; cleaner definition'}]
            if winner != 'separate' else []),'singletons':(ids if winner=='separate' else [])}


class DedupTests(unittest.TestCase):
    def test_longer_definition_preserves_original_record(self):
        rows=[record('1','a',definition='short'),record('2','a',definition='a longer definition')]
        before=copy.deepcopy(rows)
        kept,removed=dedup.length_dedup(rows,{})
        self.assertEqual(kept,[before[1]])
        self.assertEqual(removed[0]['kept_id'],'2')
        self.assertEqual(rows,before)

    def test_zh_length_has_priority_over_en_length(self):
        rows=[record('1','a',definition='xx'),record('2','a',definition='x',en_definition='z'*99)]
        self.assertEqual(dedup.length_dedup(rows,{})[0],[rows[0]])

    def test_en_definition_breaks_tie(self):
        rows=[record('1','a'),record('2','a',en_definition='long')]
        self.assertEqual(dedup.length_dedup(rows,{})[0],[rows[1]])

    def test_stable_tie_and_output_order(self):
        rows=[record('1','a'),record('2','b'),record('3','a')]
        self.assertEqual(dedup.length_dedup(rows,{})[0],rows[:2])

    def test_different_branches_never_merge(self):
        rows=[record('1','a',path='root/a'),record('2','a',path='root/b')]
        self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))
        self.assertEqual(dedup.candidate_groups(rows,'physics',{}),[])

    def test_case_and_edge_spaces_only(self):
        rows=[record('1',english=' Example '),record('2',english='example')]
        self.assertEqual(len(dedup.length_dedup(rows,{})[0]),1)
        self.assertEqual(len(dedup.length_dedup([record('1','a b'),record('2','ab')],{})[0]),2)

    def test_empty_titles_or_paths_are_not_grouped(self):
        rows=[record('1'),record('2'),record('3','a',path=''),record('4','a',path='')]
        self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))

    def test_whitespace_only_paths_are_not_grouped_or_removed(self):
        rows=[record('1','same',path='   '),record('2','same',path='   ')]
        self.assertEqual(dedup.candidate_groups(rows,'physics',{}),[])
        self.assertEqual(dedup.length_dedup(rows,{}),(rows,[]))

    def test_missing_optional_fields_and_nulls_stay_original(self):
        rows=[{'id':'1','name':'a','main_tags':'root/a','definition':None},
              {'id':'2','name':'a','main_tags':'root/a','en_definition':'long'}]
        self.assertEqual(dedup.length_dedup(rows,{})[0],[rows[1]])

    def test_duplicate_ids_fail_even_with_different_titles(self):
        with self.assertRaises(ValueError):
            dedup.length_dedup([record('1','a'),record('1','b')],{})

    def test_explicit_path_alias_without_output_rewrite(self):
        rows=[record('1','a',path='root > a'),record('2','a')]
        self.assertEqual(dedup.length_dedup(rows,{'root > a':'root/a'})[0],[rows[0]])

    def test_cyclic_alias_is_rejected(self):
        with self.assertRaises(ValueError): dedup.canonical_path('a',{'a':'b','b':'a'})

    def test_length_does_not_propagate_removed_bilingual_aliases(self):
        rows=[record('1','a','x'),record('2','b','y'),record('3','a','y')]
        self.assertEqual(dedup.length_dedup(rows,{})[0],rows[:2])

    def test_model_can_choose_shorter_higher_quality_original(self):
        rows=[record('1','a',definition='x'*99),record('2','a',definition='short precise')]
        task=dedup.candidate_groups(rows,'physics',{})[0]
        result=dedup.validate_partition(partition(task,'2'),task,{})
        self.assertEqual(result['clusters'][0]['canonical_record_id'],'2')

    def test_model_can_keep_homonyms_separate(self):
        task=dedup.candidate_groups([record('1','a'),record('2','a')],'physics',{})[0]
        self.assertEqual(dedup.validate_partition(partition(task,'separate'),task,{})['singletons'],['1','2'])

    def test_transitive_only_model_merge_is_rejected(self):
        rows=[record('1','a','x'),record('2','b','y'),record('3','a','y')]
        task=dedup.candidate_groups(rows,'physics',{})[0]
        with self.assertRaises(ValueError): dedup.validate_partition(partition(task,'1'),task,{})
        self.assertEqual(dedup.validate_partition(partition(task,'3'),task,{})['clusters'][0]['canonical_record_id'],'3')

    def test_wrong_group_and_missing_member_are_rejected(self):
        task=dedup.candidate_groups([record('1','a'),record('2','a')],'physics',{})[0]
        bad=partition(task); bad['group_id']='wrong'
        with self.assertRaises(ValueError): dedup.validate_partition(bad,task,{})
        bad=partition(task); bad['clusters'][0]['member_ids']=['1','bogus']
        with self.assertRaises(ValueError): dedup.validate_partition(bad,task,{})

    def test_declared_source_text_is_complete(self):
        row=record('1','a',definition='d'*10000)
        task={'group_id':'g','subject':'physics','main_tags':'root/a','members':[row,record('2','a')]}
        self.assertIn(row['definition'],dedup.user_prompt(task))

    def test_unknown_and_repeated_partition_members_are_rejected(self):
        task=dedup.candidate_groups([record('1','a'),record('2','a')],'physics',{})[0]
        bad=partition(task); bad['singletons']=['1']
        with self.assertRaises(ValueError): dedup.validate_partition(bad,task,{})
        bad=partition(task); bad['clusters'][0]['canonical_record_id']='bogus'
        with self.assertRaises(ValueError): dedup.validate_partition(bad,task,{})

    def test_length_matches_previous_algorithm_on_random_cases(self):
        import random
        from legacy_rules import merge_records
        rng=random.Random(20261008)
        for iteration in range(30):
            rows=[record(str(i),rng.choice(['a','b','c','']),rng.choice(['x','y','z','']),
                         rng.choice(['root/a','root/b','']),definition='d'*rng.randrange(8),
                         en_definition='e'*rng.randrange(8)) for i in range(80)]
            expected,allocations,_=merge_records('full',rows,[])
            self.assertEqual(dedup.length_dedup(rows,{})[0],expected)
            self.assertEqual(allocations,[])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.input=self.root/'input.json'
        self.rows=[record('1','a',definition='longer wrong'),record('2','a',definition='precise'),record('3','b')]
        self.input.write_text(json.dumps(self.rows),encoding='utf-8')
    def tearDown(self): self.temp.cleanup()
    def config(self,**changes):
        values={'inputs':[self.input],'out':self.root/'out','subject':'physics','mode':'llm',
                'api_url':'http://127.0.0.1:1','model':'test','workers':2,'retries':2,
                'timeout':1,'max_tokens':4096,'max_members':100,'max_input_chars':100000,
                'resume':False,'api_key_env':None,'path_aliases':None}
        return dedup.Config(**{**values,**changes})
    def test_rule_end_to_end_and_independent_verification(self):
        summary=dedup.execute(self.config(mode='length'))
        self.assertEqual(summary['removed_records'],1)
        self.assertTrue(dedup.verify_run(self.root/'out')['original_payloads_unchanged'])
    def test_model_end_to_end_no_original_field_changes(self):
        calls=[]
        def client(task,cfg): calls.append(task); return partition(task,'2')
        summary=dedup.execute(self.config(),client)
        self.assertEqual(len(calls),1)
        self.assertEqual(summary['removed_records'],1)
        self.assertEqual(json.loads((self.root/'out/retained.json').read_text(encoding='utf-8')),self.rows[1:])
    def test_one_initial_call_plus_two_retries(self):
        calls=[]
        def client(task,cfg):
            calls.append(1)
            if len(calls)<3: raise TimeoutError('offline simulated failure')
            return partition(task,'2')
        summary=dedup.execute(self.config(),client)
        self.assertEqual(len(calls),3)
        self.assertEqual(summary['removed_records'],1)
    def test_all_failed_candidates_are_kept_and_reviewed(self):
        def client(task,cfg): raise TimeoutError('offline simulated failure')
        summary=dedup.execute(self.config(retries=0),client)
        self.assertEqual(summary['removed_records'],0)
        self.assertEqual(summary['technical_failure_groups'],1)
        self.assertEqual(summary['review_records'],2)
    def test_malformed_result_is_retried_then_preserved(self):
        summary=dedup.execute(self.config(retries=0),lambda task,cfg:{})
        self.assertEqual(summary['technical_failure_groups'],1)
        self.assertEqual(summary['retained_records'],3)
    def test_oversized_group_is_not_truncated_or_called(self):
        def client(task,cfg): self.fail('oversized group was sent')
        summary=dedup.execute(self.config(max_input_chars=10),client)
        self.assertEqual(summary['oversized_groups'],1)
        self.assertEqual(summary['retained_records'],3)
    def test_resume_reuses_verified_success(self):
        dedup.execute(self.config(),lambda task,cfg:partition(task,'2'))
        def client(task,cfg): self.fail('completed group called again')
        summary=dedup.execute(self.config(resume=True),client)
        self.assertEqual(summary['reused_groups'],1)
    def test_resume_retries_failure(self):
        def fail(task,cfg): raise TimeoutError()
        dedup.execute(self.config(retries=0),fail)
        summary=dedup.execute(self.config(resume=True),lambda task,cfg:partition(task,'2'))
        self.assertEqual(summary['technical_failure_groups'],0)
        self.assertEqual(summary['removed_records'],1)
    def test_changed_source_or_model_cannot_resume(self):
        dedup.execute(self.config(),lambda task,cfg:partition(task,'2'))
        with self.assertRaises(ValueError): dedup.execute(self.config(resume=True,model='other'))
        self.input.write_text('[]',encoding='utf-8')
        with self.assertRaises(ValueError): dedup.execute(self.config(resume=True))
    def test_no_silent_output_overwrite(self):
        dedup.execute(self.config(mode='length'))
        with self.assertRaises(FileExistsError): dedup.execute(self.config(mode='length'))
    def test_verifier_detects_tampered_payload(self):
        dedup.execute(self.config(mode='length'))
        p=self.root/'out/retained.json'; rows=json.loads(p.read_text(encoding='utf-8'))
        rows[0]['source']='changed'; p.write_text(json.dumps(rows),encoding='utf-8')
        with self.assertRaises(ValueError): dedup.verify_run(self.root/'out')
    def test_jsonl_multiple_inputs_preserve_extra_metadata(self):
        self.rows[0]['extra']={'a':[1,True,None]}
        self.input.write_text(json.dumps(self.rows),encoding='utf-8')
        second=self.root/'second.jsonl'; second.write_text(json.dumps(record('4','c'))+'\n',encoding='utf-8')
        summary=dedup.execute(self.config(mode='length',inputs=[self.input,second]))
        self.assertEqual(summary['input_records'],4)
        kept=json.loads((self.root/'out/retained.json').read_text(encoding='utf-8'))
        self.assertEqual(kept[0],self.rows[0])
    def test_empty_input_is_valid(self):
        self.input.write_text('[]',encoding='utf-8')
        self.assertEqual(dedup.execute(self.config(mode='length'))['retained_records'],0)

    def test_whitespace_path_run_finishes_without_removing_records(self):
        rows=[record('1','same',path=' \t '),record('2','same',path=' \t ')]
        self.input.write_text(json.dumps(rows),encoding='utf-8')
        summary=dedup.execute(self.config(mode='length'))
        self.assertEqual(summary['retained_records'],2)
        self.assertTrue(dedup.verify_run(self.root/'out')['original_payloads_unchanged'])

    def test_group_lock_prevents_simultaneous_writers(self):
        out=self.root/'lock'; out.mkdir()
        with dedup.run_lock(out):
            with self.assertRaises(OSError):
                with dedup.run_lock(out): self.fail('Second writer acquired lock')

    def test_changed_alias_file_cannot_resume(self):
        aliases=self.root/'aliases.json'; aliases.write_text('{}',encoding='utf-8')
        dedup.execute(self.config(mode='length',path_aliases=aliases))
        aliases.write_text('{"a":"b"}',encoding='utf-8')
        with self.assertRaises(ValueError):
            dedup.execute(self.config(mode='length',path_aliases=aliases,resume=True))

    def test_credentials_are_not_written_into_run_metadata(self):
        with self.assertRaises(ValueError):
            dedup.execute(self.config(api_url='http://user:secret@localhost'))
        self.assertFalse((self.root/'out').exists())

    def test_http_request_uses_both_languages_and_exact_source(self):
        task=dedup.candidate_groups(self.rows,'physics',{})[0]
        response=SimpleNamespace(read=lambda _:json.dumps({'choices':[{'finish_reason':'stop',
                                 'message':{'content':json.dumps(partition(task,'2'))}}]}).encode())
        class Context:
            def __enter__(self): return response
            def __exit__(self,*args): pass
        with patch('dedup.urllib.request.urlopen',return_value=Context()) as call:
            raw=dedup.request_model(task,self.config())
            request=call.call_args.args[0]; payload=json.loads(request.data)
            self.assertEqual(payload['model'],'test')
            self.assertEqual(json.loads(payload['messages'][1]['content'])['members'],task['members'])
            self.assertTrue(request.full_url.endswith('/v1/chat/completions'))
            self.assertEqual(raw['group_id'],task['group_id'])

    def test_http_truncation_is_not_accepted(self):
        task=dedup.candidate_groups(self.rows,'physics',{})[0]
        response=SimpleNamespace(read=lambda _:b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}')
        class Context:
            def __enter__(self): return response
            def __exit__(self,*args): pass
        with patch('dedup.urllib.request.urlopen',return_value=Context()):
            with self.assertRaises(ValueError): dedup.request_model(task,self.config())

    def test_abnormal_finish_reason_cannot_delete_records(self):
        task=dedup.candidate_groups(self.rows,'physics',{})[0]
        for finish in ('content_filter','tool_calls',None):
            with self.subTest(finish=finish):
                response=SimpleNamespace(read=lambda _,reason=finish:json.dumps({'choices':[{
                    'finish_reason':reason,'message':{'content':json.dumps(partition(task,'2'))}}]}).encode())
                class Context:
                    def __enter__(self): return response
                    def __exit__(self,*args): pass
                with patch('dedup.urllib.request.urlopen',return_value=Context()):
                    with self.assertRaises(ValueError): dedup.request_model(task,self.config())

    def test_more_than_two_retries_is_rejected_before_any_call(self):
        with self.assertRaises(ValueError):
            dedup.execute(self.config(retries=3),lambda task,cfg:self.fail('Invalid retry budget was executed'))
        self.assertFalse((self.root/'out').exists())

    def test_filtered_response_preserves_all_records_for_review(self):
        task=dedup.candidate_groups(self.rows,'physics',{})[0]
        response=SimpleNamespace(read=lambda _:json.dumps({'choices':[{'finish_reason':'content_filter',
                                 'message':{'content':json.dumps(partition(task,'2'))}}]}).encode())
        class Context:
            def __enter__(self): return response
            def __exit__(self,*args): pass
        with patch('dedup.urllib.request.urlopen',return_value=Context()):
            summary=dedup.execute(self.config(retries=0))
        self.assertEqual(summary['retained_records'],3)
        self.assertEqual(summary['removed_records'],0)
        self.assertEqual(summary['review_records'],2)

    def test_length_verifier_rejects_wrong_original_winner(self):
        dedup.execute(self.config(mode='length'))
        out=self.root/'out'
        dedup.write_json(out/'retained.json',self.rows[1:]); dedup.write_jsonl(out/'retained.jsonl',self.rows[1:])
        dedup.write_jsonl(out/'removed.jsonl',[self.rows[0]])
        audit=dedup.read_rows(out/'duplicate_audit.jsonl'); audit[0].update(removed_id='1',kept_id='2')
        dedup.write_jsonl(out/'duplicate_audit.jsonl',audit)
        with self.assertRaises(ValueError): dedup.verify_run(out)

    def test_model_verifier_detects_checkpoint_output_disagreement(self):
        dedup.execute(self.config(),lambda task,cfg:partition(task,'2'))
        out=self.root/'out'
        checkpoint=next((out/'checkpoints').glob('*.json'))
        saved=json.loads(checkpoint.read_text(encoding='utf-8'))
        saved['partition']['clusters'][0]['canonical_record_id']='1'
        dedup.write_json(checkpoint,saved)
        with self.assertRaises(ValueError): dedup.verify_run(out)

    def test_oversize_can_be_recovered_after_budget_increase(self):
        dedup.execute(self.config(max_input_chars=10),lambda task,cfg:self.fail('unexpected request'))
        summary=dedup.execute(self.config(resume=True),lambda task,cfg:partition(task,'2'))
        self.assertEqual(summary['oversized_groups'],0)
        self.assertEqual(summary['removed_records'],1)

    def test_batch_keeps_subjects_and_repeated_cross_subject_ids_separate(self):
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps({'subjects':[{'subject':'a','slug':'a','inputs':['input.json']},
                                                    {'subject':'b','slug':'b','inputs':['input.json']}]}),encoding='utf-8')
        args=SimpleNamespace(command='batch',manifest=manifest,out=self.root/'batch',mode='length',
                             api_url='',model='',api_key_env=None,workers=1,retries=2,timeout=10,
                             max_tokens=4096,max_members=100,max_input_chars=60000,resume=False)
        result=dedup.run_batch(args)
        self.assertEqual(result['subjects'],2)
        self.assertEqual(result['removed_records'],2)
        self.assertTrue((args.out/'a/retained.json').exists())
        args.resume=True
        self.assertEqual(dedup.run_batch(args)['retained_records'],4)


if __name__=='__main__': unittest.main()
