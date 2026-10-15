import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import run_structure_v6_1 as transport


class TransportBudgetTests(unittest.TestCase):
    def invoke(self, count, config):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            md = root / "book.md"
            md.write_text("\n".join("line " + str(i) for i in range(90)), encoding="utf-8")
            stat = md.stat()
            windows = [{"window_id": "w0", "candidate": {"line": 6}, "zone": 0,
                        "lines": [{"id": "md:" + str(i + 1), "text": "line " + str(i),
                                   "pdf_format": [{"page": 1, "spans": []}]} for i in range(90)]}]
            prep = {"title": "Book", "source_stats": [{"path": str(md), "size": stat.st_size,
                                                       "mtime_ns": stat.st_mtime_ns}],
                    "md_sha256": hashlib.sha256(md.read_bytes()).hexdigest(), "windows": windows}
            transport.io.write(root / "base/prepared/book.json", prep)
            calls = []

            def http(url, payload, **kwargs):
                calls.append((url, payload))
                if url.endswith("/tokenize"):
                    sent = json.loads(payload["messages"][1]["content"])
                    return {"count": count(sent["windows"])}
                return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

            core = SimpleNamespace(PROMPT="Classification", add_context=lambda w, r: w,
                                   validate=lambda value, w: {"status": "sample_supported"})
            with patch.object(transport, "core", core), patch.object(transport.io, "http", side_effect=http):
                result = transport.process("book", root / "base", root / "out", {
                    "api_url": "http://model", "model": "test", "context_limit": 32768, **config})
            stored = transport.io.read(root / "out/prepared/book.json")
            return result, calls, stored

    def test_configured_output_budget_is_used(self):
        result, calls, _ = self.invoke(lambda windows: 20000, {"max_tokens": 8192, "timeout": 120})
        self.assertEqual(result["status"], "sample_supported")
        requests = [payload for url, payload in calls if url.endswith("/v1/chat/completions")]
        self.assertEqual(requests[0]["max_tokens"], 8192)

    def test_oversized_samples_shrink_without_rewriting_source(self):
        result, calls, stored = self.invoke(lambda windows: 1000 + sum(len(w["lines"]) * 500 for w in windows),
                                            {"max_tokens": 8192})
        self.assertEqual(result["status"], "sample_supported")
        window = stored["windows"][0]
        self.assertLess(len(window["lines"]), 90)
        self.assertEqual(window["window_id"], "w0")
        self.assertTrue(any(row["id"] == "md:7" for row in window["lines"]))
        self.assertEqual(window["lines"][0]["pdf_format"], [{"page": 1, "spans": []}])
        for row in window["lines"]:
            self.assertEqual(row["text"], "line " + str(int(row["id"].split(":")[1]) - 1))
        self.assertIn("request_budget", stored)

    def test_unfit_minimum_remains_technical_failure(self):
        result, calls, _ = self.invoke(lambda windows: 50000, {"max_tokens": 8192})
        self.assertEqual(result["status"], "technical_failed")
        self.assertFalse(any(url.endswith("/v1/chat/completions") for url, _ in calls))

    def test_long_prefix_can_be_trimmed_around_anchor(self):
        def count(windows):
            return sum(20000 if row['id'] == 'md:1' else 300 for w in windows for row in w['lines'])
        result, _, stored = self.invoke(count, {'max_tokens': 8192})
        self.assertEqual(result['status'], 'sample_supported')
        ids = [row['id'] for row in stored['windows'][0]['lines']]
        self.assertIn('md:7', ids)
        self.assertNotIn('md:1', ids)
        self.assertEqual([int(x.split(':')[1]) for x in ids],
                         list(range(int(ids[0].split(':')[1]), int(ids[-1].split(':')[1]) + 1)))

    def test_multiple_regions_keep_head_and_following_body(self):
        windows = []
        for region in range(3):
            rows = [{'id': f'md:{region * 100 + i + 1}', 'text': f'region {region} line {i}'}
                    for i in range(60)]
            windows.append({'window_id': f'w{region}', 'candidate': {'line': region * 100 + 6},
                            'lines': rows})

        def tokenize(url, payload):
            sent = json.loads(payload['messages'][1]['content'])['windows']
            return {'count': sum(20000 if row['id'].endswith('1') else 200
                                 for w in sent for row in w['lines'])}

        with patch.object(transport.io, 'http', side_effect=tokenize), \
             patch.object(transport.core, 'PROMPT', 'Classification'):
            fitted, _, _ = transport.fit_request('book', windows,
                                                 {'api_url': 'http://model', 'model': 'test',
                                                  'context_limit': 32768, 'max_tokens': 8192})
        self.assertEqual(len(fitted), 3)
        for region, window in enumerate(fitted):
            ids = [row['id'] for row in window['lines']]
            self.assertIn(f'md:{region * 100 + 7}', ids)
            self.assertIn(f'md:{region * 100 + 8}', ids)
            self.assertEqual([int(value[3:]) for value in ids],
                             list(range(int(ids[0][3:]), int(ids[-1][3:]) + 1)))

    def test_single_oversized_context_line_can_be_removed(self):
        windows = [{'window_id': 'w0', 'candidate': {'line': 0},
                    'lines': [{'id': 'md:1', 'text': 'head'}, {'id': 'md:2', 'text': 'body'}],
                    'context_lines': [{'id': 'md:0', 'text': 'large context'}]}]

        def tokenize(url, payload):
            sent = json.loads(payload['messages'][1]['content'])['windows']
            return {'count': 50000 if sent[0].get('context_lines') else 1000}

        with patch.object(transport.io, 'http', side_effect=tokenize), \
             patch.object(transport.core, 'PROMPT', 'Classification'):
            fitted, _, _ = transport.fit_request('book', windows,
                                                 {'api_url': 'http://model', 'model': 'test',
                                                  'context_limit': 32768, 'max_tokens': 8192})
        self.assertEqual([r['text'] for r in fitted[0]['lines']], ['head', 'body'])
        self.assertEqual(fitted[0]['context_lines'], [])


if __name__ == "__main__":
    unittest.main()
