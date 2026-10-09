"""验证按书选择服务及原目录恢复时的前置校验、排序和资源关闭。"""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock, patch

from book_extractor.cli import create_parser, main, run_books
from book_extractor.llm import ExtractionCancelledError
from book_extractor.telemetry import TelemetryWriteError


def result(run_id: str, status: str = "complete") -> dict[str, Any]:
    """返回给定运行身份及状态的最小书籍摘要，供离线提取边界替身使用。"""
    return {
        "run_id": run_id,
        "status": status,
        "chunks": 1,
        "candidates": 2,
        "records": 2,
        "errors": [],
    }


class ServicesResumeTests(unittest.TestCase):
    """确保新服务只处理显式指定书目，旧目录通过验证后才恢复为成功。"""

    def setUp(self) -> None:
        """创建独立输出目录和两个不访问网络的服务替身。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.primary = MagicMock(model="primary-model")
        self.primary.identity = {
            "model": "primary-model",
            "base_url": "https://primary",
        }
        self.extra = MagicMock(model="extra-model")
        self.extra.identity = {"model": "extra-model", "base_url": "https://extra"}

    def previous(self, name: str, status: str) -> dict[str, Any]:
        """写入同名原运行manifest；文件内容只供CLI发现，实际验收仍调用pipeline。"""
        row = {
            **result(name, status),
            "book_id": name,
            "source": str(self.root / f"{name}.md"),
            "config": {"model": self.primary.model, "service": self.primary.identity},
        }
        directory = self.root / "runs" / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(row), encoding="utf-8")
        return row

    def arguments(self, *extra: str) -> Any:
        """返回使用本测试临时输出路径的CLI配置，可追加恢复或并发参数。"""
        return create_parser().parse_args(
            ["--input", "unused.md", "--output", str(self.root / "runs"), *extra]
        )

    def test_services_are_fixed_per_book_and_resume_keeps_order(self) -> None:
        """先验证complete，再处理新书，最后补partial；输出保留原序及原run_id。"""
        self.previous("partial", "partial")
        self.previous("done", "complete")
        books = [
            {"local_name": "partial.md", "book_id": "partial"},
            {"local_name": "new.md", "service": "zj"},
            {"local_name": "done.md", "book_id": "done"},
        ]
        calls = []
        snapshots = []

        def extract(source: Path, output: Path, client: Any, **kwargs: Any) -> Any:
            """记录路由及恢复参数，并模拟已有结果通过pipeline校验。"""
            calls.append((source.stem, client, kwargs))
            return result(source.stem)

        def save(path: Path, value: dict[str, Any]) -> None:
            """保存主线程当次摘要副本，用于检查补做期间仍保留先前书籍条目。"""
            snapshots.append(json.loads(json.dumps(value)))

        with (
            patch("book_extractor.cli.extract_book", side_effect=extract),
            patch("book_extractor.cli.write_json", side_effect=save),
        ):
            rows = run_books(
                books,
                self.root,
                self.arguments(
                    "--resume-existing", "--compatible-implementation", "old"
                ),
                self.primary,
                clients={"zj": self.extra},
            )
        self.assertEqual([call[0] for call in calls], ["done", "new", "partial"])
        self.assertIs(calls[0][1], self.primary)
        self.assertIs(calls[1][1], self.extra)
        self.assertEqual(calls[0][2]["resume_run_id"], "done")
        self.assertEqual(calls[0][2]["compatible_implementations"], ("old",))
        self.assertNotIn("resume_run_id", calls[1][2])
        self.assertEqual([row["run_id"] for row in rows], ["partial", "new", "done"])
        for snapshot in snapshots:
            self.assertTrue(
                {"partial", "done"} <= {r["run_id"] for r in snapshot["books"]}
            )

    def test_changed_service_and_duplicate_runs_fail_before_calls(self) -> None:
        """已运行书籍换服务或存在两个匹配目录时，整批在任何提取调用前拒绝。"""
        previous = self.previous("book", "running")
        args = self.arguments("--resume-existing")
        with patch("book_extractor.cli.extract_book") as extract:
            with self.assertRaisesRegex(ValueError, "cannot change"):
                run_books(
                    [{"local_name": "book.md", "book_id": "book", "service": "zj"}],
                    self.root,
                    args,
                    self.primary,
                    clients={"zj": self.extra},
                )
            duplicate = self.root / "runs" / "duplicate"
            duplicate.mkdir()
            (duplicate / "manifest.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Multiple existing"):
                run_books([{"local_name": "book.md"}], self.root, args, self.primary)
            extract.assert_not_called()

    def test_fatal_cancels_all_services(self) -> None:
        """任一书籍发生账本故障时，所有服务停止发送新请求。"""
        with patch(
            "book_extractor.cli.extract_book", side_effect=TelemetryWriteError("x")
        ):
            rows = run_books(
                [{"local_name": "a.md"}, {"local_name": "b.md", "service": "zj"}],
                self.root,
                self.arguments(),
                self.primary,
                clients={"zj": self.extra},
            )
        self.assertEqual([row["status"] for row in rows], ["failed", "not_started"])
        self.primary.cancel.assert_called()
        self.extra.cancel.assert_called()

    def test_additional_capacity_allows_prefetch(self) -> None:
        """主服务已满但额外服务有空闲时，汇总额度能提前加载显式额外服务书。"""
        extra_started = Event()
        self.primary.request_queue_state = {"active": 1, "waiting": 20, "limit": 1}
        self.extra.request_queue_state = {"active": 0, "waiting": 0, "limit": 1}

        def extract(source: Path, output: Path, client: Any, **kwargs: Any) -> Any:
            """主书等待额外服务书启动，验证预取并非由主书完成触发。"""
            if client is self.primary:
                if not extra_started.wait(3):
                    raise AssertionError("额外服务空闲额度未触发预取")
            else:
                extra_started.set()
            return result(source.stem)

        with patch("book_extractor.cli.extract_book", side_effect=extract):
            rows = run_books(
                [{"local_name": "a.md"}, {"local_name": "b.md", "service": "zj"}],
                self.root,
                self.arguments("--book-prefetch", "1"),
                self.primary,
                clients={"zj": self.extra},
            )
        self.assertTrue(extra_started.is_set())
        self.assertTrue(all(row["status"] == "complete" for row in rows))

    def test_slow_service_cannot_take_all_resident_slots(self) -> None:
        """慢服务首书阻塞时，快服务仍连续补书，不能被静态交错顺序挤出驻留池。"""
        release = Event()
        last_fast_started = Event()
        books = [{"local_name": f"fast{index}.md"} for index in range(5)] + [
            {"local_name": f"slow{index}.md", "service": "zj"} for index in range(5)
        ]

        def extract(source: Path, output: Path, client: Any, **kwargs: Any) -> Any:
            """只有慢服务等待测试放行；记录最后一本快服务书是否及时被调度。"""
            if client is self.extra:
                release.wait(5)
            elif source.stem == "fast4":
                last_fast_started.set()
            return result(source.stem)

        with (
            patch("book_extractor.cli.extract_book", side_effect=extract),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(
                run_books,
                books,
                self.root,
                self.arguments("--book-workers", "2"),
                self.primary,
                clients={"zj": self.extra},
            )
            try:
                self.assertTrue(last_fast_started.wait(2))
            finally:
                release.set()
                rows = future.result(timeout=5)
        self.assertEqual(
            [row["run_id"] for row in rows],
            [Path(book["local_name"]).stem for book in books],
        )

    def test_services_interleave_before_book_slots_are_filled(self) -> None:
        """大量主服务书排在前面时，第二个驻留槽仍启动额外服务书。"""
        extra_started = Event()

        def extract(source: Path, output: Path, client: Any, **kwargs: Any) -> Any:
            """主服务调用等待额外服务入场；完成前不存在自然补位机会。"""
            if client is self.extra:
                extra_started.set()
            elif not extra_started.wait(3):
                raise AssertionError("额外服务被主服务书堵在入场队列")
            return result(source.stem)

        books = [
            {"local_name": "a.md"},
            {"local_name": "b.md"},
            {"local_name": "c.md", "service": "zj"},
        ]
        with patch("book_extractor.cli.extract_book", side_effect=extract):
            rows = run_books(
                books,
                self.root,
                self.arguments("--book-workers", "2"),
                self.primary,
                clients={"zj": self.extra},
            )
        self.assertEqual([row["run_id"] for row in rows], ["a", "b", "c"])
        self.assertTrue(all(row["status"] == "complete" for row in rows))

    def test_interrupt_cancels_both_clients_before_join(self) -> None:
        """SIGINT先取消两个服务，再等在途书返回，不在线程池退出时死等。"""
        started, cancelled = Event(), Event()
        self.primary.cancel.side_effect = cancelled.set
        self.extra.cancel.side_effect = cancelled.set

        def extract(*args: Any, **kwargs: Any) -> Any:
            """在途额外服务书只响应取消；超时表示取消传播顺序错误。"""
            started.set()
            if not cancelled.wait(3):
                raise AssertionError("取消没有在线程池等待前传播")
            raise ExtractionCancelledError("cancelled")

        def interrupt(*args: Any, **kwargs: Any) -> Any:
            """等待在途调用进入后模拟用户中断。"""
            if not started.wait(3):
                raise AssertionError("工作线程没有启动")
            raise KeyboardInterrupt

        with (
            patch("book_extractor.cli.extract_book", side_effect=extract),
            patch("book_extractor.cli.wait", side_effect=interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_books(
                [{"local_name": "book.md", "service": "zj"}],
                self.root,
                self.arguments(),
                self.primary,
                clients={"zj": self.extra},
            )
        self.primary.cancel.assert_called()
        self.extra.cancel.assert_called()

    def test_main_keeps_extra_model_configuration_and_closes_all(self) -> None:
        """额外服务保留自己的模型、温度、分词器与并发，不继承主model覆盖。"""
        manifest = self.root / "input.json"
        manifest.write_text(
            json.dumps({"books": [{"local_name": "new.md", "service": "zj"}]}),
            encoding="utf-8",
        )
        configs = [
            {"model": "override", "temperature": 0.7, "tokenizer_path": "primary"},
            {"model": "extra-model", "temperature": 0.4, "tokenizer_path": "extra"},
        ]
        with (
            patch(
                "sys.argv",
                [
                    "extract",
                    "--manifest",
                    str(manifest),
                    "--service",
                    "nanhu",
                    "--model",
                    "override",
                    "--additional-service",
                    "zj",
                    "--transport-failure-limit",
                    "2",
                ],
            ),
            patch("book_extractor.cli.load_service", side_effect=configs) as load,
            patch(
                "book_extractor.cli.LLMClient", side_effect=[self.primary, self.extra]
            ) as make,
            patch("book_extractor.cli.run_books", return_value=[result("new")]) as run,
        ):
            main()
        self.assertEqual(load.call_args_list[0].args[1:], ("nanhu", "override"))
        self.assertEqual(load.call_args_list[1].args[1:], ("zj",))
        extra = make.call_args_list[1].kwargs
        self.assertEqual(extra["model"], "extra-model")
        self.assertEqual(extra["temperature"], 0.4)
        self.assertEqual(extra["tokenizer_path"], "extra")
        self.assertEqual(extra["max_connections"], 16)
        self.assertEqual(extra["transport_failure_limit"], 2)
        self.assertEqual(
            run.call_args.kwargs["clients"], {"nanhu": self.primary, "zj": self.extra}
        )
        self.primary.close.assert_called_once()
        self.extra.close.assert_called_once()

    def test_unknown_service_rejected_before_client_creation(self) -> None:
        """未知书目服务在初始化任何付费客户端前报参数错误。"""
        manifest = self.root / "input.json"
        manifest.write_text(
            json.dumps({"books": [{"local_name": "book.md", "service": "missing"}]}),
            encoding="utf-8",
        )
        with (
            patch(
                "sys.argv",
                ["extract", "--manifest", str(manifest), "--service", "nanhu"],
            ),
            patch("book_extractor.cli.LLMClient") as client,
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        self.assertEqual(raised.exception.code, 2)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
