"""使用离线桩检查客户端与统计行为；测试不调用真实模型服务。"""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from book_extractor.telemetry import EventLog, TelemetryWriteError, summarize_events


class TelemetryTests(unittest.TestCase):
    """检查并发写入、残缺日志与统计边界，不接触真实执行数据。"""

    def test_member_failures_remain_visible_without_batch_parse_failure(self) -> None:
        """逐项保留成功响应后仍统计错误观察数，不将它当成HTTP或唯一候选数。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            log.event(
                "synthesis_members_validated",
                stage="synthesis",
                accepted=11,
                remaining=1,
                error_counts={"source_alias_not_found": 1},
            )
            log.event(
                "synthesis_members_validated",
                stage="synthesis",
                accepted=1,
                remaining=0,
                error_counts={},
            )
            log.close()
            stage = summarize_events(path)["stages"][0]
            observed = stage["member_validation_observations"]
            self.assertEqual((observed["accepted"], observed["remaining"]), (12, 1))
            self.assertEqual(observed["error_counts"], {"source_alias_not_found": 1})
            self.assertEqual(stage["requests"], 0)

    def test_verification_observations_are_not_request_counts(self) -> None:
        """验证校验选择和结果按观察计数，与缓存命中和 HTTP 请求次数分开。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            for record_id in ["a", "b", "unfinished"]:
                log.event(
                    "verification_selected", stage="verification", record_id=record_id
                )
            log.event("cache_hit", stage="verification")
            log.event(
                "verification_result",
                stage="verification",
                record_id="a",
                status="complete",
                changed=True,
            )
            log.event(
                "verification_result",
                stage="verification",
                record_id="b",
                status="partial",
            )
            log.close()
            stage = summarize_events(path)["stages"][0]
            self.assertEqual(
                stage["verification_observations"],
                {
                    "includes_cache_replays": True,
                    "selected": 3,
                    "complete": 1,
                    "partial": 1,
                    "changed": 1,
                },
            )
            self.assertEqual(stage["requests"], 0)
            self.assertEqual(stage["cache_hits"], 1)

    def test_unmeasured_stage_is_null_but_measured_zero_is_zero(self) -> None:
        """验证缺失阶段计时返回 None，明确测得零秒仍返回零。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path)
            log.event("cache_hit", stage="grouping")
            log.event("stage_finished", stage="review", elapsed_seconds=0.0)
            log.event("stage_finished", stage="missing_duration")
            log.close()
            stages = {
                stage["stage"]: stage for stage in summarize_events(path)["stages"]
            }
            self.assertIsNone(stages["grouping"]["work_seconds"])
            self.assertEqual(stages["review"]["work_seconds"], 0.0)
            self.assertIsNone(stages["missing_duration"]["work_seconds"])

    def test_concurrent_append_and_partial_tail(self) -> None:
        """验证多线程事件保持完整，崩溃末行显式标记且原日志不被修复。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path, execution_id="execution")

            def write(index: int) -> None:
                """通过共享日志写入独立缓存事件，供并发完整性检查。"""
                log.event("cache_hit", stage="discovery", work_id=str(index))

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(20)))
            log.event("request_started", request_id="unfinished", attempt=1)
            log.close()
            with path.open("ab") as handle:
                handle.write(b'{"truncated":')
            summary = summarize_events(path)
            self.assertEqual(summary["totals"]["cache_hits"], 20)
            self.assertEqual(summary["totals"]["requests_unresolved"], 1)
            self.assertTrue(summary["incomplete_final_line"])
            self.assertFalse(summary["usage_is_complete"])
            self.assertIsNone(summary["usage"]["prompt_tokens"])
            with self.assertRaises(TelemetryWriteError):
                EventLog(path)
            with path.open("ab") as handle:
                handle.write(b"\n")
            with self.assertRaises(ValueError):
                summarize_events(path)

    def test_stage_and_book_metrics(self) -> None:
        """根据阶段和整书事件验证墙钟、缓存、输出数量及端到端速率。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            log = EventLog(path, execution_id="execution")
            log.event(
                "book_started", source_characters=400, source_bytes=1200, model="stub"
            )
            log.event("stage_started", stage="discovery")
            log.event("cache_hit", stage="discovery", work_id="c1")
            log.event("stage_finished", stage="discovery", elapsed_seconds=3.5)
            log.event(
                "book_finished",
                status="complete",
                elapsed_seconds=4.0,
                records=10,
                candidates=12,
                chunks=3,
            )
            log.close()
            summary = summarize_events(path)
            self.assertEqual(summary["stages"][0]["work_seconds"], 3.5)
            self.assertIsNotNone(summary["stages"][0]["active_wall_seconds"])
            self.assertEqual(summary["usage"]["prompt_tokens"], 0)
            self.assertTrue(summary["usage_is_complete"])
            self.assertEqual(summary["book"]["records"], 10)
            self.assertEqual(summary["book"]["pipeline_characters_per_second"], 100)
            self.assertEqual(summary["book"]["pipeline_records_per_second"], 2.5)
            self.assertEqual(
                len(
                    {
                        json.loads(line)["event_id"]
                        for line in path.read_text().splitlines()
                    }
                ),
                5,
            )

    def test_overlapping_work_uses_interval_union(self) -> None:
        """验证重叠任务按区间并集计墙钟，累计任务时间不冒充阶段墙钟。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            rows = []
            for index, (kind, work, second, elapsed) in enumerate(
                [
                    ("stage_started", "a", 0, None),
                    ("stage_started", "b", 2, None),
                    ("stage_finished", "a", 5, 5),
                    ("stage_finished", "b", 7, 5),
                ]
            ):
                rows.append(
                    {
                        "event_id": str(index),
                        "event": kind,
                        "stage": "discovery",
                        "work_id": work,
                        "timestamp_utc": f"2026-09-22T00:00:0{second}+00:00",
                        "elapsed_seconds": elapsed,
                    }
                )
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            stage = summarize_events(path)["stages"][0]
            self.assertEqual(stage["work_seconds"], 10)
            self.assertEqual(stage["active_wall_seconds"], 7)
            self.assertEqual(stage["elapsed_span_seconds"], 7)
            with path.open("ab") as handle:
                handle.write(b"{")
            self.assertFalse(summarize_events(path)["usage_is_complete"])


if __name__ == "__main__":
    unittest.main()
