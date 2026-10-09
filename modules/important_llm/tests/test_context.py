"""验证生产发现的整书恢复和保守的区域排除。"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from synthesis_fixtures import accepted_work

from book_extractor.models import NameDiscovery
from book_extractor.pipeline import (
    extract_book,
)


class CachedReviewClient:
    """使用模拟响应统计调用数，实际执行 Schema 校验和缓存读写。"""

    model = "offline"
    identity: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    max_output_tokens = 100
    last_call_attempts = 1

    def __init__(self) -> None:
        """记录正文发现调用，整书释义由独立测试桩完成。"""
        self.calls: list[str] = []

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """兼容执行日志归属，不产生HTTP请求。"""
        return nullcontext()

    def call(self, model: type, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """返回一个有实际来源的名称，供整书恢复测试使用。"""
        self.calls.append(model.__name__)
        return NameDiscovery(
            findings=[
                {
                    "name": "concept",
                    "evidence_ids": [next(iter(kwargs["context"]["evidence_ids"]))],
                }
            ],
            needs_context=False,
            context_reason="",
        )


class ContextTests(unittest.TestCase):
    """检查整书缓存复用和保守的区域排除规则。"""

    def setUp(self) -> None:
        """准备独立的临时运行目录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_whole_book_resume_skips_all_successful_calls(self) -> None:
        """整书检查点保留完成状态和审计邻文，恢复不重复已成功调用。"""
        source = self.root / "book.md"
        source.write_text(
            "# A\n\n" + "解释。" * 45 + "\n\n# B\n\n" + "补充。" * 45, encoding="utf-8"
        )
        client = CachedReviewClient()
        with patch("book_extractor.pipeline.synthesize", side_effect=accepted_work):
            result = extract_book(
                source, self.root / "run", client, chunk_tokens=50, workers=1
            )
            self.assertEqual(result["status"], "complete")
            calls = len(client.calls)
            extract_book(source, self.root / "run", client, chunk_tokens=50, workers=1)
        self.assertEqual(len(client.calls), calls)
        checkpoints = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (Path(result["directory"]) / "discovery").glob("*.json")
        ]
        self.assertTrue(all("review_neighbors" not in row for row in checkpoints))
        self.assertTrue(all(row["complete"] for row in checkpoints))


if __name__ == "__main__":
    unittest.main()
