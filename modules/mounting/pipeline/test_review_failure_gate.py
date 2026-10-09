"""Regression tests for the review queue's technical-failure safety gate."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runner


class ReviewFailureGateTests(unittest.TestCase):
    def test_large_fresh_failure_still_stops_queue(self):
        """The safety gate still stops a fresh run when most calls fail."""
        items = [{"request_id": f"M{i:04d}"} for i in range(200)]
        with tempfile.TemporaryDirectory() as temporary:
            def failed_review(_base, _model, item):
                return {
                    "request_id": item["request_id"],
                    "final": {"judgment": "technical_failure"},
                }

            with patch.object(runner.reviewer, "review_one", side_effect=failed_review):
                with self.assertRaisesRegex(RuntimeError, "FAILURE_RATE_OVER_50_PERCENT"):
                    runner.review_batch(items, Path(temporary))

    def test_small_residual_failures_do_not_abort_entire_subject(self):
        """A 300/20,000 residual retry batch must not trip the source gate."""
        items = [{"request_id": f"M{i:05d}"} for i in range(20000)]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (directory / "responses.jsonl").open("w", encoding="utf-8") as stream:
                for item in items[:19700]:
                    stream.write(json.dumps({
                        "request_id": item["request_id"],
                        "final": {"judgment": "reasonable"},
                    }) + "\n")

            def failed_review(_base, _model, item):
                return {
                    "request_id": item["request_id"],
                    "final": {"judgment": "technical_failure"},
                }

            with patch.object(runner.reviewer, "review_one", side_effect=failed_review):
                results = runner.review_batch(items, directory)

            self.assertEqual(len(results), 20000)
            self.assertEqual(sum(
                row["judgment"] == "technical_failure" for row in results.values()
            ), 300)
            self.assertTrue((directory / "final.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
