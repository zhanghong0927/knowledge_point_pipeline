"""验证选择性核验的有界补位、输出顺序、独立缓存与致命错误边界。"""

import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, local
from typing import Any
from unittest.mock import patch

from book_extractor.markdown import Unit
from book_extractor.models import Finding, Record
from book_extractor.synthesis import _verify_records
from book_extractor.telemetry import TelemetryWriteError


class ConcurrentVerifier:
    """用事件制造慢首项，并在模型边界记录并发及线程上下文。"""

    model = "offline"

    def __init__(self, mode: str = "normal") -> None:
        """选择普通、滚动补位、单项失败或致命失败场景。"""
        self.mode = mode
        self.stopped = Event()
        self.third = Event()
        self.second = Event()
        self.lock = Lock()
        self.thread = local()
        self.calls: list[str] = []
        self.scopes: list[dict[str, Any]] = []
        self.active = 0
        self.peak = 0

    @contextmanager
    def scope(self, **metadata: Any) -> Iterator[None]:
        """在线程内叠加统计上下文，退出时恢复外层归属。"""
        previous = getattr(self.thread, "scope", {})
        self.thread.scope = {**previous, **metadata}
        try:
            yield
        finally:
            self.thread.scope = previous

    def call(
        self, model: type[Finding], messages: list[dict[str, Any]], **kwargs: Any
    ) -> Finding:
        """返回源内公式，或按场景抛出单项异常；不访问网络。"""
        payload = json.loads(messages[-1]["content"])
        name = payload["name"]
        with self.lock:
            self.calls.append(name)
            self.scopes.append(dict(self.thread.scope))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if name == "乙":
                self.second.set()
            if name == "丙":
                self.third.set()
            if self.mode == "rolling" and name == "甲":
                if not self.third.wait(5):
                    raise AssertionError("第三项没有及时补位")
            if self.mode == "fatal" and name == "甲":
                if not self.second.wait(5):
                    raise AssertionError("第二项没有启动")
                raise TelemetryWriteError("offline fatal")
            if self.mode == "fatal" and name == "乙":
                if not self.stopped.wait(5):
                    raise AssertionError("致命错误没有停止补位")
            if self.mode == "failure" and name == "乙":
                raise RuntimeError("offline failure")
            return model.model_validate(
                {
                    "support_reason": "测试来源支持范围",
                    "definition_supported": True,
                    "confidence": "medium",
                    "name": name,
                    "definition": "$x=1$",
                    "category": "概念",
                    "aliases": [],
                    "conditions": [],
                    "issues": [],
                    "evidence_ids": [payload["sources"][0]["id"]],
                },
                context=kwargs["context"],
            )
        finally:
            with self.lock:
                self.active -= 1


class VerificationParallelTests(unittest.TestCase):
    """通过三个独立来源验证真实核验缓存和调度路径。"""

    def setUp(self) -> None:
        """创建三个均需公式核验的记录及独立临时缓存。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.records = []
        self.source = {}
        for index, name in enumerate("甲乙丙"):
            key = f"u{index}"
            self.source[key] = Unit(key, f"{name}：$x=1$", 1, 1, [], "paragraph")
            self.records.append(
                Record(
                    support_reason="测试来源支持范围",
                    definition_supported=True,
                    confidence="medium",
                    name=name,
                    definition="$x=1$",
                    category="概念",
                    aliases=[],
                    conditions=[],
                    issues=[],
                    evidence_ids=[key],
                    record_id=f"r{index}",
                    candidate_ids=[f"c{index}"],
                    book_id="book",
                    run_id="run",
                    scope=[],
                )
            )

    def test_rolling_preserves_order_and_scope(self) -> None:
        """首项等第三项启动仍能完成，证明补位不等整波并保留归属。"""
        client = ConcurrentVerifier("rolling")
        records, errors = _verify_records(
            self.records,
            self.source,
            client,
            self.directory,
            workers=2,
            scope={"book_id": "book", "execution_id": "execution"},
        )
        self.assertFalse(errors)
        self.assertEqual([record.name for record in records], list("甲乙丙"))
        self.assertCountEqual(client.calls, list("甲乙丙"))
        self.assertEqual(client.peak, 2)
        self.assertTrue(all(row["book_id"] == "book" for row in client.scopes))
        self.assertTrue(all(row["stage"] == "verification" for row in client.scopes))
        self.assertTrue(
            all(row["execution_id"] == "execution" for row in client.scopes)
        )

    def test_failure_recovers_only_failed_record(self) -> None:
        """普通单项失败不漏记录，恢复命中其他缓存且仅重做失败项。"""
        first = ConcurrentVerifier("failure")
        records, errors = _verify_records(
            self.records, self.source, first, self.directory, workers=3
        )
        self.assertEqual(len(records), 3)
        self.assertEqual(len(errors), 1)
        second = ConcurrentVerifier()
        restored, errors = _verify_records(
            self.records, self.source, second, self.directory, workers=3
        )
        self.assertFalse(errors)
        self.assertEqual(second.calls, ["乙"])
        self.assertEqual([record.name for record in restored], list("甲乙丙"))

    def test_fatal_prevents_replenishment(self) -> None:
        """致命账本错误向外传播，排队之外的第三项不会调用模型。"""
        client = ConcurrentVerifier("fatal")
        with (
            patch("book_extractor.synthesis.Event", return_value=client.stopped),
            self.assertRaises(TelemetryWriteError),
        ):
            _verify_records(
                self.records, self.source, client, self.directory, workers=2
            )
        self.assertNotIn("丙", client.calls)
