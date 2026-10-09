"""离线验证检查点原子替换及文件占用时的有界重试。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Timer
from unittest.mock import patch

from book_extractor import storage


class StorageTests(unittest.TestCase):
    """覆盖 Windows 真实共享锁和跨平台错误重试边界。"""

    def setUp(self) -> None:
        """在同一卷的临时目录准备完整旧文件及待替换文件。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "checkpoint.json"
        self.pending = Path(self.temporary.name) / "checkpoint.json.tmp"
        self.destination.write_text("old", encoding="utf-8")
        self.pending.write_text("new", encoding="utf-8")

    @unittest.skipUnless(
        sys.platform == "win32", "Real FILE_SHARE_DELETE behavior requires Windows"
    )
    def test_reader_release_recovers(self) -> None:
        """真实读取句柄短暂占用后释放，有限重试能成功完成替换。"""
        reader = self.destination.open("r", encoding="utf-8")
        timer = Timer(0.12, reader.close)
        timer.start()
        try:
            storage.replace_checkpoint(self.pending, self.destination)
        finally:
            reader.close()
            timer.join()
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")
        self.assertFalse(self.pending.exists())

    @unittest.skipUnless(
        sys.platform == "win32", "Real FILE_SHARE_DELETE behavior requires Windows"
    )
    def test_persistent_reader_stops_and_preserves_both_files(self) -> None:
        """读取句柄持续占用时，在预算内失败并保留新旧两份文件。"""
        start = time.monotonic()
        with self.destination.open("r", encoding="utf-8"):
            with self.assertRaises(PermissionError):
                storage.replace_checkpoint(self.pending, self.destination)
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.pending.read_text(encoding="utf-8"), "new")

    def test_permanent_access_error_has_six_attempts(self) -> None:
        """持续访问拒绝只能尝试六次，不能无限循环。"""
        error = PermissionError("access denied")
        error.winerror = 5
        with (
            patch.object(storage, "platform", "win32"),
            patch.object(storage.os, "replace", side_effect=error) as replace,
            patch.object(storage.time, "sleep") as sleep,
        ):
            with self.assertRaises(PermissionError):
                storage.replace_checkpoint(self.pending, self.destination)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list], [0.05, 0.1, 0.2, 0.4, 0.8]
        )
        self.assertEqual(self.pending.read_text(encoding="utf-8"), "new")

    def test_other_failures_are_not_retried(self) -> None:
        """非 Windows 权限错误和其他 I/O 异常立即传播，不错误重试。"""
        for platform, error in [
            ("linux", PermissionError("denied")),
            ("win32", FileNotFoundError("missing")),
        ]:
            with (
                self.subTest(platform=platform),
                patch.object(storage, "platform", platform),
                patch.object(storage.os, "replace", side_effect=error) as replace,
                patch.object(storage.time, "sleep") as sleep,
            ):
                with self.assertRaises(type(error)):
                    storage.replace_checkpoint(self.pending, self.destination)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
