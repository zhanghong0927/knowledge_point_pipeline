import json
import hashlib
import contextlib
import io as text_io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

import classify_books as subject


class StreamingClassificationTests(unittest.TestCase):
    def books(self, root, count):
        path = root / 'books.json'
        path.write_text(json.dumps([{'identifier': f'b{i}', 'title': f'Book {i}',
                                     'md_path': str(root / f'b{i}.md')} for i in range(count)]))
        return path

    def test_overlap_bounded_and_bad_book_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            books = self.books(root, 1000)
            source = root / 'shared.md'
            source.write_text('a source', encoding='utf-8')
            events = {}
            guard = threading.Lock()

            def prepare_one(book, out, strict_pdf):
                ref = book['identifier']
                if ref == 'b999':
                    time.sleep(.08)
                with guard:
                    events['prepared_' + ref] = time.monotonic()
                if ref == 'b1':
                    subject.io.write(out / 'preparation_failed' / f'{ref}.json',
                                     {'ref': ref, 'status': 'technical_failed', 'errors': ['bad_pdf']})
                else:
                    stat = source.stat()
                    subject.io.write(out / 'prepared' / f'{ref}.json',
                                     {'ref': ref, 'source_stats': [{'path': str(source),
                                      'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}],
                                      'md_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})

            def process(ref, base, out, cfg):
                with guard:
                    events.setdefault('model_start', time.monotonic())
                time.sleep(.0001)
                result = {'ref': ref, 'status': 'technical_failed', 'errors': ['fake_model']}
                subject.io.write(out / 'results' / f'{ref}.json', result)
                return result

            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))
            def fake_pool(max_workers, mp_context):
                return ThreadPoolExecutor(max_workers=max_workers)

            observed = []
            original_wait = subject.wait

            def bounded_wait(futures, **kwargs):
                observed.append(len(futures))
                return original_wait(futures, **kwargs)

            with patch.object(subject, 'ProcessPoolExecutor', fake_pool), \
                 patch.object(subject, 'prepare_one', side_effect=prepare_one), \
                 patch.object(subject.transport, 'process', side_effect=process), \
                 patch.object(subject, 'wait', side_effect=bounded_wait), \
                 patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}):
                with contextlib.redirect_stdout(text_io.StringIO()):
                    subject.prepare_run(books, root / 'prep', root / 'classified', cfg, 3, 2, 5)
            self.assertLess(events['model_start'], events['prepared_b999'])
            self.assertEqual(subject.io.read(root / 'classified/results/b1.json')['status'], 'technical_failed')
            self.assertEqual(subject.io.read(root / 'classified/DONE.json')['finished'], 1000)
            self.assertLessEqual(subject.io.read(root / 'classified/METRICS.json')['peak_pending'], 5)
            self.assertLessEqual(max(observed), 5)
            metrics = subject.io.read(root / 'classified/METRICS.json')
            self.assertLess(metrics['first_model_started_at'], metrics['last_preparation_finished_at'])

    def test_empty_cohort_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            books = self.books(root, 0)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))
            with patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}):
                subject.prepare_run(books, root / 'prep', root / 'out', cfg, 2, 2, 4)
            self.assertEqual(subject.io.read(root / 'out/DONE.json')['expected'], 0)
            books.write_text('[] ', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'books_changed'):
                subject.verify(root / 'out')
            self.assertFalse((root / 'out/DONE.json').exists())

    def test_duplicate_ids_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            books = root / 'books.json'
            books.write_text(json.dumps([{'identifier': 'same'}, {'identifier': 'same'}]))
            cfg = root / 'config.json'
            cfg.write_text('{}')
            with self.assertRaises(ValueError):
                subject.prepare_run(books, root / 'prep', root / 'out', cfg, 2, 2, 4)
            self.assertFalse((root / 'out').exists())

    def test_malformed_config_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            books = self.books(root, 0)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test',
                                       'context_limit': '32768'}))
            with patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}):
                with self.assertRaises(ValueError):
                    subject.prepare_run(books, root / 'prep', root / 'out', cfg, 2, 2, 4)
            self.assertFalse((root / 'out').exists())

    def test_source_change_prevents_done(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            md = root / 'b0.md'
            md.write_text('## term\ndefinition', encoding='utf-8')
            books = self.books(root, 1)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))

            def fake_pool(max_workers, mp_context):
                return ThreadPoolExecutor(max_workers=max_workers)

            def process(ref, base, out, config):
                md.write_text('changed source text', encoding='utf-8')
                result = {'ref': ref, 'status': 'technical_failed', 'errors': ['fake_model']}
                subject.io.write(out / 'results' / f'{ref}.json', result)
                return result

            with patch.object(subject, 'ProcessPoolExecutor', fake_pool), \
                 patch.object(subject.transport, 'process', side_effect=process), \
                 patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}), \
                 contextlib.redirect_stdout(text_io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'source_changed'):
                    subject.prepare_run(books, root / 'prep', root / 'out', cfg, 1, 1, 2)
            self.assertFalse((root / 'out/DONE.json').exists())

    def test_bad_pdf_isolated_with_spawned_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good_md = root / 'good.md'
            bad_md = root / 'bad.md'
            good_md.write_text('## Good\nA definition.', encoding='utf-8')
            bad_md.write_text('## Bad\nA definition.', encoding='utf-8')
            bad_pdf = root / 'bad.pdf'
            bad_pdf.write_text('not a PDF', encoding='utf-8')
            books = root / 'books.json'
            books.write_text(json.dumps([
                {'identifier': 'bad', 'title': 'Bad', 'md_path': str(bad_md), 'pdf_path': str(bad_pdf)},
                {'identifier': 'good', 'title': 'Good', 'md_path': str(good_md)}]))
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))

            def process(ref, base, out, config):
                result = {'ref': ref, 'status': 'technical_failed', 'errors': ['fake_model']}
                subject.io.write(out / 'results' / f'{ref}.json', result)
                return result

            with patch.object(subject.transport, 'process', side_effect=process), \
                 patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}), \
                 contextlib.redirect_stdout(text_io.StringIO()):
                subject.prepare_run(books, root / 'prep', root / 'out', cfg, 2, 2, 4)
            self.assertEqual(subject.io.read(root / 'prep/preparation_failed/bad.json')['status'],
                             'technical_failed')
            self.assertTrue((root / 'prep/prepared/good.json').exists())
            self.assertEqual(subject.io.read(root / 'out/DONE.json')['finished'], 2)

    def test_config_change_during_model_prevents_done(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'b0.md').write_text('## term\ndefinition', encoding='utf-8')
            books = self.books(root, 1)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))

            def fake_pool(max_workers, mp_context):
                return ThreadPoolExecutor(max_workers=max_workers)

            def process(ref, base, out, config):
                cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'changed',
                                           'context_limit': 32768}))
                result = {'ref': ref, 'status': 'technical_failed', 'errors': ['fake_model']}
                subject.io.write(out / 'results' / f'{ref}.json', result)
                return result

            with patch.object(subject, 'ProcessPoolExecutor', fake_pool), \
                 patch.object(subject.transport, 'process', side_effect=process), \
                 patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}), \
                 contextlib.redirect_stdout(text_io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'config_changed'):
                    subject.prepare_run(books, root / 'prep', root / 'out', cfg, 1, 1, 2)
            self.assertFalse((root / 'out/DONE.json').exists())

    def test_legacy_first_failure_does_not_skip_remaining_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'prep'
            subject.io.write(base / 'manifest.json', {'targets': ['first', 'second']})
            for ref in ('first', 'second'):
                subject.io.write(base / 'prepared' / f'{ref}.json', {'ref': ref})
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'api_url': 'http://model', 'model': 'test', 'context_limit': 32768}))

            def process(ref, base, out, config):
                result = {'ref': ref, 'status': 'technical_failed', 'errors': ['probe']}
                subject.io.write(out / 'results' / f'{ref}.json', result)
                return result

            fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None)
            with patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                 patch.object(subject.transport, 'process', side_effect=process), \
                 patch.object(subject.io, 'http', return_value={'data': [{'id': 'test'}]}), \
                 contextlib.redirect_stdout(text_io.StringIO()):
                subject.classify(base, root / 'out', cfg, 2)
            self.assertEqual({p.stem for p in (root / 'out/results').glob('*.json')},
                             {'first', 'second'})
            self.assertTrue((root / 'out/DONE.json').exists())


if __name__ == '__main__':
    unittest.main()
