import argparse
import io
import json
import unittest
from unittest.mock import patch

import fullbook_llm_v5 as v5
import fullbook_llm_v5_retry16 as retry16


class BudgetTests(unittest.TestCase):
    runner_class = v5.Runner

    def runner(self, url='http://service/v1'):
        args = argparse.Namespace(api_url=url, model='test', tokenizer=None,
                                  workers=2, timeout=3, context=32768,
                                  output_tokens=8192)
        runner = self.runner_class(args)
        self.addCleanup(runner.close)
        return runner

    def test_chinese_bytes_do_not_reject_a_measured_fitting_request(self):
        runner = self.runner()
        messages = [{'role': 'user', 'content': '\u8bcd' * 10000}]
        requests = []
        def tokenize(request, **kwargs):
            requests.append(request)
            return io.BytesIO(b'{"count":10001}')
        with patch('urllib.request.urlopen', side_effect=tokenize):
            self.assertTrue(runner.fits_messages(messages))
            self.assertTrue(runner.fits_messages(messages))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, 'http://service/tokenize')
        sent = json.loads(requests[0].data)
        self.assertEqual(sent['messages'], messages)
        self.assertEqual(sent['model'], 'test')
        self.assertTrue(sent['add_generation_prompt'])
        self.assertFalse(sent['chat_template_kwargs']['enable_thinking'])

    def test_output_margin_and_chat_template_tokens_are_counted(self):
        runner = self.runner(url='http://service/')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"count":22528}')):
            self.assertTrue(runner.fits_messages([{'role': 'user', 'content': 'a'}]))
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"count":22529}')):
            self.assertFalse(runner.fits_messages([{'role': 'user', 'content': 'b'}]))

    def test_tokenize_failure_does_not_become_content_rejection(self):
        runner = self.runner()
        with patch('urllib.request.urlopen', side_effect=OSError('tokenizer down')):
            with self.assertRaises(OSError):
                runner.fits_messages([{'role': 'user', 'content': 'short'}])
        for count in (True, -1, 1.5, '5', None):
            with self.subTest(count=count), patch('urllib.request.urlopen',
                    return_value=io.BytesIO(json.dumps({'count': count}).encode())):
                with self.assertRaises(ValueError):
                    runner.fits_messages([{'role': 'user', 'content': 'short'}])

    def test_local_tokenizer_uses_the_inference_template_without_network(self):
        runner = self.runner()
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                self.options = kwargs
                return [0] * 100
        runner.tokenizer = Tokenizer()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            self.assertTrue(runner.fits_messages([{'role': 'user', 'content': 'short'}]))
        self.assertFalse(runner.tokenizer.options['enable_thinking'])


class RetryBudgetTests(BudgetTests):
    runner_class = retry16.Runner


if __name__ == '__main__':
    unittest.main()
