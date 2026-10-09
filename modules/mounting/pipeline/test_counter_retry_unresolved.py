import unittest
from unittest.mock import patch
from counter_retry_unresolved import run_counter


class CounterRetryTest(unittest.TestCase):
    def test_independent_uncertain_overrides_unconfirmed_unreasonable(self):
        item={'request_id':'r1'}
        first={'judgment':'unreasonable','reason':'首轮认为错挂'}
        second={'judgment':'uncertain','reason':'证据不足'}
        with patch('counter_retry_unresolved.reviewer.call_one',return_value={'result':second,'attempts':[]}) as call:
            result=run_counter(item,first,'http://example','model')
        self.assertEqual(result['final']['judgment'],'uncertain')
        self.assertEqual(call.call_args.args[:3],('http://example','model',item))
        self.assertTrue(call.call_args.args[3])

    def test_failed_independent_review_does_not_confirm_unreasonable(self):
        item={'request_id':'r1'}
        first={'judgment':'unreasonable','reason':'首轮认为错挂'}
        with patch('counter_retry_unresolved.reviewer.call_one',return_value={'result':None,'attempts':[]}):
            result=run_counter(item,first,'http://example','model')
        self.assertEqual(result['final']['judgment'],'technical_failure')


if __name__=='__main__':unittest.main()
