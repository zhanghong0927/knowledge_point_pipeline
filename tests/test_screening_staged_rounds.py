import argparse
import csv
import io
import json
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules/book_screening/scripts'))
import llm_refine_important_book_metadata as metadata
import general_book_screening_pipeline as important_md
sys.path.insert(0, str(ROOT / 'modules/book_screening/dictionary_md_v3'))
import dictionary_md_audit as dictionary_md


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class StagedScreeningTests(unittest.TestCase):
    def metadata_fixture(self, root):
        source = root / 'books.csv'
        with source.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['identifier', 'title'])
            writer.writeheader()
            writer.writerows([{'identifier': 'a', 'title': 'A'},
                              {'identifier': 'b', 'title': 'B'}])
        config = root / 'scope.json'
        config.write_text(json.dumps({'subject_name': '测试', 'boundary': {}}), encoding='utf-8')
        return argparse.Namespace(input_csv=source, subject_config=config,
            output_dir=root / 'out', api_url='http://127.0.0.1:9', model='test-model',
            sample_size=0, seed=5, batch_size=1, workers=2, timeout=1,
            retries=2, max_tokens=1000, only_round=1, finalize_only=False)

    def test_metadata_rounds_keep_success_and_retry_only_502(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.metadata_fixture(Path(temp))
            calls = Counter()

            def respond(request, **_):
                user_prompt = json.loads(request.data)['messages'][1]['content']
                record = json.loads(user_prompt.split('\n', 1)[1])[0]['record_id']
                calls[record] += 1
                if record == 'r000000001' and calls[record] == 1:
                    raise HTTPError(request.full_url, 502, 'bad gateway', {}, io.BytesIO(b''))
                return Response({'choices': [{'message': {'content': json.dumps({'results': [
                    {'record_id': record, 'decision': 'KEEP', 'evidence_fields': []}]})}}]})

            with patch.object(metadata.urllib.request, 'urlopen', side_effect=respond):
                first = metadata.run(args)
                self.assertEqual(first['state'], 'awaiting_retry')
                args.only_round = 2
                second = metadata.run(args)
                self.assertEqual(second['state'], 'ready_to_finalize')
                args.only_round = None
                args.finalize_only = True
                summary = metadata.run(args)
            self.assertEqual(calls, {'r000000001': 2, 'r000000002': 1})
            self.assertEqual(summary['counts']['KEEP'], 2)
            self.assertEqual(json.loads((args.output_dir / 'ORCHESTRATION_STATUS.json').read_text())['state'],
                             'finalized')
            with (args.output_dir / '批次检查点.jsonl').open('a', encoding='utf-8') as handle:
                handle.write('{}\n')
            with self.assertRaisesRegex(ValueError, '检查点|checkpoint changed'):
                metadata.run(args)

    def test_permanent_http_413_is_terminal_review_and_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.metadata_fixture(Path(temp))
            calls = 0

            def reject(request, **_):
                nonlocal calls
                calls += 1
                raise HTTPError(request.full_url, 413, 'too large', {}, io.BytesIO(b''))

            with patch.object(metadata.urllib.request, 'urlopen', side_effect=reject):
                first = metadata.run(args)
            self.assertEqual(first['state'], 'technical_failure')
            self.assertEqual(first['pending_transport'], 0)
            self.assertEqual(calls, 2)
            args.only_round = None
            args.finalize_only = True
            with patch.object(metadata.urllib.request, 'urlopen', side_effect=AssertionError('HTTP in finalize')):
                summary = metadata.run(args)
            self.assertEqual(summary['counts']['REVIEW'], 2)

    def test_important_md_transport_first_pass_is_one_call(self):
        for code, kind in ((502, 'transport'), (413, 'terminal_transport')):
            calls = 0

            def reject(request, **_):
                nonlocal calls
                calls += 1
                raise HTTPError(request.full_url, code, 'failure', {}, io.BytesIO(b''))

            with patch.object(important_md.urllib.request, 'urlopen', side_effect=reject):
                result = important_md.request_llm('http://127.0.0.1:9', 'm', 'p',
                    {'samples': [], 'book_track': '其他重要书籍'}, set(), 1, 2, 100,
                    defer_transport_retry=True)
            self.assertEqual(calls, 1)
            self.assertEqual(result['failure_kind'], kind)

    def test_important_md_native_rounds_finalize_review_after_502(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            md = root / 'book.md'
            md.write_text('普通专业书籍\n学科概念及方法。\n' * 20, encoding='utf-8')
            source = root / 'input.csv'
            with source.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=['identifier', 'title',
                    'book_track', 'parsed_path'])
                writer.writeheader()
                writer.writerow({'identifier': 'b1', 'title': 'Book',
                    'book_track': '其他重要书籍', 'parsed_path': str(md)})
            scope = root / 'scope.json'
            scope.write_text(json.dumps({'subject_name': '测试',
                'boundary': {'core_scope': '测试'}}), encoding='utf-8')
            out = root / 'out'
            out.mkdir()
            (out / 'MD审核试验样本.csv').write_bytes(source.read_bytes())
            base = ['general_book_screening_pipeline.py', 'audit', '--input-csv',
                    str(source), '--output', str(out), '--subject-config', str(scope),
                    '--api-url', 'http://127.0.0.1:9', '--model', 'fake',
                    '--workers', '1', '--retries', '2']
            calls = 0

            def unavailable(request, **_):
                nonlocal calls
                calls += 1
                raise HTTPError(request.full_url, 502, 'bad gateway', {}, io.BytesIO(b''))

            with patch.object(important_md.urllib.request, 'urlopen', side_effect=unavailable):
                for number, expected in ((1, 'awaiting_retry'), (2, 'awaiting_retry'),
                                         (3, 'technical_failure')):
                    with patch.object(sys, 'argv', [*base, '--only-round', str(number)]), \
                         redirect_stdout(io.StringIO()):
                        important_md.main()
                    self.assertEqual(json.loads((out / 'ORCHESTRATION_STATUS.json').read_text())['state'],
                                     expected)
            self.assertEqual(calls, 3)
            with patch.object(sys, 'argv', [*base, '--finalize-only']), \
                 patch.object(important_md.urllib.request, 'urlopen',
                              side_effect=AssertionError('HTTP in finalization')), \
                 redirect_stdout(io.StringIO()):
                important_md.main()
            with (out / '最终审核结果.csv').open(encoding='utf-8-sig') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]['final_decision'], 'REVIEW')
            self.assertEqual(json.loads((out / 'ORCHESTRATION_STATUS.json').read_text())['state'],
                             'finalized')

    def test_dictionary_md_transport_first_pass_is_one_call(self):
        item = {'short_id': 'X1', 'subject': '测试', 'title': 'T', 'file_name': 'a.md',
                'source_path': '/tmp/a.md', 'relative_path': 'a.md', 'samples': []}
        for code, kind in ((502, 'transport'), (413, 'terminal_transport')):
            calls = 0

            def reject(request, **_):
                nonlocal calls
                calls += 1
                raise HTTPError(request.full_url, code, 'failure', {}, io.BytesIO(b''))

            with patch.object(dictionary_md.urllib.request, 'urlopen', side_effect=reject):
                result = dictionary_md.request_one('http://127.0.0.1:9', 'm', item,
                    1, 100, 2, defer_transport_retry=True)
            self.assertEqual(calls, 1)
            self.assertEqual(result['failure_kind'], kind)

    def test_dictionary_native_rounds_exhaust_502_without_promoting_book(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'MD').mkdir()
            (root / 'MD/book.md').write_text('词条\n定义。\n', encoding='utf-8')
            config = root / 'config.json'
            config.write_text(json.dumps({'subjects': [{'name': '测试', 'code': 'T',
                'sources': ['MD']}]}), encoding='utf-8')
            out = root / 'out'
            dictionary_md.prepare(root, out, config, 1, 200)
            base = ['dictionary_md_audit.py', 'run', '--root', str(root),
                    '--output', str(out), '--config', str(config),
                    '--api-url', 'http://127.0.0.1:9', '--model', 'fake',
                    '--workers', '1', '--sample-count', '1', '--chunk-chars', '200',
                    '--pass-challenge', '--skip-priority-workbook']
            calls = 0

            def unavailable(request, **_):
                nonlocal calls
                calls += 1
                raise HTTPError(request.full_url, 502, 'bad gateway', {}, io.BytesIO(b''))

            with patch.object(dictionary_md.urllib.request, 'urlopen', side_effect=unavailable):
                for number, expected in ((1, 'awaiting_retry'), (2, 'awaiting_retry'),
                                         (3, 'technical_failure')):
                    with patch.object(sys, 'argv', [*base, '--only-round', str(number)]), redirect_stdout(io.StringIO()):
                        dictionary_md.main()
                    status = json.loads((out / 'PRIMARY_ORCHESTRATION_STATUS.json').read_text())
                    self.assertEqual(status['state'], expected)
            self.assertEqual(calls, 3)
            challenge = [*base]
            challenge[1] = 'challenge'
            with patch.object(sys, 'argv', [*challenge, '--only-round', '1']), \
                 patch.object(dictionary_md.urllib.request, 'urlopen',
                              side_effect=AssertionError('no challenge input')), \
                 redirect_stdout(io.StringIO()):
                dictionary_md.main()

            final = [*base]
            final[1] = 'materialize'
            with patch.object(sys, 'argv', [*final, '--finalize-only']), \
                 patch.object(dictionary_md.urllib.request, 'urlopen',
                              side_effect=AssertionError('HTTP in finalization')), \
                 redirect_stdout(io.StringIO()):
                dictionary_md.main()
            with (out / '全学科审核结果.csv').open(encoding='utf-8-sig') as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            self.assertEqual(json.loads((out / 'CHALLENGE_ORCHESTRATION_STATUS.json').read_text())['state'],
                             'finalized')
            (root / 'MD/book.md').write_text('词条\n已篡改定义。\n', encoding='utf-8')
            with patch.object(sys, 'argv', [*final, '--finalize-only']), \
                 self.assertRaisesRegex(ValueError, 'source content changed'):
                dictionary_md.main()

    def test_missing_pass_challenge_is_review_only_in_staged_finalization(self):
        primary = {'book.md': {'ok': True, 'source_path': 'book.md',
                   'result': {'decision': 'PASS', 'title_guess': '趣闻词典'}}}
        manifest = {'book.md': {'title': '趣闻词典'}}
        with self.assertRaisesRegex(ValueError, 'missing pass challenge'):
            dictionary_md.overlay_pass_challenges(primary, {}, manifest)
        staged = dictionary_md.overlay_pass_challenges(primary, {}, manifest,
                                                       allow_incomplete=True)
        self.assertEqual(staged['book.md']['result']['decision'], 'REVIEW')
        self.assertEqual(primary['book.md']['result']['decision'], 'PASS')


if __name__ == '__main__':
    unittest.main()
