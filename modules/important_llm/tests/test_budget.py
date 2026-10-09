"""离线验证空白分块处理和仅允许一层拆分的请求预算恢复。"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from synthesis_fixtures import accepted_work

from book_extractor.llm import ContextBudgetError
from book_extractor.markdown import Chunk
from book_extractor.models import NameDiscovery
from book_extractor.pipeline import extract_book, neighbor_payloads, write_json


class BudgetClient:
    """模拟请求边界，按指定调用序号触发预算不足。"""

    model = "fake"
    identity: dict[str, Any] = {}
    max_output_tokens = 100
    extra_body: dict[str, Any] = {}
    stats: dict[str, int] = {}
    last_call_attempts = 1

    def __init__(self, failures: set[int]) -> None:
        """保存 failures 指定的失败调用序号，并初始化请求记录。"""
        self.failures = failures
        self.payloads: list[dict[str, Any]] = []

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """接收归账参数 kwargs，返回不生成网络统计的空上下文。"""
        return nullcontext()

    def call(
        self, model: type[NameDiscovery], messages: list[dict[str, Any]], **kwargs: Any
    ) -> NameDiscovery:
        """记录 messages 中的请求；命中失败序号时抛出预算错误，否则返回空名称结果。"""
        self.payloads.append(json.loads(messages[-1]["content"]))
        if len(self.payloads) in self.failures:
            raise ContextBudgetError("fake assembled request too large")
        return NameDiscovery(findings=[], needs_context=False, context_reason="")


class BudgetTests(unittest.TestCase):
    """通过临时源文件验证整书入口的预算、检查点和恢复行为。"""

    def setUp(self) -> None:
        """建立独立临时目录，替换与预算测试无关的复核和综合调用。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.md"
        self.source.write_text("# A\n\n正文讲解。\n\n", encoding="utf-8")
        stub = patch("book_extractor.pipeline.synthesize", side_effect=accepted_work)
        stub.start()
        self.addCleanup(stub.stop)

    def run_book(self, client: BudgetClient) -> dict[str, Any]:
        """用给定 client 串行提取临时书籍，返回整书执行摘要。"""
        return extract_book(self.source, self.root / "out", client, workers=1)

    def test_whitespace_only_chunk_skips_model_without_changing_source(self) -> None:
        """空白核心不调用模型，源文、单元和分块中的空白仍逐字保留。"""
        text = "# A\n\n\n"
        self.source.write_text(text, encoding="utf-8", newline="")
        client = BudgetClient(set())
        chunks = [
            Chunk("c1", ["u000001"], "# A\n", ["A"]),
            Chunk("c2", ["u000002"], "\n\n", ["A"]),
        ]
        with patch("book_extractor.pipeline.chunk_units", return_value=chunks):
            result = self.run_book(client)
        directory = Path(result["directory"])
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual((directory / "source.md").read_text(encoding="utf-8"), text)
        units = json.loads((directory / "units.json").read_text(encoding="utf-8"))
        self.assertEqual("".join(u["text"] for u in units), text)
        saved_chunks = json.loads(
            (directory / "chunks.json").read_text(encoding="utf-8")
        )
        self.assertEqual("".join(c["text"] for c in saved_chunks), text)
        skipped = json.loads(
            (directory / "discovery/c2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped["skip_reason"], "whitespace_only")
        self.assertTrue(skipped["complete"])
        self.assertEqual(result["output_statistics"]["successful_discovery_blocks"], 1)
        self.assertEqual(result["output_statistics"]["empty_discovery_blocks"], 1)
        self.assertEqual(
            neighbor_payloads([[{"id": "a", "text": "\n"}], []], 1, 512), []
        )

    def test_budget_rejection_splits_once(self) -> None:
        """父请求超出预算后仅拆为两个子请求，并能完成整书。"""
        client = BudgetClient({1})
        result = self.run_book(client)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(client.payloads), 3)
        self.assertEqual(result["output_statistics"]["successful_discovery_blocks"], 2)
        self.assertEqual(result["output_statistics"]["empty_discovery_blocks"], 2)
        self.assertEqual(result["output_statistics"]["discovered_names"], 0)
        resumed = BudgetClient(set())
        cached = self.run_book(resumed)
        self.assertEqual(resumed.payloads, [])
        self.assertEqual(cached["output_statistics"], result["output_statistics"])
        event_path = (
            Path(cached["directory"])
            / "executions"
            / cached["execution_id"]
            / "events.jsonl"
        )
        observations = [
            row
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if (row := json.loads(line))["event"] == "discovery_result"
        ]
        self.assertEqual(len(observations), 2)
        self.assertTrue(
            all(row["from_cache"] and row["is_empty"] for row in observations)
        )
        self.assertTrue(all("-s" in row["work_id"] for row in observations))
        self.assertTrue(
            all(
                p["text"].strip()
                for request in client.payloads
                for p in request["core"]
            )
        )

    def test_child_budget_failure_stays_partial_and_resumes_only_child(self) -> None:
        """子块不能再次拆分；恢复时只重试失败子块，复用成功兄弟块。"""
        client = BudgetClient({1, 2})
        result = self.run_book(client)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(client.payloads), 3)
        self.assertEqual(result["errors"][0]["error"], "ContextBudgetError")
        self.assertEqual(result["output_statistics"]["successful_discovery_blocks"], 1)
        self.assertEqual(result["output_statistics"]["empty_discovery_blocks"], 1)
        resumed = BudgetClient(set())
        completed = self.run_book(resumed)
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(len(resumed.payloads), 1)

    def test_split_parent_resume_preserves_draft_neighbors(self) -> None:
        """拆分父块的中间及最终检查点都保留原始草稿和邻文。"""
        first = self.run_book(BudgetClient({1, 2}))
        parent = Path(first["directory"]) / "discovery/c000001.json"
        checkpoint = json.loads(parent.read_text(encoding="utf-8"))
        draft = NameDiscovery(
            findings=[], needs_context=True, context_reason="needs adjacent explanation"
        ).model_dump()
        neighbors = [{"id": "u000003", "text": "正文讲解。\n"}]
        checkpoint.update(
            raw_discovery=draft,
            draft_ready=True,
            draft_neighbors=neighbors,
            draft_context_reason="needs adjacent explanation",
        )
        write_json(parent, checkpoint)
        snapshots: list[dict[str, Any]] = []

        def capture(path: Path, value: Any) -> None:
            """记录指定 path 的检查点 value，再调用真实持久化函数完成写入。"""
            if path == parent:
                snapshots.append(value)
            write_json(path, value)

        resumed = BudgetClient(set())
        with patch("book_extractor.pipeline.write_json", side_effect=capture):
            result = self.run_book(resumed)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(resumed.payloads), 1)
        self.assertGreaterEqual(len(snapshots), 2)
        for saved in snapshots:
            self.assertEqual(saved["raw_discovery"], draft)
            self.assertEqual(saved["draft_neighbors"], neighbors)
            self.assertEqual(
                saved["draft_context_reason"], checkpoint["draft_context_reason"]
            )


if __name__ == "__main__":
    unittest.main()
