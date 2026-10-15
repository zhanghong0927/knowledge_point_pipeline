"""Transient tokenize failures recover without becoming false content rejections."""
import argparse
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import fullbook_llm_v5 as v5


class TokenizeRecoveryTests(unittest.TestCase):
    def runner(self):
        args = argparse.Namespace(api_url='http://unused/v1', model='test', tokenizer=None,
            workers=2, timeout=3, context=32768, output_tokens=8192, tokenize_attempts=3)
        runner = v5.Runner(args)
        runner.backoff = lambda _attempt: None
        self.addCleanup(runner.close)
        return runner

    def test_transient_502_recovers_and_success_is_cached(self):
        runner = self.runner()
        error = HTTPError('http://unused/tokenize', 502, 'bad gateway', {}, None)
        messages = [{'role': 'user', 'content': 'Alpha'}]
        with patch('urllib.request.urlopen', side_effect=[error, io.BytesIO(b'{"count":100}')]) as transport:
            try:
                fitting = runner.fits_messages(messages)
            except HTTPError as exc:
                self.fail(f'A transient 502 interrupted token counting: {exc}')
            self.assertTrue(fitting)
            self.assertTrue(runner.fits_messages(messages))
        self.assertEqual(transport.call_count, 2)

    def test_temporary_network_timeout_recovers(self):
        runner = self.runner()
        with patch('urllib.request.urlopen', side_effect=[URLError('timeout'), io.BytesIO(b'{"count":10}')]):
            try:
                fitting = runner.fits_messages([{'role': 'user', 'content': 'Alpha'}])
            except URLError as exc:
                self.fail(f'A transient timeout interrupted token counting: {exc}')
            self.assertTrue(fitting)

    def test_persistent_503_is_bounded_and_remains_technical_failure(self):
        runner = self.runner()
        error = HTTPError('http://unused/tokenize', 503, 'unavailable', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as transport:
            with self.assertRaises(HTTPError):
                runner.fits_messages([{'role': 'user', 'content': 'Alpha'}])
        self.assertEqual(transport.call_count, 3)

    def test_413_still_returns_a_split_decision_without_retries(self):
        runner = self.runner()
        error = HTTPError('http://unused/tokenize', 413, 'too large', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as transport:
            self.assertFalse(runner.fits_messages([{'role': 'user', 'content': 'Alpha'}]))
        self.assertEqual(transport.call_count, 1)

    def test_401_is_not_retried(self):
        runner = self.runner()
        error = HTTPError('http://unused/tokenize', 401, 'unauthorized', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as transport:
            with self.assertRaises(HTTPError):
                runner.fits_messages([{'role': 'user', 'content': 'Alpha'}])
        self.assertEqual(transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
