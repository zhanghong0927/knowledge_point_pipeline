"""验证显式原目录恢复的身份边界、完整结果复用和历史执行审计。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from synthesis_fixtures import accepted_work
from test_pipeline import FakeClient

from book_extractor.pipeline import (
    IMPLEMENTATION_MODULES,
    digest,
    extract_book,
    validate_resume,
    write_json,
)


class ResumeInPlaceTests(unittest.TestCase):
    """用离线发现及综合结果覆盖原地恢复，不访问真实推理服务。"""

    def setUp(self) -> None:
        """建立一份完整结果，供各测试独立修改身份或恢复状态。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "book.md"
        self.source.write_text("惯性描述物体运动状态。" * 12, encoding="utf-8")
        self.output = self.root / "runs"
        stub = patch("book_extractor.pipeline.synthesize", side_effect=accepted_work)
        stub.start()
        self.addCleanup(stub.stop)
        self.first = extract_book(
            self.source, self.output, FakeClient(["name:惯性"]), workers=1
        )
        self.directory = Path(self.first["directory"])
        self.run_id = self.first["run_id"]

    def resume(self, **options: Any) -> dict[str, Any]:
        """按 options 显式恢复测试运行，返回原目录的运行摘要。"""
        return extract_book(
            self.source,
            self.output,
            FakeClient([]),
            workers=1,
            resume_run_id=self.run_id,
            **options,
        )

    def test_complete_is_read_only_and_invalid_records_fail(self) -> None:
        """完整结果离线验证后不重跑；伪造记录的书籍身份必须失败。"""
        before = {
            path.relative_to(self.directory): path.read_bytes()
            for path in self.directory.rglob("*")
            if path.is_file()
        }
        with patch("book_extractor.pipeline._run") as run:
            result = self.resume()
        run.assert_not_called()
        self.assertEqual(result, self.first)
        self.assertEqual(
            before,
            {
                path.relative_to(self.directory): path.read_bytes()
                for path in self.directory.rglob("*")
                if path.is_file()
            },
        )
        record_path = self.directory / "records.jsonl"
        rows = [json.loads(line) for line in record_path.read_text().splitlines()]
        rows[0]["book_id"] = "different-book"
        record_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "record identity"):
            self.resume()
        self.assertFalse((self.directory / ".running").exists())

    def test_partial_preserves_history_and_audits_actual_implementation(self) -> None:
        """调度代码变更只有明确白名单才可复用，旧日志和缓存身份保持不变。"""
        manifest = copy.deepcopy(self.first)
        manifest["status"] = "partial"
        write_json(self.directory / "manifest.json", manifest)
        previous_config = copy.deepcopy(manifest["config"])
        history = {
            path: path.read_bytes()
            for path in (self.directory / "executions").rglob("*")
            if path.is_file()
        }
        package = Path(__file__).parents[1] / "src" / "book_extractor"
        implementation_bytes = b"".join(
            (package / (name + ".py")).read_bytes() for name in IMPLEMENTATION_MODULES
        )

        def new_digest(value: bytes) -> str:
            """仅模拟整包源码的指纹变化，其余来源和检查点哈希保持真实。"""
            return "f" * 64 if value == implementation_bytes else digest(value)

        with patch("book_extractor.pipeline.digest", side_effect=new_digest):
            with self.assertRaisesRegex(ValueError, "not explicitly compatible"):
                self.resume()
            result = self.resume(
                compatible_implementations=(previous_config["implementation"],)
            )
            fresh = extract_book(self.source, self.output, FakeClient([]), workers=1)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["config"], previous_config)
        self.assertEqual(result["current_execution"]["implementation"], "f" * 64)
        self.assertNotEqual(fresh["run_id"], self.run_id)
        self.assertEqual(len(list((self.directory / "executions").iterdir())), 2)
        for path, content in history.items():
            self.assertEqual(path.read_bytes(), content)

    def test_semantics_source_and_path_cannot_be_bypassed(self) -> None:
        """白名单仅放行源码指纹，服务/模型/分词器等语义差异和越界均拒绝。"""
        original = self.first["config"]
        mutations = {
            "prompt": "changed",
            "model": "another",
            "chunk_tokens": 8000,
            "dependencies": {},
            "python": "different",
            "service": {"service": "different", "tokenizer_sha256": "different"},
            "max_output_tokens": 99,
            "extra_body": {"enable_thinking": True},
            "title": "different",
            "subject": "different",
            "book_id": "different",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                current = {**original, field: value}
                with self.assertRaisesRegex(ValueError, "semantic configuration"):
                    validate_resume(
                        self.output,
                        self.run_id,
                        self.source.read_bytes(),
                        current,
                        (original["implementation"],),
                    )
        with self.assertRaisesRegex(ValueError, "source or run identity"):
            validate_resume(self.output, self.run_id, b"changed", original)
        for invalid in ("../outside", "/absolute", "a" * 19):
            with self.subTest(run_id=invalid):
                with self.assertRaisesRegex(ValueError, "Invalid resume"):
                    validate_resume(self.output, invalid, b"", original)

    def test_live_lock_and_inconsistent_complete_are_rejected(self) -> None:
        """即使是完整结果也不能绕过活动锁或未解决错误。"""
        lock = self.directory / ".running"
        lock.write_text("active", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.resume()
        self.assertEqual(lock.read_text(), "active")
        lock.unlink()
        manifest = copy.deepcopy(self.first)
        manifest["errors"] = [{"severity": "error", "issue": "unfinished"}]
        write_json(self.directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "unresolved errors"):
            self.resume()


if __name__ == "__main__":
    unittest.main()
