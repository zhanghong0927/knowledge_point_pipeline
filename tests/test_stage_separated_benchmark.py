import threading
import time
import unittest
import sys
import tempfile
import json
import argparse
import sqlite3
from pathlib import Path
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from stage_scheduling import run_bounded_phase, run_simple_phase
from staged_benchmark import (run_phase_sequence, dictionary_rounds,
                              cohort_transport_rounds, native_round_state,
                              technical_retry_refs, validate_staged_subject,
                              extraction_handoff_totals, final_output_totals,
                              freeze_important_clean_identity, stream_reconciliation_ready,
                              important_clean_transport_errors, mounting_dispositions, main)
from pipeline import select_tasks

DICTIONARY_SCRIPTS = Path(__file__).resolve().parents[1] / "modules/dictionary/classification/scripts"
sys.path.insert(0, str(DICTIONARY_SCRIPTS))
import classify_books
import run_structure_v6_1 as dictionary_transport
IMPORTANT_SCRIPTS = Path(__file__).resolve().parents[1] / "modules/important_books/scripts"
sys.path.insert(0, str(IMPORTANT_SCRIPTS))
import layout_model_validation as important_classifier
import modules.cleaning.scripts.model_clean as clean_transport


class StagedBenchmarkTests(unittest.TestCase):
    def test_empty_code_snapshot_rejected_before_gateway_or_status_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'SERVICE_PREFLIGHT.json').write_text(json.dumps({
                'api_url': 'http://127.0.0.1:9', 'model': 'fake'}))
            (root / 'CODE_SNAPSHOT.json').write_text('{}')
            with patch('staged_benchmark.require_executed_code_root'), \
                 patch('staged_benchmark.Gateway', side_effect=AssertionError('gateway started')):
                with self.assertRaisesRegex(ValueError, 'CODE_SNAPSHOT mismatch'):
                    main(['run', '--root', str(root), '--dictionary', str(root / 'd.json'),
                          '--important', str(root / 'i.json')])
            self.assertFalse((root / 'STATUS.json').exists())

    def test_valid_checkout_snapshot_in_wrong_run_code_rejected_before_gateway(self):
        from staged_benchmark import code_snapshot
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'code').mkdir()
            (root / 'SERVICE_PREFLIGHT.json').write_text(json.dumps({
                'api_url': 'http://127.0.0.1:9', 'model': 'fake'}))
            (root / 'CODE_SNAPSHOT.json').write_text(json.dumps(code_snapshot()))
            with patch('staged_benchmark.Gateway', side_effect=AssertionError('gateway started')):
                with self.assertRaisesRegex(ValueError, 'RUN/code'):
                    main(['run', '--root', str(root), '--dictionary', str(root / 'd.json'),
                          '--important', str(root / 'i.json')])
            self.assertFalse((root / 'STATUS.json').exists())

    def test_mounting_review_and_permanent_failure_count_as_unresolved_without_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            mount = home / 'run/05_mounting'
            mount.mkdir(parents=True)
            (mount / 'SUMMARY.json').write_text(json.dumps({'counts': {
                'mounted': 1, 'review': 2, 'technical_failure': 1,
                'not_mounted': 3}}), encoding='utf-8')
            (mount / 'REVIEWED.json').write_text(json.dumps({
                'format_or_semantic_unresolved': 1}), encoding='utf-8')
            (mount / 'route').mkdir()
            (mount / 'route/ORCHESTRATION_STATUS.json').write_text(json.dumps({
                'state': 'finalized', 'transport_unresolved': 0}), encoding='utf-8')
            transport, counts, format_unresolved = mounting_dispositions([{'home': str(home)}])
            self.assertEqual(transport, 0)
            self.assertEqual(counts['review'], 2)
            self.assertEqual(counts['technical_failure'], 1)
            self.assertEqual(format_unresolved, 1)

    def test_transient_transport_selector_excludes_permanent_http_and_local_io(self):
        from urllib.error import URLError
        for classifier in (dictionary_transport, important_classifier):
            for code in (400, 413):
                self.assertFalse(classifier.transient_transport(
                    HTTPError('http://127.0.0.1:9', code, 'permanent', {}, None)))
            for code in (408, 429, 500, 502, 503, 504):
                self.assertTrue(classifier.transient_transport(
                    HTTPError('http://127.0.0.1:9', code, 'transient', {}, None)))
            self.assertTrue(classifier.transient_transport(URLError('unavailable')))
            self.assertFalse(classifier.transient_transport(OSError('local file error')))

    def test_important_stream_retry_keeps_books_and_config_manifest_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            books, config = root / 'books.json', root / 'config.json'
            base, output = root / 'base', root / 'output'
            books.write_text('[]', encoding='utf-8')
            config.write_text(json.dumps({'api_url': 'http://127.0.0.1:9',
                                          'model': 'fake'}), encoding='utf-8')
            with patch.object(important_classifier, 'http_json',
                              return_value={'data': [{'id': 'fake'}]}):
                important_classifier.prepare_run(books, base, output, config, 1, 1, 1)
                before = important_classifier.read_json(output / 'manifest.json')
                important_classifier.run(base, output, config, 1)
            self.assertEqual(before, important_classifier.read_json(output / 'manifest.json'))
            books.write_text('[{"identifier":"changed"}]', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'books_changed'):
                important_classifier.verify(output)

    def test_plan_requires_no_paths_and_starts_no_gateway(self):
        from io import StringIO
        from contextlib import redirect_stdout
        output = StringIO()
        with patch('staged_benchmark.Gateway', side_effect=AssertionError('gateway started')):
            with redirect_stdout(output):
                self.assertEqual(main(['plan']), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['resource_profile']['phase_round_limits']['front'], [64, 16, 4])
        self.assertEqual(plan['resource_profile']['round_limits'], [128, 32, 8])
        self.assertEqual(plan['model_calls'], 0)

    def test_barrier_blocks_next_phase_until_every_book_finishes(self):
        events = []
        lock = threading.Lock()
        def transition(_url, limit, phase, **_kwargs):
            with lock:
                events.append(("transition", phase, limit))
            return {"phase": phase, "effective_limit": limit}
        def work(item):
            time.sleep(0.01 if item == 1 else 0.002)
            with lock:
                events.append(("done", item))
            return item
        phases = [("screening", 512, [1, 2], work, 2),
                  ("classification", 512, [3], work, 1)]
        run_phase_sequence("http://127.0.0.1:1234", phases, transition)
        self.assertLess(events.index(("done", 1)), events.index(("transition", "classification", 512)))
        self.assertLess(events.index(("done", 2)), events.index(("transition", "classification", 512)))

    def test_preparation_queue_is_bounded(self):
        in_prepare = 0
        peak = 0
        lock = threading.Lock()
        def prepare(item):
            nonlocal in_prepare, peak
            with lock:
                in_prepare += 1
                peak = max(peak, in_prepare)
            time.sleep(.002)
            with lock:
                in_prepare -= 1
            return item
        results = run_bounded_phase(range(20), prepare, lambda value: value * 2,
                                    prepare_workers=2, process_workers=3, max_pending=4)
        self.assertEqual(sorted(results), list(range(0, 40, 2)))
        self.assertLessEqual(peak, 2)

    def test_parallel_phase_results_follow_input_identity(self):
        def finish(item):
            time.sleep(.02 if item == "slow" else .001)
            return item
        self.assertEqual(run_simple_phase(["slow", "fast"], finish, 2), ["slow", "fast"])

    def test_dictionary_retries_are_cohort_wide_and_skip_resolved(self):
        events = []
        pending = {"a": 2, "b": 3}
        def run_round(book, number):
            events.append((book, number))
            if pending.get(book, 0) > number:
                return {"status": "awaiting_retry"}
            if book == "b" and number == 3:
                return {"status": "technical_failure"}
            return {"status": "ready_to_finalize"}
        def transition(_url, limit, phase, **_kwargs):
            events.append(("phase", number_from_name(phase), limit))
            return {}
        def number_from_name(name):
            return int(name.rsplit("_", 1)[1])
        results = dictionary_rounds(["a", "b", "c"], run_round, transition,
                                    "http://127.0.0.1:1", (512, 128, 32), workers=(3, 2, 1))
        self.assertEqual([x for x in events if x[0] == "phase"],
                         [("phase", 1, 512), ("phase", 2, 128), ("phase", 3, 32)])
        self.assertEqual([x for x in events if x[0] == "c"], [("c", 1)])
        self.assertEqual(results["b"]["status"], "technical_failure")

    def test_important_retry_only_technical_failure(self):
        rows = [{"ref": "a", "status": "classified", "primary_class": "N1"},
                {"ref": "b", "status": "needs_review"},
                {"ref": "c", "status": "technical_failed"},
                {"ref": "d", "status": "classified", "primary_class": "N3"}]
        self.assertEqual(technical_retry_refs(rows), ["c"])

    def test_cleaning_and_mounting_retries_share_cohort_phase_barriers(self):
        for label in ('dictionary_cleaning', 'mounting_route'):
            events = []
            def transition(_url, limit, phase):
                events.append(('phase', phase, limit))
            def run(item, number):
                events.append(('job', item, number))
                return {'status': 'awaiting_retry' if item == 'slow' and number == 1
                        else 'ready_to_finalize'}
            outcomes = cohort_transport_rounds(['slow', 'fast'], run, transition,
                'http://127.0.0.1:1', (512, 128, 32), (2, 2, 2), label)
            self.assertEqual([event for event in events if event[0] == 'phase'],
                [('phase', label + '_round_1', 512),
                 ('phase', label + '_round_2', 128)])
            self.assertEqual([event for event in events if event[:2] == ('job', 'fast')],
                             [('job', 'fast', 1)])
            self.assertEqual(outcomes['slow']['status'], 'ready_to_finalize')

    def test_native_round_state_rejects_missing_or_invalid_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / 'ORCHESTRATION_STATUS.json'
            self.assertEqual(native_round_state(status, 0)['status'], 'technical_failure')
            status.write_text(json.dumps({'state': 'awaiting_retry', 'next_round': 2}), encoding='utf-8')
            self.assertEqual(native_round_state(status, 0)['status'], 'awaiting_retry')
            self.assertEqual(native_round_state(status, 1)['status'], 'technical_failure')
            status.write_text(json.dumps({'state': 'awaiting_retry', 'next_round': 99}), encoding='utf-8')
            self.assertEqual(native_round_state(status, 0)['reason'], 'invalid_next_round')
            status.write_text(json.dumps({'state': 'completed'}), encoding='utf-8')
            self.assertEqual(native_round_state(status, 0)['status'], 'technical_failure')

    def test_staged_subject_requires_canonical_registered_taxonomy(self):
        registry = {'education': {'file': 'education_taxonomy.json'},
                    'hydraulic_engineering': {'file': None}}
        self.assertEqual(validate_staged_subject('education', registry), 'education')
        with self.assertRaises(ValueError):
            validate_staged_subject('教育学与体育科学', registry)
        with self.assertRaises(ValueError):
            validate_staged_subject('hydraulic_engineering', registry)
        with self.assertRaises(ValueError):
            validate_staged_subject('unknown', registry)

    def test_native_partial_extraction_is_reported_as_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            report = home / 'run/03_extraction/raw/HANDOFF.json'
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({'partial_books': 1, 'unresolved_items': 3}),
                              encoding='utf-8')
            self.assertEqual(extraction_handoff_totals([{'home': str(home)}]), (1, 3))

    def test_missing_final_export_is_null_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            job = {'job_id': 'subject', 'home': str(home)}
            missing = final_output_totals([job])
            self.assertIsNone(missing['final_retained_count'])
            self.assertEqual(missing['missing_exports'], ['subject'])
            output = home / 'run/06_dedup/retained.jsonl'
            output.parent.mkdir(parents=True)
            output.write_text('{"id":1}\n', encoding='utf-8')
            complete = final_output_totals([job])
            self.assertEqual(complete['final_retained_count'], 1)
            self.assertEqual(complete['missing_exports'], [])

    def test_model_clean_retry_refuses_changed_input_or_semantic_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name in ('input.jsonl', 'taxonomy.json', 'model_clean.py', 'pipeline.json'):
                (home / name).write_text(name, encoding='utf-8')
            command = [sys.executable, str(home / 'model_clean.py'), '--input',
                       str(home / 'input.jsonl'), '--taxonomy', str(home / 'taxonomy.json'),
                       '--api-url', 'http://127.0.0.1:1', '--model', 'fake', '--workers', '64']
            job = {'home': str(home), 'config': str(home / 'pipeline.json')}
            freeze_important_clean_identity(job, command)
            command[-1] = '16'
            freeze_important_clean_identity(job, command)
            (home / 'input.jsonl').write_text('changed', encoding='utf-8')
            with self.assertRaises(ValueError):
                freeze_important_clean_identity(job, command)

    def test_stream_reconciliation_requires_complete_native_result_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepared = home / 'run/02_structure/prepared'
            results = home / 'run/02_structure/classification/results'
            results.mkdir(parents=True)
            (prepared / 'manifest.json').parent.mkdir(parents=True)
            (prepared / 'manifest.json').write_text(json.dumps({'targets': ['a', 'b']}), encoding='utf-8')
            (results.parent / 'manifest.json').write_text('{}', encoding='utf-8')
            job = {'home': str(home)}
            self.assertFalse(stream_reconciliation_ready(job))
            for ref in ('a', 'b'):
                (results / (ref + '.json')).write_text(json.dumps({'ref': ref}), encoding='utf-8')
            self.assertTrue(stream_reconciliation_ready(job))

    def test_important_model_clean_transport_defers_without_singleton_fanout(self):
        batch = [{'key': 'k1', 'source_row': {'name': '齿轮', 'knowledge_point': 'gear'},
                  'api_item': {'key': 'k1', 'name': '齿轮', 'knowledge_point': 'gear'}}]
        args = argparse.Namespace(model='fake', temperature=0.0, max_tokens=1024,
            enable_thinking=False, response_format='off', retries=2, single_retries=1,
            confidence_threshold=0.8, fix_swapped_languages=False,
            defer_transport_retry=True)
        calls = [0]
        def unavailable(*_args, **_kwargs):
            calls[0] += 1
            raise HTTPError('http://127.0.0.1:1', 504, 'Gateway timeout', None, None)
        with patch.object(clean_transport, 'http_request', side_effect=unavailable), \
                patch.object(clean_transport.time, 'sleep', side_effect=AssertionError('inline transport retry')):
            staged = clean_transport.process_batch(1, batch, 'prompt', args)
        self.assertEqual(calls[0], 1)
        self.assertEqual(staged['results'][0]['result']['decision'], 'error')
        args.defer_transport_retry = False
        calls[0] = 0
        with patch.object(clean_transport, 'http_request', side_effect=unavailable), \
                patch.object(clean_transport.time, 'sleep', return_value=None):
            clean_transport.process_batch(1, batch, 'prompt', args)
        self.assertGreater(calls[0], 1)

    def test_important_model_clean_http_413_is_terminal_and_not_low_cap_pending(self):
        batch = [{'key': 'k1', 'source_row': {'name': '齿轮', 'knowledge_point': 'gear'},
                  'api_item': {'key': 'k1', 'name': '齿轮', 'knowledge_point': 'gear'}}]
        args = argparse.Namespace(model='fake', temperature=0.0, max_tokens=1024,
            enable_thinking=False, response_format='off', retries=2, single_retries=1,
            confidence_threshold=0.8, fix_swapped_languages=False,
            defer_transport_retry=True)
        calls = [0]
        def reject(*_args, **_kwargs):
            calls[0] += 1
            raise HTTPError('http://127.0.0.1:1', 413, 'too large', None, None)
        with patch.object(clean_transport, 'http_request', side_effect=reject), \
             patch.object(clean_transport.time, 'sleep', side_effect=AssertionError('retry')):
            result = clean_transport.process_batch(1, batch, 'prompt', args)
        self.assertEqual(calls[0], 1)
        self.assertEqual(result['results'][0]['result']['failure_kind'], 'terminal_transport')

    def test_model_clean_resume_selects_transport_errors_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = clean_transport.init_db(Path(tmp) / 'checkpoint.sqlite')
            try:
                clean_transport.save_results(db, [
                    {'key': 'transport', 'result': {'decision': 'error',
                     'failure_kind': 'transport', 'policy_version': clean_transport.MODEL_POLICY_VERSION}},
                    {'key': 'format', 'result': {'decision': 'error',
                     'failure_kind': 'content_or_format', 'policy_version': clean_transport.MODEL_POLICY_VERSION}},
                    {'key': 'pass', 'result': {'decision': 'keep',
                     'policy_version': clean_transport.MODEL_POLICY_VERSION}},
                ])
                done, _ = clean_transport.done_keys(db, clean_transport.MODEL_POLICY_VERSION,
                                                    {'error'}, retry_failure_kind='transport')
                self.assertEqual(done, {'format', 'pass'})
            finally:
                db.close()

    def test_model_clean_pending_selector_ignores_format_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            report = home / 'run/04_cleaning/current/02_model_clean/model_judgments.jsonl'
            report.parent.mkdir(parents=True)
            rows = [{'model_cleaning': {'decision': 'error', 'failure_kind': 'content_or_format'}},
                    {'model_cleaning': {'decision': 'error', 'failure_kind': 'transport'}},
                    {'model_cleaning': {'decision': 'keep'}}]
            report.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            self.assertEqual(important_clean_transport_errors({'home': str(home)}), 1)

    def test_selected_native_tasks_keep_original_order_and_require_known_names(self):
        plan = [{"stage": "03", "tasks": [{"name": "prepare_fullbook"},
                                              {"name": "extract_fullbook"},
                                              {"name": "verify_extraction_sources"}]}]
        selected = select_tasks(plan, ["prepare_fullbook", "verify_extraction_sources"])
        self.assertEqual([x["name"] for x in selected[0]["tasks"]],
                         ["prepare_fullbook", "verify_extraction_sources"])
        with self.assertRaises(ValueError):
            select_tasks(plan, ["missing"])

    def test_dictionary_retry_candidate_selection_rejects_input_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, out = root / "prepared", root / "classified"
            (base / "prepared").mkdir(parents=True)
            (out / "results").mkdir(parents=True)
            (base / "manifest.json").write_text(json.dumps({"targets": ["ok", "bad", "format"]}), encoding="utf-8")
            for ref in ("ok", "bad", "format"):
                (base / "prepared" / (ref + ".json")).write_text(json.dumps({"ref": ref}), encoding="utf-8")
            (out / "results/ok.json").write_text(json.dumps({"ref": "ok", "status": "classified"}), encoding="utf-8")
            (out / "results/bad.json").write_text(json.dumps({"ref": "bad", "status": "technical_failed", "failure_kind": "transport", "errors": ["HTTP 504"]}), encoding="utf-8")
            (out / "results/format.json").write_text(json.dumps({"ref": "format", "status": "technical_failed", "failure_kind": "content_or_format", "errors": ["invalid evidence"]}), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"api_url": "http://127.0.0.1:1", "model": "fake", "context_limit": 32768, "defer_transport_retry": True}), encoding="utf-8")
            source = root / "books.json"
            source.write_text("[]", encoding="utf-8")
            manifest = classify_books.classification_identity(base, config)
            manifest.update(books_path=str(source.resolve()), books_sha256=classify_books.sha256(source),
                            config_sha256=classify_books.sha256(config))
            (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(classify_books.technical_retry_candidates(base, out, config, source), ["bad"])
            config.write_text(config.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(ValueError):
                classify_books.technical_retry_candidates(base, out, config, source)

    def test_dictionary_transport_defers_http_failure_only_in_staged_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md = root / 'book.md'
            md.write_text('one\ntwo\n', encoding='utf-8')
            stat = md.stat()
            prepared = {'title': 'Book', 'source_stats': [{'path': str(md),
                        'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}],
                        'md_sha256': classify_books.sha256(md),
                        'windows': [{'window_id': 'w0', 'candidate': {'line': 0},
                                     'lines': [{'id': 'md:1', 'text': 'one'}]}]}
            classify_books.io.write(root / 'base/prepared/book.json', prepared)
            core = type('Core', (), {'PROMPT': 'classify', 'add_context': staticmethod(lambda w, r: w),
                                     'validate': staticmethod(lambda data, windows: {'status': 'classified'})})
            calls = [0]
            def http(url, payload=None, **_kwargs):
                if url.endswith('/tokenize'):
                    return {'count': 100}
                calls[0] += 1
                raise HTTPError(url, 504, 'Gateway timeout', None, None)
            config = {'api_url': 'http://127.0.0.1:1', 'model': 'fake',
                      'context_limit': 32768, 'max_tokens': 8192}
            with patch.object(dictionary_transport, 'core', core), \
                    patch.object(dictionary_transport.io, 'http', side_effect=http):
                staged = dictionary_transport.process('book', root / 'base', root / 'staged',
                                                       {**config, 'defer_transport_retry': True})
                self.assertEqual(calls[0], 1)
                self.assertEqual(staged['failure_kind'], 'transport')
                legacy = dictionary_transport.process('book', root / 'base', root / 'legacy', config)
            self.assertEqual(calls[0], 3)
            self.assertEqual(legacy['status'], 'technical_failed')

    def test_dictionary_stream_task_reconciles_completed_native_outputs_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            books, config = root / 'books.json', root / 'config.json'
            books.write_text('[]', encoding='utf-8')
            config.write_text(json.dumps({'api_url': 'http://127.0.0.1:1',
                                          'model': 'fake', 'context_limit': 32768}), encoding='utf-8')
            with patch.object(classify_books.io, 'http',
                              return_value={'data': [{'id': 'fake'}]}):
                classify_books.prepare_run(books, root / 'prepared', root / 'classified',
                                           config, 1, 1, 1)
            with patch.object(classify_books.io, 'http',
                              side_effect=AssertionError('model called during reconcile')):
                classify_books.prepare_run(books, root / 'prepared', root / 'classified',
                                           config, 1, 1, 1)
            self.assertEqual(classify_books.io.read(root / 'classified/DONE.json')['expected'], 0)

    def test_important_streaming_manifest_can_resume_only_at_same_identity(self):
        core = {"base": "fixed", "base_manifest_sha256": "sha", "targets": ["a"],
                "config": {"model": "fake"}, "prompt_sha256": "prompt"}
        streaming = {**core, "config_path": "fixed-config", "config_sha256": "config-sha",
                     "code_sha256": "code-sha"}
        important_classifier.validate_resume_manifest(streaming, core,
                                                       "fixed-config", "config-sha", "code-sha")
        with self.assertRaises(ValueError):
            important_classifier.validate_resume_manifest(
                {**streaming, "targets": ["b"]}, core,
                "fixed-config", "config-sha", "code-sha")

    def test_important_native_retry_preserves_success_and_archives_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            books, config = root / 'books.json', root / 'config.json'
            prepared, classified = root / 'prepared', root / 'classified'
            paths = []
            for n in range(2):
                path = root / f'b{n}.md'
                path.write_text('正文内容\n第二行正文\n', encoding='utf-8')
                paths.append({'identifier': f'b{n}', 'md_path': str(path)})
            important_classifier.write_json(books, paths)
            important_classifier.write_json(config,
                {'api_url': 'http://127.0.0.1:1', 'model': 'fake',
                 'defer_transport_retry': True})
            b1_calls = [0]
            format_b1 = [False]
            def response(url, cfg, payload=None):
                if url.endswith('/v1/models'):
                    return {'data': [{'id': 'fake'}]}
                if 'b1' in json.dumps(payload, ensure_ascii=False):
                    b1_calls[0] += 1
                    if fail_b1[0]:
                        raise HTTPError(url, 504, 'Gateway timeout', None, None)
                    if format_b1[0]:
                        return {'choices': [{'message': {'content': 'invalid json'}}]}
                return {'choices': [{'message': {'content': json.dumps({
                    'status': 'needs_review', 'primary_class': None, 'other_subtype': None,
                    'review_reason': 'insufficient_evidence', 'basis': '正文证据不足',
                    'is_mixed': False, 'secondary_classes': [], 'layout_features': [],
                    'evidence': [{'line_id': 'md:1', 'quote': '正文内容',
                                  'role': 'representative', 'class_code': None}],
                    'uncertainties': '章节范围不明'}, ensure_ascii=False)}}]}
            fail_b1 = [True]
            with patch.object(important_classifier, 'http_json', side_effect=response), \
                    patch.object(important_classifier, 'ProcessPoolExecutor',
                                 side_effect=lambda **kw: ThreadPoolExecutor(max_workers=kw['max_workers'])):
                first = important_classifier.prepare_run(books, prepared, classified, config,
                    workers=2, prepare_workers=2, max_pending=2)
            self.assertEqual(first['statuses']['technical_failed'], 1)
            self.assertEqual(b1_calls[0], 1)
            success = classified / 'results/b0.json'
            digest = important_classifier.sha256(success)
            fail_b1[0] = False
            with patch.object(important_classifier, 'http_json', side_effect=response):
                second = important_classifier.run(prepared, classified, config, workers=2)
            self.assertEqual(second['statuses'], {'needs_review': 2})
            self.assertEqual(important_classifier.sha256(success), digest)
            self.assertTrue((classified / 'technical_retry_archive/b1/round_1/results/b1.json').is_file())
            with patch.object(important_classifier, 'http_json',
                              side_effect=AssertionError('model called during reconcile')):
                third = important_classifier.prepare_run(books, prepared, classified, config,
                    workers=2, prepare_workers=2, max_pending=2)
            self.assertEqual(third['statuses'], {'needs_review': 2})
            legacy_config = root / 'legacy_config.json'
            important_classifier.write_json(legacy_config,
                {'api_url': 'http://127.0.0.1:1', 'model': 'fake'})
            b1_calls[0] = 0
            fail_b1[0] = True
            with patch.object(important_classifier, 'http_json', side_effect=response):
                legacy = important_classifier.run(prepared, root / 'legacy_classified',
                                                   legacy_config, workers=2)
            self.assertEqual(legacy['statuses']['technical_failed'], 1)
            self.assertEqual(b1_calls[0], 2)
            b1_calls[0] = 0
            fail_b1[0] = False
            format_b1[0] = True
            format_out = root / 'format_classified'
            with patch.object(important_classifier, 'http_json', side_effect=response):
                first_format = important_classifier.run(prepared, format_out, config, workers=2)
                self.assertEqual(b1_calls[0], 2)
                self.assertEqual(first_format['statuses']['technical_failed'], 1)
                self.assertEqual(important_classifier.read_json(format_out / 'results/b1.json')
                                 ['failure_kind'], 'content_or_format')
                important_classifier.run(prepared, format_out, config, workers=2)
            self.assertEqual(b1_calls[0], 2)


if __name__ == "__main__":
    unittest.main()
