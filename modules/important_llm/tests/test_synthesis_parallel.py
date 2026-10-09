"""验证书内批次并行的上界、稳定输出、缓存归属及致命错误停止边界。"""

import json
import tempfile
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Lock, local
from typing import Any
from unittest.mock import patch

import httpx
from openai import APIStatusError
from synthesis_fixtures import accepted_review

from book_extractor.markdown import Unit
from book_extractor.models import (
    Candidate,
    Finding,
    Grouping,
    IndependentSynthesis,
    SynthesisReview,
)
from book_extractor.synthesis import synthesize
from book_extractor.telemetry import EventLog


class ParallelClient:
    """按请求内容返回结果，并用同步原语确定性制造并行和反序完成。"""

    model = "parallel-offline"

    def __init__(
        self, stopped: Event | None = None, *, wait_for_c: bool = False
    ) -> None:
        """初始化并发计数；提供 stopped 时在 B 组触发致命服务错误。"""
        self.stopped = stopped
        self.wait_for_c = wait_for_c
        self.c_started = Event()
        self.a_saw_c = False
        self.barrier = Barrier(2)
        self.b_finished = Event()
        self.lock = Lock()
        self.thread = local()
        self.active = 0
        self.maximum = 0
        self.calls: list[tuple[str, str]] = []

    @contextmanager
    def scope(self, **metadata: Any) -> Iterator[None]:
        """在工作线程保留外层书籍和账本归属，再叠加请求阶段。"""
        previous = getattr(self.thread, "scope", {})
        self.thread.scope = {**previous, **metadata}
        try:
            yield
        finally:
            self.thread.scope = previous

    def event(self, kind: str, **metadata: Any) -> None:
        """写入真实本地账本，用于验证并行归属而不伪造 HTTP 事件。"""
        fields = dict(getattr(self.thread, "scope", {}))
        log = fields.pop("log", None)
        if log is not None:
            log.event(kind, **fields, **metadata)

    def call(self, model: type, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """只替代模型边界；保留生产缓存、校验和分组综合调用链。"""
        payload = json.loads(messages[-1]["content"])
        members = payload["candidates"]
        name = members[0]["name"]
        with self.lock:
            self.calls.append((model.__name__, name))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if model is Grouping:
                if name == "C":
                    self.c_started.set()
                if name in {"A", "B"}:
                    self.barrier.wait(timeout=5)
                if self.stopped is not None:
                    if name == "B":
                        raise APIStatusError(
                            "offline refusal",
                            response=httpx.Response(
                                400,
                                request=httpx.Request("POST", "https://stub.invalid"),
                            ),
                            body=None,
                        )
                    if name == "A" and not self.stopped.wait(5):
                        raise AssertionError("Fatal worker did not signal stop")
                return Grouping(
                    groups=[
                        {
                            "candidate_ids": [
                                member["candidate_id"] for member in members
                            ],
                            "status": "same",
                        }
                    ]
                )
            if name == "A" and self.wait_for_c:
                self.a_saw_c = self.c_started.wait(5)
                if not self.a_saw_c:
                    raise AssertionError("C did not start while A was waiting")
            if name == "A" and not self.b_finished.wait(5):
                raise AssertionError("B did not finish before A")
            if name == "B":
                self.b_finished.set()
            return accepted_review(
                evidence_by_candidate={
                    c["candidate_id"]: c["evidence_ids"] for c in members
                },
                findings=[
                    {
                        "support_reason": "测试来源支持范围",
                        "definition_supported": bool(f"{name}的解释"),
                        "confidence": "high",
                        "name": name,
                        "definition": f"{name}的解释",
                        "category": "概念",
                        "aliases": [],
                        "conditions": [],
                        "issues": [],
                        "evidence_ids": [source["id"] for source in payload["sources"]],
                        "draft_ids": [member["candidate_id"] for member in members],
                    }
                ],
            )
        finally:
            with self.lock:
                self.active -= 1


class ParallelSynthesisTests(unittest.TestCase):
    """并行只能改变执行时序，不能改变记录顺序、成员身份或错误边界。"""

    def setUp(self) -> None:
        """建立三个互不重叠的同名候选批次和独立缓存目录。"""
        # 固定每批两成员，保证三个完整桶不能合批，专门测试调度并行而非打包。
        group_limit = patch("book_extractor.synthesis.GROUP_SIZE", 2)
        group_limit.start()
        self.addCleanup(group_limit.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidates = [
            Candidate(
                name=name,
                aliases=[],
                issues=[],
                evidence_ids=[f"u{name}{index}"],
                candidate_id=f"c{name}{index}",
                chunk_id="chunk",
                scope=[name],
            )
            for name in ("A", "B", "C")
            for index in range(2)
        ]
        self.units = [
            Unit(c.evidence_ids[0], f"{c.name}的解释", 1, 1, c.scope, "paragraph")
            for c in self.candidates
        ]

    def test_parallel_order_bound_scope_and_cache(self) -> None:
        """两批真实重叠且不越界，反序完成仍按原序输出，恢复全部命中缓存。"""
        client = ParallelClient()
        log = EventLog(self.root / "events.jsonl")
        try:
            records, issues = synthesize(
                self.candidates,
                self.units,
                client,
                "book",
                "run",
                self.root / "cache",
                workers=2,
                scope={"log": log, "book_id": "book", "execution_id": "exec"},
            )
        finally:
            log.close()
        self.assertEqual(client.maximum, 2)
        self.assertEqual([record.name for record in records], ["A", "B", "C"])
        self.assertEqual(len(client.calls), 6)
        events = [json.loads(line) for line in log.path.read_text().splitlines()]
        self.assertTrue(events)
        self.assertTrue(
            all(
                event["book_id"] == "book" and event["execution_id"] == "exec"
                for event in events
            )
        )
        self.assertEqual(
            {event["stage"] for event in events}, {"grouping", "synthesis"}
        )
        cached = ParallelClient()
        again, repeated_issues = synthesize(
            self.candidates,
            self.units,
            cached,
            "book",
            "run",
            self.root / "cache",
            workers=1,
        )
        self.assertEqual((again, repeated_issues), (records, issues))
        self.assertEqual(cached.calls, [])

    def test_completion_refills_before_slow_first_job_finishes(self) -> None:
        """A 等待时 B 完成即补入 C，保持两槽上界及原始输出顺序。"""
        client = ParallelClient(wait_for_c=True)
        records, issues = synthesize(
            self.candidates,
            self.units,
            client,
            "book",
            "run",
            self.root,
            workers=2,
        )
        self.assertTrue(client.a_saw_c)
        self.assertEqual(client.maximum, 2)
        self.assertEqual([record.name for record in records], ["A", "B", "C"])
        self.assertEqual(len(client.calls), 6)
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))

    def test_fatal_stops_pending_and_followup_calls(self) -> None:
        """一批致命失败后，另一在途分组可结束，但不能继续综合或补入任务。"""
        stopped = Event()
        client = ParallelClient(stopped)
        with patch("book_extractor.synthesis.Event", return_value=stopped):
            with self.assertRaises(APIStatusError):
                synthesize(
                    self.candidates,
                    self.units,
                    client,
                    "book",
                    "run",
                    self.root,
                    workers=2,
                )
        self.assertCountEqual(client.calls, [("Grouping", "A"), ("Grouping", "B")])

    def test_verification_starts_before_slow_synthesis_finishes(self) -> None:
        """A 综合等待 B 复核，验证共用两槽仍能前进、保持顺序及复用缓存。"""
        verified_b = Event()

        class StreamingClient(ParallelClient):
            """在真实综合/复核模型边界制造阶段依赖，不改变缓存和校验路径。"""

            def call(
                self, model: type, messages: list[dict[str, Any]], **kwargs: Any
            ) -> Any:
                """B 复核释放 A 综合；只有整书屏障被移除时才不会超时。"""
                payload = json.loads(messages[-1]["content"])
                if model is Finding:
                    with self.lock:
                        self.calls.append((model.__name__, payload["name"]))
                    result = model.model_validate(
                        {
                            "support_reason": "测试来源支持范围",
                            "definition_supported": True,
                            "confidence": "high",
                            "name": payload["name"],
                            "definition": "$x=1$",
                            "category": "概念",
                            "aliases": [],
                            "conditions": [],
                            "issues": [],
                            "evidence_ids": [part["id"] for part in payload["sources"]],
                        },
                        context=kwargs["context"],
                    )
                    if payload["name"] == "B":
                        verified_b.set()
                    return result
                result = super().call(model, messages, **kwargs)
                if model is IndependentSynthesis:
                    for item in result.items:
                        item.finding.confidence = "medium"
                elif model is SynthesisReview:
                    for finding in result.findings:
                        finding.confidence = "medium"
                if model is not Grouping and payload["candidates"][0]["name"] == "A":
                    if not verified_b.wait(5):
                        raise AssertionError("B 复核被 A 综合阻塞")
                return result

        formula_units = [
            Unit(unit.id, unit.text + "$x=1$", 1, 1, unit.headings, "paragraph")
            for unit in self.units
        ]
        client = StreamingClient()
        with patch(
            "book_extractor.synthesis.ThreadPoolExecutor", wraps=ThreadPoolExecutor
        ) as pools:
            records, issues = synthesize(
                self.candidates,
                formula_units,
                client,
                "book",
                "run",
                self.root,
                workers=2,
            )
        pools.assert_called_once_with(max_workers=2)
        self.assertTrue(verified_b.is_set())
        self.assertEqual([record.name for record in records], ["A", "B", "C"])
        self.assertTrue(all(record.definition == "$x=1$" for record in records))
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))
        cached = StreamingClient()
        self.assertEqual(
            synthesize(
                self.candidates,
                formula_units,
                cached,
                "book",
                "run",
                self.root,
                workers=1,
            ),
            (records, issues),
        )
        self.assertEqual(cached.calls, [])


if __name__ == "__main__":
    unittest.main()
