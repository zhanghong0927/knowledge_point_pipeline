"""离线验证并发批量提取的调度、结果落盘及失败退出码。"""

import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from openai import APIStatusError

from book_extractor.cli import create_parser, main, run_books
from book_extractor.llm import ExtractionCancelledError
from book_extractor.telemetry import TelemetryWriteError


class CliTests(unittest.TestCase):
    """检查多本书并发运行时批次结果完整且客户端正常关闭。"""

    def test_language_screen_skips_without_extraction(self) -> None:
        """非中英文及无法判断的书籍不发提取调用，批次摘要保留明确跳过原因。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = create_parser().parse_args(
                ["--input", "unused.md", "--output", str(root)]
            )
            books = [
                {"local_name": f"{lang}.md", "language": lang}
                for lang in ["other", "undetermined", "zh", "en", "zh-en"]
            ]
            completed = {
                "run_id": "test",
                "status": "complete",
                "chunks": 1,
                "candidates": 1,
                "records": 1,
                "errors": [],
            }
            with patch(
                "book_extractor.cli.extract_book", return_value=completed
            ) as extract:
                rows = run_books(books, root, args, MagicMock())
            self.assertEqual(extract.call_count, 3)
            self.assertEqual(
                [r["status"] for r in rows],
                ["skipped", "skipped", "complete", "complete", "complete"],
            )
            self.assertTrue(
                all(r["reason"] == "language_not_supported" for r in rows[:2])
            )

    def test_interrupt_cancels_before_waiting_for_book_workers(self) -> None:
        """SIGINT先通知工作线程取消，才能排空线程池并写出中断与未启动书。"""
        started, cancelled = Event(), Event()
        client = MagicMock()
        client.cancel.side_effect = cancelled.set

        def extract(*args: Any, **kwargs: Any) -> Any:
            """模拟在途书等待取消；若CLI先等待线程池，此测试会确定性超时。"""
            started.set()
            if not cancelled.wait(5):
                raise AssertionError("线程池等待前未触发取消")
            raise ExtractionCancelledError("cancelled")

        def interrupt(*args: Any, **kwargs: Any) -> Any:
            """等书籍已经启动再模拟主线程KeyboardInterrupt。"""
            if not started.wait(5):
                raise AssertionError("书籍没有启动")
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = create_parser().parse_args(
                ["--input", "book.md", "--output", str(root)]
            )
            with (
                patch("book_extractor.cli.extract_book", side_effect=extract),
                patch("book_extractor.cli.wait", side_effect=interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                run_books(
                    [{"local_name": "a.md"}, {"local_name": "b.md"}],
                    root,
                    args,
                    client,
                )
            batch = json.loads((root / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [row["status"] for row in batch["books"]],
                ["interrupted", "not_started"],
            )
            self.assertTrue(cancelled.is_set())

    def test_invalid_manifest_fails_before_client_creation(self) -> None:
        """整份清单先验格式检查，后半条目损坏时不得先为前半发送请求。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            path.write_text(json.dumps({"books": [{"local_name": "ok.md"}, {}]}))
            with (
                patch("sys.argv", ["book-extract", "--manifest", str(path)]),
                patch("book_extractor.cli.LLMClient") as client,
            ):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                client.assert_not_called()

    def test_fatal_failure_stops_batch_and_accounts_for_unstarted_books(self) -> None:
        """认证或账本失败后不继续书目，未开始书仍有明确状态，不能伪装全部处理。"""
        response = httpx.Response(401, request=httpx.Request("POST", "https://test/v1"))
        failures = [
            APIStatusError("unauthorized", response=response, body=None),
            APIStatusError(
                "budget exceeded",
                response=httpx.Response(402, request=response.request),
                body=None,
            ),
            TelemetryWriteError("accounting unavailable"),
            PermissionError("output storage unavailable"),
        ]
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    manifest = root / "input.json"
                    manifest.write_text(
                        json.dumps(
                            {"books": [{"local_name": f"{i}.md"} for i in range(3)]}
                        ),
                        encoding="utf-8",
                    )
                    with (
                        patch(
                            "sys.argv",
                            [
                                "book-extract",
                                "--manifest",
                                str(manifest),
                                "--output",
                                str(root / "runs"),
                            ],
                        ),
                        patch("book_extractor.cli.load_service", return_value={}),
                        patch("book_extractor.cli.LLMClient", return_value=MagicMock()),
                        patch(
                            "book_extractor.cli.extract_book", side_effect=failure
                        ) as call,
                    ):
                        with self.assertRaises(SystemExit):
                            main()
                    self.assertEqual(call.call_count, 1)
                    batch = json.loads((root / "runs/batch.json").read_text("utf-8"))
                    self.assertEqual(
                        [row["status"] for row in batch["books"]],
                        ["failed", "not_started", "not_started"],
                    )

    def test_batch_keeps_success_and_failure(self) -> None:
        """单本失败不遮蔽其他成功结果，批次必须以非零状态退出。"""

        def extract(source: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
            """根据 source 文件名返回固定执行摘要；bad.md 抛错，其他参数仅兼容接口。"""
            if source.name == "bad.md":
                raise ValueError("bad input")
            directory = source.parent / "runs" / "test"
            result = {
                "run_id": "test",
                "status": "complete",
                "chunks": 1,
                "candidates": 1,
                "records": 1,
                "book_id": "book-test",
                "directory": str(directory),
                "config": {"large_field": "payload" * 10000},
                "execution_stats": {"details": ["long statistics"] * 1000},
                "errors": [{"stage": "review", "severity": "limitation"}],
            }
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            return result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "input.json"
            manifest.write_text(
                json.dumps(
                    {"books": [{"local_name": "ok.md"}, {"local_name": "bad.md"}]}
                ),
                encoding="utf-8",
            )
            client = MagicMock()
            client.stats = {"calls": 0}
            with (
                patch(
                    "sys.argv",
                    [
                        "book-extract",
                        "--manifest",
                        str(manifest),
                        "--output",
                        str(root / "runs"),
                        "--book-workers",
                        "2",
                        "--timeout",
                        "600",
                        "--max-connections",
                        "192",
                    ],
                ),
                patch("book_extractor.cli.load_service", return_value={}),
                patch(
                    "book_extractor.cli.LLMClient", return_value=client
                ) as client_type,
                patch("book_extractor.cli.extract_book", side_effect=extract),
            ):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 1)
                self.assertEqual(client_type.call_args.kwargs["timeout"], 600)
                self.assertEqual(client_type.call_args.kwargs["max_connections"], 192)
            results = json.loads(
                (root / "runs" / "batch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [book["status"] for book in results["books"]], ["complete", "failed"]
            )
            saved = results["books"][0]
            self.assertNotIn("config", saved)
            self.assertNotIn("execution_stats", saved)
            self.assertNotIn("errors", saved)
            self.assertEqual(saved["book_id"], "book-test")
            self.assertEqual(saved["error_count"], 1)
            expected = root / "runs" / "test" / "manifest.json"
            self.assertEqual(Path(saved["manifest"]), expected)
            self.assertIn("execution_stats", json.loads(expected.read_text()))
            client.close.assert_called_once()

    def test_finished_worker_starts_next_book(self) -> None:
        """首本阻塞时，另一个工作线程仍能开始第三本书。"""
        third_started = Event()

        def extract(source: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
            """根据 source 文件名设置或等待事件，返回执行摘要；等待超时说明调度阻塞。"""
            if source.name == "slow.md" and not third_started.wait(5):
                raise RuntimeError("book three was blocked behind book one")
            if source.name == "third.md":
                third_started.set()
            return {
                "run_id": source.stem,
                "status": "complete",
                "chunks": 1,
                "candidates": 1,
                "records": 1,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "input.json"
            manifest.write_text(
                json.dumps(
                    {
                        "books": [
                            {"local_name": name}
                            for name in ["slow.md", "fast.md", "third.md"]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            client = MagicMock()
            client.stats = {"calls": 0}
            with (
                patch(
                    "sys.argv",
                    [
                        "book-extract",
                        "--manifest",
                        str(manifest),
                        "--output",
                        str(root / "runs"),
                        "--book-workers",
                        "2",
                    ],
                ),
                patch("book_extractor.cli.load_service", return_value={}),
                patch("book_extractor.cli.LLMClient", return_value=client),
                patch("book_extractor.cli.extract_book", side_effect=extract),
            ):
                main()
            result = json.loads(
                (root / "runs" / "batch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [book["run_id"] for book in result["books"]], ["slow", "fast", "third"]
            )

    def test_idle_request_capacity_prefetches_before_book_finishes(self) -> None:
        """首书尚未结束但请求槽位有空闲时，允许提前加载一本，驻留不超过配置上限。"""
        second_started = Event()

        def extract(source: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
            """首书等第二本被预取后才结束，避免把正常完成补位误认成提前加载。"""
            if source.name == "first.md":
                if not second_started.wait(3):
                    raise AssertionError("空闲请求额度未触发新书预取")
            else:
                second_started.set()
            return {
                "run_id": source.stem,
                "status": "complete",
                "chunks": 1,
                "candidates": 1,
                "records": 1,
            }

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = create_parser().parse_args(
                [
                    "--input",
                    "unused.md",
                    "--output",
                    str(root),
                    "--book-workers",
                    "1",
                    "--book-prefetch",
                    "1",
                ]
            )
            client = MagicMock()
            client.request_queue_state = {"active": 1, "waiting": 0, "limit": 2}
            with patch("book_extractor.cli.extract_book", side_effect=extract):
                results = run_books(
                    [{"local_name": "first.md"}, {"local_name": "second.md"}],
                    root,
                    args,
                    client,
                )
            self.assertTrue(second_started.is_set())
            self.assertEqual([r["status"] for r in results], ["complete", "complete"])


if __name__ == "__main__":
    unittest.main()
