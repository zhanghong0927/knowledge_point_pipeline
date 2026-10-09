"""复现共享客户端超额发送，以及验证跨书借用、轮转、取消与退避。"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from unittest.mock import patch

import test_llm
from openai.types.chat import ChatCompletion

from book_extractor.llm import ExtractionCancelledError


class SchedulingTests(unittest.TestCase):
    """用真实客户端调用链替换网络边界，观察实际进入HTTP的请求数。"""

    def test_shared_limit_and_idle_book_capacity(self) -> None:
        """单书可以借用全局额度，多书合计不能超过两槽，剩余请求必须排队。"""
        client, _ = test_llm.LLMTests().make_client(
            [test_llm.LLMTests.reply()], max_connections=2
        )
        release = Event()
        two_entered = Event()
        lock = Lock()
        active = peak = entered = 0

        def create(**kwargs: Any) -> ChatCompletion:
            """阻塞网络边界直到主测试放行，并记录同时进入的数量。"""
            nonlocal active, peak, entered
            with lock:
                active += 1
                entered += 1
                peak = max(peak, active)
                if entered >= 2:
                    two_entered.set()
            release.wait(3)
            with lock:
                active -= 1
            return ChatCompletion.model_validate(test_llm.LLMTests.reply()[1])

        def invoke(book: str) -> None:
            """以书籍身份调用共享客户端，不预先切分请求额度。"""
            with client.scope(book_id=book):
                client.call(
                    test_llm.Selection,
                    [{"role": "user", "content": "p2"}],
                    context={"allowed_ids": ["p2"]},
                )

        try:
            with (
                patch.object(client, "_create", side_effect=create),
                ThreadPoolExecutor(max_workers=6) as pool,
            ):
                futures = [
                    pool.submit(invoke, book) for book in ["A", "A", "A", "B", "B", "B"]
                ]
                self.assertTrue(two_entered.wait(2))
                # 排队路径由真实调度器状态确认，不能把线程未调度误认成受限。
                for _ in range(200):
                    if (
                        entered > 2
                        or getattr(client, "request_queue_state", {}).get("waiting", 0)
                        >= 4
                    ):
                        break
                    Event().wait(0.005)
                release.set()
                for future in futures:
                    future.result()
            self.assertEqual(peak, 2)
            self.assertEqual(entered, 6)
        finally:
            release.set()
            client.close()

    def wait_queue(self, client: Any, count: int) -> None:
        """有界等待count个请求真正排队；失败说明测试没有覆盖争用路径。"""
        deadline = monotonic() + 2
        while client.request_queue_state["waiting"] < count and monotonic() < deadline:
            Event().wait(0.005)
        self.assertEqual(client.request_queue_state["waiting"], count)

    def test_book_rotation_and_cancelled_waiter_cleanup(self) -> None:
        """一书有积压仍让另一书轮转执行；取消排队请求后槽位与票据全部释放。"""
        for cancel in (False, True):
            client, _ = test_llm.LLMTests().make_client(
                [test_llm.LLMTests.reply()], max_connections=1
            )
            order: list[str] = []

            def enter(book: str) -> None:
                """绑定书籍身份进入真实准入队列，记录获得槽位的顺序。"""
                with client.scope(book_id=book), client._request_slot():
                    order.append(book)

            with ThreadPoolExecutor(max_workers=3) as pool:
                with client.scope(book_id="held"), client._request_slot():
                    futures = []
                    for i, book in enumerate(("A", "A", "B")):
                        futures.append(pool.submit(enter, book))
                        self.wait_queue(client, i + 1)
                    if cancel:
                        client.cancel()
                for future in futures:
                    if cancel:
                        with self.assertRaises(ExtractionCancelledError):
                            future.result(timeout=2)
                    else:
                        future.result(timeout=2)
            self.assertEqual(order, [] if cancel else ["A", "B", "A"])
            self.assertEqual(client.request_queue_state["active"], 0)
            self.assertEqual(client.request_queue_state["waiting"], 0)
            client.close()

    def test_retry_backoff_returns_request_slot(self) -> None:
        """504请求进入退避后，另一书仍可使用唯一HTTP槽位并完成。"""
        client, requests = test_llm.LLMTests().make_client(
            [(504, {"error": {"message": "gateway"}}), test_llm.LLMTests.reply()],
            max_connections=1,
        )
        sleeping, resume = Event(), Event()

        def sleep(_: float) -> None:
            """固定停留在退避阶段，以便验证槽位是否已归还。"""
            sleeping.set()
            if not resume.wait(3):
                raise AssertionError("退避阻塞了其他书")

        def invoke(book: str) -> None:
            """执行一个带可验证响应的真实Instructor调用。"""
            with client.scope(book_id=book):
                client.call(
                    test_llm.Selection,
                    [{"role": "user", "content": "p2"}],
                    context={"allowed_ids": ["p2"]},
                )

        try:
            with (
                patch.object(client._cancelled, "wait", side_effect=sleep),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(invoke, "A")
                self.assertTrue(sleeping.wait(2))
                self.assertEqual(client.request_queue_state["active"], 0)
                second = pool.submit(invoke, "B")
                second.result(timeout=2)
                resume.set()
                first.result(timeout=2)
            self.assertEqual(len(requests), 3)
        finally:
            resume.set()
            client.close()
