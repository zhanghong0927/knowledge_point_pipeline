"""验证真实缓存调用边界的短编号传输、来源隔离和原编号恢复。"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from book_extractor.models import Grouping
from book_extractor.synthesis import _cached_call


class TransportTests(unittest.TestCase):
    """模型只见短编号，缓存和审计仍保留原编号，非法响应仍被拒绝。"""

    def test_short_ids_preserve_prose_and_cache_identity(self) -> None:
        """真实边界压缩ID、保持原文且恢复零调用；伪造原长ID也不能绕过线端校验。"""
        original = ["c000001-" + "a" * 20, "c000002-" + "b" * 20]
        context = {"candidate_ids": original, "comparison_buckets": [original]}
        payload = {
            "candidates": [{"candidate_id": key, "name": key} for key in original],
            "comparison_buckets": [original],
            "sources": [
                {"id": original[0], "text": original[0], "candidate_ids": original}
            ],
        }

        class Client:
            """只模拟模型边界，缓存与校验执行生产代码。"""

            model = "transport"
            calls = 0
            forged = False

            def call(self, model: type, messages: list, **kwargs: Any) -> Any:
                """检查实际请求编号及原文，然后按短编号给出相反顺序的合法组。"""
                self.calls += 1
                wire = json.loads(messages[-1]["content"])
                assert [c["candidate_id"] for c in wire["candidates"]] == ["c1", "c2"]
                assert wire["sources"][0]["text"] == original[0]
                assert wire["sources"][0]["id"] == original[0]
                assert wire["candidates"][0]["name"] == original[0]
                assert kwargs["context"]["comparison_buckets"] == [["c1", "c2"]]
                return Grouping(
                    groups=[
                        {
                            "candidate_ids": original if self.forged else ["c2", "c1"],
                            "status": "same",
                        }
                    ]
                )

        client = Client()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            answer = _cached_call(client, Grouping, "group", payload, context, path)
            self.assertEqual(answer.groups[0].candidate_ids, original[::-1])
            self.assertEqual(
                _cached_call(client, Grouping, "group", payload, context, path), answer
            )
            self.assertEqual(client.calls, 1)
            saved = json.loads(next(path.glob("*.json")).read_text())
            self.assertEqual(saved["groups"][0]["candidate_ids"], original[::-1])
            client.forged = True
            with self.assertRaises(ValidationError):
                _cached_call(client, Grouping, "changed", payload, context, path)
