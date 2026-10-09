"""离线验证名称校验耗尽后的拆分及证据候选恢复。"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from instructor.core.exceptions import InstructorRetryException
from openai import APIStatusError
from pydantic import ValidationError
from synthesis_fixtures import accepted_work

from book_extractor.models import NameDiscovery
from book_extractor.pipeline import exhausted_validation, extract_book
from book_extractor.telemetry import TelemetryWriteError


def invalid_output() -> InstructorRetryException:
    """构造带真实 Pydantic 原因异常的 InstructorRetryException 并返回。"""
    error = InstructorRetryException("invalid output", n_attempts=3, total_usage=None)
    error.__cause__ = ValidationError.from_exception_data(
        "NameDiscovery", [{"type": "missing", "loc": ("findings",), "input": {}}]
    )
    return error


class FakeClient:
    """按调用序号模拟名称校验耗尽，提供实际尝试次数供拆分策略判断。"""

    model = "offline"
    identity: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    max_output_tokens = 100
    attempts = 3
    last_call_attempts = 1

    def __init__(self, failures: set[int] | None = None) -> None:
        """保存会耗尽校验预算的 failures 序号，初始化名称调用计数。"""
        self.failures = failures or set()
        self.calls = 0

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """接收归账参数 kwargs，返回不记录虚构 HTTP 请求的空上下文。"""
        return nullcontext()

    def call(
        self,
        model: type[NameDiscovery],
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> NameDiscovery:
        """记录名称调用；命中失败序号则耗尽预算，否则返回引用核心的名称。"""
        self.calls += 1
        if self.calls in self.failures:
            self.last_call_attempts = 3
            raise invalid_output()
        self.last_call_attempts = 1
        payload = json.loads(messages[-1]["content"])
        return model.model_validate(
            {
                "findings": [
                    {"name": "正文说明", "evidence_ids": [payload["core"][0]["id"]]}
                ],
                "needs_context": False,
                "context_reason": "",
            },
            context=kwargs.get("context"),
        )


class ValidationSplitTests(unittest.TestCase):
    """名称失败允许一层拆分；成功名称直接落盘证据候选。"""

    def setUp(self) -> None:
        """建立单个逻辑源块，仅替换最终综合以隔离名称拆分策略。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.md"
        self.source.write_text("正文说明。" * 20, encoding="utf-8")
        for stub in (
            patch("book_extractor.pipeline.synthesize", side_effect=accepted_work),
        ):
            stub.start()
            self.addCleanup(stub.stop)

    def test_parent_exhaustion_splits_then_children_succeed(self) -> None:
        """父块名称提取耗尽三次校验修复后，可尝试两个更小的核心块。"""
        client = FakeClient({1})
        result = extract_book(self.source, self.root / "runs", client, workers=1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 3)
        self.assertEqual(result["candidates"], 2)
        client.last_call_attempts = 2
        self.assertFalse(exhausted_validation(invalid_output(), client, 3))
        self.assertTrue(exhausted_validation(invalid_output(), client, 2))
        other = invalid_output()
        other.__cause__ = ValueError("not Pydantic")
        self.assertFalse(exhausted_validation(other, client, 2))

    def test_child_exhaustion_remains_partial_without_grandchildren(self) -> None:
        """子块各有有限名称校验预算，但再次耗尽后保持 partial，不产生孙块。"""
        client = FakeClient({1, 2, 3})
        result = extract_book(self.source, self.root / "runs", client, workers=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(client.calls, 3)
        names = [
            path.stem
            for path in (Path(result["directory"]) / "discovery").glob("*.json")
        ]
        self.assertEqual(len(names), 3)
        self.assertTrue(all(name.count("-s") <= 1 for name in names))

    def test_successful_names_are_complete_evidence_without_local_definition(
        self,
    ) -> None:
        """名称发现成功即完成块证据，恢复不增加调用，也不生成局部释义。"""
        client = FakeClient()
        result = extract_book(self.source, self.root / "runs", client, workers=1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 1)
        checkpoints = list((Path(result["directory"]) / "discovery").glob("*.json"))
        self.assertEqual(len(checkpoints), 1)
        saved = json.loads(checkpoints[0].read_text(encoding="utf-8"))
        self.assertTrue(saved["complete"])
        self.assertNotIn("definition", saved["candidates"][0])
        self.assertNotIn("pending_definition_names", saved)
        resumed = FakeClient()
        final = extract_book(self.source, self.root / "runs", resumed, workers=1)
        self.assertEqual(resumed.calls, 0)
        self.assertEqual(final["candidates"], 1)

    def test_service_refusal_and_telemetry_never_split(self) -> None:
        """名称阶段服务拒绝和统计故障始终在父块中止，不能通过拆分绕过。"""
        errors = [
            APIStatusError(
                "refused",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://fake.invalid")
                ),
                body=None,
            ),
            TelemetryWriteError("disk full"),
        ]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                client = FakeClient()
                with patch.object(client, "call", side_effect=error) as call:
                    with self.assertRaises(type(error)):
                        extract_book(
                            self.source, self.root / str(index), client, workers=1
                        )
                self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
