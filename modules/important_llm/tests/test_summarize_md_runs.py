"""验证历史统计归账、日志去重及不完整输出的计数边界。"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.summarize_md_runs import summarize_runs


class RunStatisticsTests(unittest.TestCase):
    """仅使用临时事件文件，不访问模型或修改真实运行。"""

    def test_history_latest_duplicates_missing_usage_and_wall_time(self) -> None:
        """每个原始样本只归账一次，失败运行的旧结果不得计入有效导出。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for run_id, status, records, chars in [
                ("a", "complete", 2, 100),
                ("b", "failed", 5, 200),
                ("c", "complete", 4, 300),
            ]:
                directory = root / run_id
                directory.mkdir()
                (directory / "manifest.json").write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "book_id": run_id,
                            "status": status,
                            "records": records,
                            "source_statistics": {"characters": chars},
                        }
                    ),
                    encoding="utf-8",
                )
                (directory / "records.jsonl").write_text(
                    "{}\n" * records, encoding="utf-8"
                )

            def execution(
                run_id: str,
                execution_id: str,
                offset: int,
                duration: int,
                latencies: list[int],
                status: str,
                missing_usage: bool = False,
            ) -> Path:
                """按运行 ID、执行 ID、时间偏移、时长和延迟生成日志，

                返回文件路径；missing_usage 控制用量缺失。
                """
                rows = []

                def event(kind: str, second: float, **metadata: Any) -> None:
                    """按 kind、相对秒数 second 和 metadata 追加带唯一 ID 的事件，

                    不返回值。
                    """
                    timestamp = datetime(2026, 9, 22, tzinfo=timezone.utc) + timedelta(
                        seconds=offset + second
                    )
                    rows.append(
                        {
                            "event_id": f"{execution_id}:{len(rows)}",
                            "event": kind,
                            "execution_id": execution_id,
                            "run_id": run_id,
                            "book_id": run_id,
                            "timestamp_utc": timestamp.isoformat(),
                            **metadata,
                        }
                    )

                event("book_started", 0, source_characters=100)
                event("stage_started", 0, stage="discovery", work_id="all")
                for index, latency in enumerate(latencies):
                    request_id = f"{execution_id}:request:{index}"
                    event(
                        "request_started",
                        0.01,
                        request_id=request_id,
                        attempt=1,
                        stage="discovery",
                    )
                    usage = {
                        key: None if missing_usage else value
                        for key, value in {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "cached_tokens": 0,
                            "reasoning_tokens": 0,
                        }.items()
                    }
                    event(
                        "request_finished",
                        latency,
                        request_id=request_id,
                        elapsed_seconds=latency,
                        http_success=True,
                        usage=usage,
                        stage="discovery",
                    )
                event("cache_hit", duration - 0.1, stage="discovery")
                event(
                    "stage_finished",
                    duration,
                    stage="discovery",
                    work_id="all",
                    elapsed_seconds=duration,
                )
                event(
                    "book_finished",
                    duration,
                    status=status,
                    elapsed_seconds=duration,
                    records=2,
                    candidates=2,
                    chunks=1,
                )
                path = root / run_id / "executions" / execution_id / "events.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )
                return path

            first = execution("a", "old", 0, 20, [10], "partial", missing_usage=True)
            latest = execution("a", "new", 30, 10, [1] * 9, "complete")
            execution("b", "failed", 35, 10, [], "failed")
            copy = latest.parent.parent / "copied-log"
            copy.mkdir()
            shutil.copyfile(latest, copy / "events.jsonl")
            with first.open("ab") as handle:
                handle.write(b'{"interrupted":')
            before = {
                str(path): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            result = summarize_runs(root)
            self.assertEqual(result["execution_count"], 3)
            self.assertEqual(result["history"]["totals"]["requests_started"], 10)
            self.assertEqual(result["history"]["http_latency_seconds"]["p95"], 10)
            self.assertEqual(result["history"]["http_latency_seconds"]["p50"], 1)
            self.assertEqual(result["history"]["usage"]["prompt_tokens"], 90)
            self.assertEqual(result["history"]["usage_missing"]["prompt_tokens"], 1)
            self.assertFalse(result["history"]["usage_is_complete"])
            self.assertTrue(result["history"]["incomplete_final_line"])
            self.assertEqual(
                result["history"]["timing"]["execution_wall_seconds_known_sum"], 40
            )
            self.assertEqual(
                result["history"]["timing"]["observed_batch_span_seconds"], 45
            )
            self.assertEqual(
                result["history"]["stages"][0]["usage"]["prompt_tokens"], 90
            )
            runs = {run["run_id"]: run for run in result["runs"]}
            self.assertEqual(
                runs["a"]["latest_execution"]["totals"]["requests_started"], 9
            )
            self.assertEqual(runs["a"]["history"]["totals"]["requests_started"], 10)
            self.assertNotIn("book", result["history"])
            self.assertEqual(runs["b"]["complete_records"], 0)
            self.assertEqual(runs["c"]["telemetry"], "missing")
            self.assertIsNone(runs["c"]["history"]["usage"]["prompt_tokens"])
            self.assertEqual(result["complete_records_known_sum"], 6)
            self.assertEqual(result["source_characters_known_sum"], 600)
            self.assertIsNone(result["pricing"])
            self.assertEqual(
                before,
                {
                    str(path): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )


if __name__ == "__main__":
    unittest.main()
