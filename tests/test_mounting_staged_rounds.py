"""Regression checks for separately scheduled native mounting transport rounds."""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import request

from test_mounting_bridge import bridge, fake_routes, fake_review, fixture


NATIVE = bridge.NATIVE / 'scripts/hierarchical-knowledge-labeling-beam-v3.py'


def native_module():
    spec = importlib.util.spec_from_file_location('staged_mounting_native', NATIVE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StagedNativeRouteTests(unittest.TestCase):
    def test_format_failures_stop_after_third_round_and_remain_explicit_in_export(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_=fixture(Path(directory),mount_review_mode='off');native=native_module();calls=[]
            def run_native(command,check=True,env=None):
                def malformed(record,*unused):
                    calls.append(record['record_id']);raise ValueError('invalid model JSON format')
                with patch.object(sys,'argv',command[1:]),patch.object(native,'route_record',side_effect=malformed):native.main()
            with patch.object(bridge.subprocess,'run',side_effect=run_native):
                for phase in (1,2,3):
                    args.only_round=phase;args.finalize_only=False;bridge.route(args)
                status=bridge.read(args.out/'route/ORCHESTRATION_STATUS.json')
                self.assertEqual(status['state'],'ready_to_finalize')
                self.assertEqual(status['pending_format'],3)
                args.only_round=None;args.finalize_only=True;bridge.route(args)
            report=bridge.export(args)
            self.assertEqual(len(calls),9)
            self.assertEqual(report['counts']['technical_failure'],3)
            self.assertEqual(report['technical_failure_breakdown']['route_response_format'],3)
            self.assertEqual(report['technical_failure_breakdown']['route_transport'],0)

    def test_real_json_and_structure_failures_retry_through_three_rounds_reusing_root_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_=fixture(Path(directory));target=args.out/'groups/mechanical_engineering'
            source=target/'one.jsonl';bridge.write_rows(source,bridge.rows(target/'input.jsonl')[:1])
            native=native_module();calls=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    wire=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    payload=json.loads(wire['messages'][1]['content']);calls.append(payload['current_node']['node_code'])
                    if len(calls)==2:content='{"candidates":['
                    elif len(calls)==3:content=json.dumps({'routing_decision':{},'candidates':['not-an-object']})
                    else:
                        content=json.dumps({'routing_decision':{'action':'descend','confidence':.95,'reason':'fixture'},
                            'candidates':[{'node_code':payload['candidates'][0]['node_code'],
                                           'absolute_confidence':.95,'local_probability':.95,'evidence':[]}]})
                    encoded=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':content}}]}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
            server=HTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            argv=['native','--input',str(source),'--output',str(target/'one_routed.jsonl'),
                  '--profile-dir',str(target/'profile'),'--model','fixture',
                  '--api-url',f'http://127.0.0.1:{server.server_port}/v1/chat/completions','--skip-preflight']
            try:
                for phase,workers in ((1,4),(2,2),(3,1)):
                    with patch.object(sys,'argv',argv+['--only-round',str(phase),'--workers',str(workers)]):native.main()
                    state=bridge.read(target/'one_routed.rounds.json')
                    self.assertEqual(state['pending_transport'],0)
                    self.assertEqual(state['pending_format'],0 if phase==3 else 1)
                self.assertEqual(len(calls),4)
                self.assertEqual(calls.count(calls[0]),1)
                self.assertEqual(len(list(target.glob('one_routed.requests/*.json'))),2)
                with patch.object(sys,'argv',argv+['--finalize-only']):native.main()
                self.assertEqual(bridge.rows(target/'one_routed.jsonl')[0]['knowledge_labeling']['status'],'ok')
                self.assertEqual(len(calls),4)
            finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def test_successful_root_request_is_not_repeated_after_child_502(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            target = args.out/'groups/mechanical_engineering'
            native = native_module()
            source = target/'one_record.jsonl'
            bridge.write_rows(source, bridge.rows(target/'input.jsonl')[:1])
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    payload = json.loads(wire['messages'][1]['content'])
                    level = payload['current_node']['node_code']
                    calls.append(level)
                    if len(calls) == 2:
                        self.send_response(502)
                        self.end_headers()
                        return
                    candidate = payload['candidates'][0]
                    answer = {'routing_decision': {'action': 'descend', 'confidence': .95, 'reason': '示例'},
                              'candidates': [{'node_code': candidate['node_code'],
                                              'absolute_confidence': .95, 'local_probability': .95,
                                              'evidence': []}]}
                    encoded = json.dumps({'choices': [{'finish_reason': 'stop',
                                                       'message': {'content': json.dumps(answer, ensure_ascii=False)}}]},
                                         ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            argv = ['native', '--input', str(source), '--output', str(target/'one_routed.jsonl'),
                    '--profile-dir', str(target/'profile'), '--model', 'literal-fake',
                    '--api-url', f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                    '--workers', '1', '--skip-preflight', '--retries', '0']
            try:
                with patch.object(sys, 'argv', argv+['--only-round', '1']):
                    native.main()
                self.assertEqual(len(calls), 2)
                cached = next(target.glob('one_routed.requests/*.json'))
                saved = cached.read_bytes()
                cached.unlink()
                with patch.object(sys, 'argv', argv+['--only-round', '2']):
                    with self.assertRaisesRegex(ValueError, 'request cache checksum changed'):
                        native.main()
                self.assertEqual(len(calls), 2)
                cached.write_bytes(saved)
                with patch.object(sys, 'argv', argv+['--only-round', '2']):
                    native.main()
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls.count(calls[0]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_tampered_route_checkpoint_is_rejected_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            target = args.out/'groups/mechanical_engineering'
            native = native_module()
            argv = ['native', '--input', str(target/'input.jsonl'), '--output', str(target/'routed.jsonl'),
                    '--profile-dir', str(target/'profile'), '--model', 'literal-fake',
                    '--api-url', 'http://127.0.0.1:9/v1/chat/completions', '--skip-preflight']
            with patch.object(sys, 'argv', argv+['--only-round', '1']), patch.object(native, 'route_record',
                    return_value=({'status': 'ok', 'decision': 'needs_review', 'token_usage': {}}, native.TokenUsage())):
                native.main()
            path = target/'routed.rounds.jsonl'
            path.write_text(path.read_text(encoding='utf-8').replace('needs_review', 'accepted_leaf', 1), encoding='utf-8')
            with patch.object(sys, 'argv', argv+['--only-round', '2']), patch.object(native, 'route_record',
                    side_effect=AssertionError('unexpected model request')):
                with self.assertRaisesRegex(ValueError, 'checkpoint.*changed|checksum'):
                    native.main()

    def test_bridge_route_phase_status_and_finalized_fastpath(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            native = native_module()
            calls = []

            def route_record(record, root, options, prompt):
                calls.append(record['record_id'])
                return {'status': 'ok', 'decision': 'needs_review', 'token_usage': {}}, native.TokenUsage()

            def run_native(command, check=True, env=None):
                with patch.object(sys, 'argv', command[1:]), patch.object(native, 'route_record', side_effect=route_record):
                    native.main()

            with patch.object(bridge.subprocess, 'run', side_effect=run_native):
                for phase in (1, 2, 3):
                    args.only_round = phase
                    args.finalize_only = False
                    bridge.route(args)
                    status = bridge.read(args.out/'route/ORCHESTRATION_STATUS.json')
                    self.assertEqual(status['completed_round'], phase)
                self.assertEqual(len(calls), 3)
                args.only_round = None
                args.finalize_only = True
                bridge.route(args)
            self.assertEqual(bridge.read(args.out/'route/ORCHESTRATION_STATUS.json')['state'], 'finalized')
            self.assertTrue((args.out/'ROUTED.json').exists())
            args.finalize_only = False
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('unexpected route request')):
                bridge.route(args)

    def test_rounds_retry_transport_and_format_without_repeating_valid_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            target = args.out / 'groups/mechanical_engineering'
            native = native_module()
            output = target / 'routed.jsonl'
            argv = ['native', '--input', str(target / 'input.jsonl'), '--output', str(output),
                    '--profile-dir', str(target / 'profile'), '--model', 'literal-fake',
                    '--api-url', 'http://127.0.0.1:9/v1/chat/completions',
                    '--workers', '2', '--skip-preflight', '--retries', '0',
                    '--retry-failed-rounds', '0']
            called = []

            def first(record, root, options, prompt):
                called.append(record['record_id'])
                if record['id'] == '2':
                    raise RuntimeError('API request failed: TimeoutError: timed out')
                if record['id'] == '3':
                    raise ValueError('invalid model JSON format')
                return {'status': 'ok', 'decision': 'needs_review', 'token_usage': {}}, native.TokenUsage()

            with patch.object(sys, 'argv', argv + ['--only-round', '1']), patch.object(native, 'route_record', side_effect=first):
                native.main()
            self.assertEqual(len(called), 3)
            self.assertFalse(output.exists())

            retried = []

            def second(record, root, options, prompt):
                retried.append(record['record_id'])
                return {'status': 'ok', 'decision': 'accepted_leaf', 'token_usage': {}}, native.TokenUsage()

            with patch.object(sys, 'argv', argv + ['--only-round', '2']), patch.object(native, 'route_record', side_effect=second):
                native.main()
            self.assertEqual(len(retried), 2)
            with patch.object(sys, 'argv', argv + ['--only-round', '3']), patch.object(native, 'route_record', side_effect=AssertionError('unexpected model request')):
                native.main()
            with patch.object(sys, 'argv', argv + ['--finalize-only']), patch.object(native, 'route_record', side_effect=AssertionError('unexpected model request')):
                native.main()
            results = bridge.rows(output)
            self.assertEqual(len(results), 3)
            self.assertEqual([item['record_id'] for item in results], list(bridge.read(target / 'original_records.json')))
            self.assertEqual(sum(item['knowledge_labeling']['status'] == 'failed' for item in results), 0)
            self.assertTrue((target / 'routed.rounds.jsonl').exists())

    def test_format_retry_is_pending_even_with_zero_transport_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            args,_=fixture(Path(directory));native=native_module()
            def run_native(command,check=True,env=None):
                with patch.object(sys,'argv',command[1:]),patch.object(native,'route_record',
                      side_effect=ValueError('invalid model JSON format')):
                    native.main()
            args.only_round=1;args.finalize_only=False
            with patch.object(bridge.subprocess,'run',side_effect=run_native):bridge.route(args)
            status=bridge.read(args.out/'route/ORCHESTRATION_STATUS.json')
            self.assertEqual(status['state'],'awaiting_retry')
            self.assertEqual(status['pending_transport'],0)
            self.assertEqual(status['pending_format'],3)
            self.assertEqual(status['pending_retryable'],3)

    def test_invalid_response_structure_is_not_written_as_successful_cache(self):
        native=native_module()
        for value in ('not-an-object',{'routing_decision':{},'candidates':['bad-item']}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,'[Mm]odel.*format|[Rr]esponse.*format'):
                    native.validate_model_response(value)

    def test_valid_semantic_refusals_are_not_retryable(self):
        native=native_module()
        for decision in ('out_of_scope','insufficient_evidence','needs_review','accepted_leaf'):
            self.assertFalse(native.staged_retryable_failure({'knowledge_labeling':{
                'status':'ok','decision':decision,'error':'invalid model JSON format'}}))
        self.assertFalse(native.staged_retryable_failure({'knowledge_labeling':{'status':'failed','error':'HTTP 400: bad model'}}))
        self.assertTrue(native.staged_retryable_failure({'knowledge_labeling':{'status':'failed','error':"'str' object has no attribute 'get'"}}))

    def test_source_change_rejected_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            target = args.out / 'groups/mechanical_engineering'
            native = native_module()
            argv = ['native', '--input', str(target / 'input.jsonl'), '--output', str(target / 'routed.jsonl'),
                    '--profile-dir', str(target / 'profile'), '--model', 'literal-fake',
                    '--api-url', 'http://127.0.0.1:9/v1/chat/completions', '--skip-preflight', '--retries', '0']
            with patch.object(sys, 'argv', argv + ['--only-round', '1']), patch.object(native, 'route_record', side_effect=RuntimeError('timed out')):
                native.main()
            input_path = target / 'input.jsonl'
            input_path.write_text(input_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
            with patch.object(sys, 'argv', argv + ['--only-round', '2']), patch.object(native, 'route_record', side_effect=AssertionError('unexpected model request')):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    native.main()


class StagedReviewTests(unittest.TestCase):
    def test_tampered_review_checkpoint_is_rejected_before_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            fake_routes(args)
            args.only_round = 1
            args.finalize_only = False

            def response(reviewer, base, model, item, position, previous=None):
                value = fake_review(base, model, {**item, 'request_id': f'R{position:07d}'})
                value['request_id'] = item['request_id']
                value['first']['result']['request_id'] = item['request_id']
                value['final'] = {'judgment': 'reasonable'}
                value['transport_retryable'] = False
                return value

            with patch.object(bridge, 'staged_review_wire_item', side_effect=response):
                bridge.review(args)
            path = args.out/'review/review_rounds.jsonl'
            path.write_text(path.read_text(encoding='utf-8').replace('reasonable', 'unreasonable', 1), encoding='utf-8')
            args.only_round = None
            args.finalize_only = True
            with self.assertRaisesRegex(ValueError, 'checkpoint.*changed|checksum'):
                bridge.review(args)

    def test_review_counter_transport_retry_preserves_first_result_over_literal_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            fake_routes(args)
            item = bridge.review_plan(args.out, bridge.frozen(args.out))[0][0]
            reviewer = bridge.reviewer_module()
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    wire = json.loads(body['messages'][1]['content'])
                    calls.append(wire['request_id'])
                    if len(calls) == 2:
                        self.send_response(503)
                        self.end_headers()
                        return
                    unreasonable = len(calls) == 1
                    answer = {'request_id': wire['request_id'],
                              'judgment': 'unreasonable' if unreasonable else 'uncertain',
                              'problem_type': 'wrong_leaf' if unreasonable else 'insufficient_information',
                              'reason': '证据不够确定', 'meaning_status': 'clear', 'needs_boundary': False,
                              'support_evidence': [],
                              'conflict_evidence': ([{'field': 'name', 'text': wire['name']},
                                                     {'field': 'main_tags', 'text': wire['main_tags']}]
                                                    if unreasonable else [])}
                    payload = json.dumps({'choices': [{'finish_reason': 'stop',
                                                       'message': {'content': json.dumps(answer, ensure_ascii=False)}}]},
                                         ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'

            def transport(url, payload):
                req = request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                      headers={'Content-Type': 'application/json'})
                with request.urlopen(req, timeout=3) as response:
                    return json.load(response)

            try:
                with patch.object(reviewer, 'request_json', side_effect=transport):
                    first = bridge.staged_review_wire_item(reviewer, base, 'literal-fake', item, 1)
                    self.assertTrue(first['transport_retryable'])
                    self.assertEqual(len(calls), 2)
                    second = bridge.staged_review_wire_item(reviewer, base, 'literal-fake', item, 1, first)
                self.assertEqual(len(calls), 3)
                self.assertFalse(second['transport_retryable'])
                self.assertEqual(second['first'], first['first'])
                self.assertEqual(second['final']['judgment'], 'uncertain')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_review_retries_only_transport_and_reuses_successful_first_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = fixture(Path(directory))
            fake_routes(args)
            args.only_round = 1
            args.finalize_only = False
            seen = []

            def first(reviewer, base, model, item, position, previous=None):
                seen.append((1, item['request_id']))
                if item['name'] == '齿轮传动':
                    return {'request_id': item['request_id'], 'wire_request_id': f'R{position:07d}',
                            'first': {'result': None, 'failure_kind': 'transport', 'attempts': []},
                            'second': None, 'final': {'judgment': 'technical_failure'},
                            'transport_retryable': True}
                answer = fake_review(base, model, {**item, 'request_id': f'R{position:07d}'})
                answer['request_id'] = item['request_id']
                answer['first']['result']['request_id'] = item['request_id']
                answer['final'] = {'judgment': 'reasonable'}
                answer['transport_retryable'] = False
                return answer

            with patch.object(bridge, 'staged_review_wire_item', side_effect=first):
                bridge.review(args)
            self.assertEqual(bridge.read(args.out/'review/ORCHESTRATION_STATUS.json')['state'], 'awaiting_retry')
            self.assertFalse((args.out/'review_responses.jsonl').exists())
            initial = len(seen)
            args.only_round = 2

            def second(reviewer, base, model, item, position, previous=None):
                seen.append((2, item['request_id']))
                self.assertTrue(previous['transport_retryable'])
                recovered = fake_review(base, model, {**item, 'request_id': f'R{position:07d}'})
                recovered['request_id'] = item['request_id']
                recovered['first']['result']['request_id'] = item['request_id']
                recovered['final'] = {'judgment': 'uncertain'}
                recovered['transport_retryable'] = False
                return recovered

            with patch.object(bridge, 'staged_review_wire_item', side_effect=second):
                bridge.review(args)
            self.assertEqual(len(seen)-initial, sum(item['name'] == '齿轮传动' for item in bridge.rows(args.out/'review_input.jsonl')))
            args.only_round = 3
            with patch.object(bridge, 'staged_review_wire_item', side_effect=AssertionError('unexpected review request')):
                bridge.review(args)
            self.assertEqual(bridge.read(args.out/'review/ORCHESTRATION_STATUS.json')['state'], 'ready_to_finalize')
            args.only_round = None
            args.finalize_only = True
            with patch.object(bridge, 'api_settings', side_effect=AssertionError('unexpected HTTP')):
                bridge.review(args)
            self.assertTrue((args.out/'REVIEWED.json').exists())
            self.assertTrue((args.out/'review_responses.jsonl').exists())
            args.finalize_only = False
            with patch.object(bridge, 'api_settings', side_effect=AssertionError('unexpected HTTP')):
                bridge.review(args)


if __name__ == '__main__':
    unittest.main()
