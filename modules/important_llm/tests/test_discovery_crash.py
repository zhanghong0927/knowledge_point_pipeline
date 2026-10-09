"""用离线响应模拟草稿落盘后中断、恢复及再次中断。"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from synthesis_fixtures import accepted_work

from book_extractor import pipeline
from book_extractor.models import NameDiscovery


class Client:
    """模拟首次草稿成功、补充请求中断以及后续恢复响应。"""

    model = "offline"
    max_output_tokens = 100
    extra_body: dict[str, Any] = {}
    identity = {"service": "offline"}
    attempts = 3
    last_call_attempts = 1

    def __init__(self, interrupt: bool) -> None:
        """用 interrupt 控制是否在第二次请求中断，并初始化请求记录。"""
        self.interrupt = interrupt
        self.payloads: list[dict[str, Any]] = []

    def scope(self, **kwargs: Any) -> nullcontext[None]:
        """接收管线归账参数 kwargs，返回不产生网络事件的空上下文。"""
        return nullcontext()

    def call(
        self,
        model: type[NameDiscovery],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any],
        attempts: int = 3,
    ) -> NameDiscovery:
        """解析 messages 并按 context 校验草稿；

        启用中断时在第二次调用抛出 KeyboardInterrupt。
        """
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if self.interrupt and len(self.payloads) == 2:
            raise KeyboardInterrupt("simulated interruption before follow-up response")
        findings = (
            [
                {
                    "name": "saved_candidate",
                    "evidence_ids": [payload["core"][0]["id"]],
                }
            ]
            if self.interrupt
            else []
        )
        return model.model_validate(
            {
                "findings": findings,
                "needs_context": self.interrupt,
                "context_reason": "requires continuation",
            },
            context=context,
        )


class ContinuedClient(Client):
    """只响应指定核心块的补充请求，并记录每次请求预算。"""

    def __init__(self, target: list[dict[str, str]], mode: str) -> None:
        """保存 target 核心块和 mode 响应模式，并初始化预算记录。"""
        super().__init__(False)
        self.target = target
        self.mode = mode
        self.budgets: list[int] = []

    def call(
        self,
        model: type[NameDiscovery],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any],
        attempts: int = 3,
    ) -> NameDiscovery:
        """记录 messages 和 attempts；

        目标块按 mode 返回补充草稿或抛错，并用 context 校验。
        """
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        self.budgets.append(attempts)
        target = payload["core"] == self.target
        if target and self.mode == "fail":
            raise RuntimeError("offline follow-up failure")
        findings = (
            [
                {
                    "name": "followup_candidate",
                    "evidence_ids": [payload["core"][0]["id"]],
                }
            ]
            if target
            else []
        )
        return model.model_validate(
            {
                "findings": findings,
                "needs_context": target and self.mode == "unresolved",
                "context_reason": "still missing"
                if target and self.mode == "unresolved"
                else "",
            },
            context=context,
        )


class RecoveryTests(unittest.TestCase):
    """验证实际管线的草稿恢复，不访问真实模型服务。"""

    def setUp(self) -> None:
        """建立临时书籍和审计列表，替换复核与综合以隔离恢复逻辑。"""
        self.pipeline = pipeline
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "book.md"
        self.source.write_text(
            "".join(f"第{index}处惯性描述物体运动状态。" for index in range(15)),
            encoding="utf-8",
        )
        replacement = patch.object(
            self.pipeline, "synthesize", side_effect=accepted_work
        )
        replacement.start()
        self.addCleanup(replacement.stop)

    def run_book(self, client: Client) -> dict[str, Any]:
        """用给定 client 串行运行临时书籍，返回管线执行摘要。"""
        return self.pipeline.extract_book(
            self.source, self.root / "runs", client, workers=1, chunk_tokens=40
        )

    def interrupt_after_first(self) -> tuple[Path, list[dict[str, str]]]:
        """在首次草稿原子落盘后中断补充请求，返回检查点路径及原始核心片段。"""
        first = Client(True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_book(first)
        path = next((self.root / "runs").glob("*/discovery/c000001.json"))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(saved["draft_ready"])
        self.assertEqual(len(saved["raw_discovery"]["findings"]), 1)
        self.assertEqual(len(first.payloads), 2)
        return path, first.payloads[0]["core"]

    def test_checkpoint_write_failure_stops_discovery(self) -> None:
        """首轮模型成功后写盘失败必须传播，不能降为partial继续请求其他块。"""
        client = Client(False)
        original = self.pipeline.write_json
        failed = False

        def write(path: Path, value: Any) -> None:
            """只让首次发现检查点写入失败，其他写入照常，暴露吞错后继续运行的路径。"""
            nonlocal failed
            if path.parent.name == "discovery" and not failed:
                failed = True
                raise PermissionError("checkpoint unavailable")
            original(path, value)

        with patch.object(self.pipeline, "write_json", side_effect=write):
            with self.assertRaises(PermissionError):
                self.run_book(client)
        self.assertTrue(failed)
        self.assertEqual(len(client.payloads), 1)

    def test_resume_keeps_first_and_follows_up_once(self) -> None:
        """恢复只补充一次上下文，保留两轮候选；再次恢复不再调用模型。"""
        path, target = self.interrupt_after_first()
        resumed = ContinuedClient(target, "ok")
        final = self.run_book(resumed)
        calls = [payload for payload in resumed.payloads if payload["core"] == target]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["neighbors"])
        self.assertEqual(resumed.budgets[0], 3)
        names = ["saved_candidate", "followup_candidate"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [candidate["name"] for candidate in saved["candidates"]], names
        )
        self.assertEqual(final["status"], "complete")
        cached = ContinuedClient(target, "ok")
        self.run_book(cached)
        self.assertEqual(cached.payloads, [])

    def test_failed_followup_retains_pending_names(self) -> None:
        """名称补文失败仍保留首轮原始名称，未完成的发现等待恢复。"""
        path, target = self.interrupt_after_first()
        resumed = ContinuedClient(target, "fail")
        final = self.run_book(resumed)
        self.assertEqual(
            len([payload for payload in resumed.payloads if payload["core"] == target]),
            1,
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "partial")
        self.assertEqual(saved["candidates"], [])
        self.assertEqual(
            [anchor["name"] for anchor in saved["raw_discovery"]["findings"]],
            ["saved_candidate"],
        )
        self.assertNotIn("definition", saved["raw_discovery"]["findings"][0])
        self.assertFalse(saved["complete"])
        self.assertFalse(saved["draft_ready"])
        self.assertEqual(saved["draft_neighbors"], [])
        recovered = ContinuedClient(target, "ok")
        completed = self.run_book(recovered)
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(
            sum(payload["core"] == target for payload in recovered.payloads), 1
        )

    def test_saved_followup_does_not_become_third_round(self) -> None:
        """第二轮结果落盘后再次中断，恢复直接保存证据，不能产生第三轮发现。"""
        path, target = self.interrupt_after_first()
        writer = self.pipeline.write_json

        def interrupt(path: Path, value: Any) -> None:
            """先把 value 写入 path，再在目标补充检查点落盘后模拟中断。"""
            writer(path, value)
            if (
                path.name == "c000001.json"
                and isinstance(value, dict)
                and value.get("draft_neighbors")
                and value.get("draft_ready") is False
            ):
                raise KeyboardInterrupt("after successful follow-up checkpoint")

        with (
            patch.object(self.pipeline, "write_json", side_effect=interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_book(ContinuedClient(target, "unresolved"))
        resumed = ContinuedClient(target, "ok")
        final = self.run_book(resumed)
        self.assertFalse(any(payload["core"] == target for payload in resumed.payloads))
        self.assertEqual(final["status"], "partial")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [candidate["name"] for candidate in saved["candidates"]],
            ["saved_candidate", "followup_candidate"],
        )
        self.assertTrue(
            all("context_incomplete" in c["issues"] for c in saved["candidates"])
        )


if __name__ == "__main__":
    unittest.main()
