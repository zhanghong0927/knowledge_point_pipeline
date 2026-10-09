"""持久化每次执行的安全 JSONL 事件并派生统计；

不保存正文、响应或凭据，不猜测未知用量和价格。
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


class TelemetryWriteError(RuntimeError):
    """日志创建或写入失败时的异常；调用方必须停止本次执行，不能重发已完成的模型请求。"""


class EventLog:
    """由单进程、多线程共享的执行日志；每次执行新建文件，崩溃旧日志保持只读以供审计。"""

    def __init__(
        self,
        path: Path,
        *,
        execution_id: str | None = None,
        run_id: str | None = None,
        book_id: str | None = None,
    ) -> None:
        """按路径独占创建日志并绑定公共身份；

        拒绝已有文件，文件系统错误转为 TelemetryWriteError。
        """
        self.path = Path(path)
        self._base = {
            "execution_id": execution_id,
            "run_id": run_id,
            "book_id": book_id,
        }
        self._lock = Lock()
        self._failed = False
        self._closed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600
            )
        except OSError as error:
            raise TelemetryWriteError(
                "Cannot create a fresh execution event log"
            ) from error

    def event(self, kind: str, **metadata: Any) -> None:
        """校验事件元数据后追加并 fsync；

        失败会锁死日志，阻止随后请求在无账状态下继续。
        """
        event = {
            **self._base,
            "call_id": None,
            "request_id": None,
            **metadata,
            "event": kind,
            "event_id": uuid4().hex,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        # All production callers supply counts and IDs. Reject accidental future
        # additions of request content, response content, or credential containers.
        prohibited = {
            "api_key",
            "authorization",
            "headers",
            "messages",
            "prompt",
            "response",
            "request_body",
            "response_body",
            "extra_body",
            "context",
            "text",
        }

        def check(value: Any) -> None:
            """递归检查元数据键名；发现正文或凭据容器时抛 ValueError，禁止写入日志。"""
            if isinstance(value, dict):
                if prohibited.intersection(str(key).lower() for key in value):
                    raise ValueError("Sensitive content cannot be stored in telemetry")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        try:
            check(event)
            data = (
                json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as error:
            with self._lock:
                self._failed = True
            raise TelemetryWriteError(
                "Invalid or sensitive accounting data; execution logging stopped"
            ) from error
        with self._lock:
            if self._failed or self._closed:
                raise TelemetryWriteError(
                    "Execution event log is unavailable; no further calls are allowed"
                )
            try:
                remaining = memoryview(data)
                while remaining:
                    count = os.write(self._fd, remaining)
                    if count <= 0:
                        raise OSError("Event log write made no progress")
                    remaining = remaining[count:]
                os.fsync(self._fd)
            except OSError as error:
                self._failed = True
                raise TelemetryWriteError(
                    "Execution event write failed; stop before retrying model work"
                ) from error

    def close(self) -> None:
        """关闭本日志拥有的描述符；

        应先结束所有写入线程，关闭失败抛 TelemetryWriteError。
        """
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    os.close(self._fd)
                except OSError as error:
                    raise TelemetryWriteError(
                        "Closing the execution log failed"
                    ) from error


def _read_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """读取路径中的完整事件行，返回事件与残缺末行标记；

    不修复原文件，完整行损坏时抛异常。
    """
    data = path.read_bytes()
    incomplete = bool(data and not data.endswith(b"\n"))
    lines = data.splitlines(keepends=True)
    if incomplete:
        lines = lines[:-1]
    events = []
    for line_number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise ValueError(
                f"Invalid complete telemetry line {line_number}"
            ) from error
        if not isinstance(event, dict):
            raise ValueError(f"Telemetry line {line_number} must be an object")
        events.append(event)
    return events, incomplete


def _latencies(values: list[float]) -> dict[str, float | int | None]:
    """根据原始秒数样本返回数量、总和与最近秩分位数；无样本时分位数为 None。"""
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "p50": ordered[math.ceil(len(ordered) * 0.50) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
    }


def _usage_totals(
    requests: list[dict[str, Any]], unresolved: int
) -> tuple[dict[str, int | None], dict[str, int]]:
    """根据已结束请求及未决数返回已知 token 总和和缺失数；

    无请求为零，未报告的请求为未知。
    """
    usage, missing = {}, {}
    for field in USAGE_FIELDS:
        values = [event.get("usage", {}).get(field) for event in requests]
        known = [value for value in values if value is not None]
        usage[field] = None
        if known or (not requests and not unresolved):
            usage[field] = sum(known)
        missing[field] = len(values) - len(known) + unresolved
    return usage, missing


def _stage_intervals(events: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """根据阶段事件计算已结束工作量及时间区间并集；

    区分累计工作与墙钟，时钟倒退时抛异常。
    """
    pending: dict[tuple[Any, ...], list[float]] = {}
    intervals: list[tuple[float, float]] = []
    for event in events:
        if event["event"] not in {"stage_started", "stage_finished"}:
            continue
        key = tuple(
            event.get(field)
            for field in ["execution_id", "run_id", "book_id", "work_id"]
        )
        timestamp = datetime.fromisoformat(event["timestamp_utc"]).timestamp()
        if event["event"] == "stage_started":
            pending.setdefault(key, []).append(timestamp)
        elif pending.get(key):
            start = pending[key].pop(0)
            if timestamp < start:
                raise ValueError(
                    "Stage UTC clock moved backwards; cannot derive wall time"
                )
            intervals.append((start, timestamp))
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    finished = [event for event in events if event["event"] == "stage_finished"]
    durations = [event.get("elapsed_seconds") for event in finished]
    work_seconds = None
    if durations and all(value is not None for value in durations):
        work_seconds = sum(durations)
    elapsed_span = None
    active_wall = None
    if merged:
        elapsed_span = merged[-1][1] - merged[0][0]
        active_wall = sum(end - start for start, end in merged)
    return {
        "work_seconds": work_seconds,
        "elapsed_span_seconds": elapsed_span,
        "active_wall_seconds": active_wall,
        "unfinished_work": sum(len(starts) for starts in pending.values()),
    }


def _summarize_stage(
    stage: str,
    events: list[dict[str, Any]],
    finished_ids: set[str],
    incomplete: bool,
) -> dict[str, Any]:
    """根据单阶段事件返回请求、用量和工作区间；复核数量是含缓存重放的观察值。"""
    counts = Counter(event["event"] for event in events)
    requests = [event for event in events if event["event"] == "request_finished"]
    starts = [event for event in events if event["event"] == "request_started"]
    unresolved = sum(event["request_id"] not in finished_ids for event in starts)
    usage, missing = _usage_totals(requests, unresolved)
    reviews = [event for event in events if event["event"] == "review_decision"]
    verification = [
        event for event in events if event["event"] == "verification_result"
    ]
    member_rounds = [
        event for event in events if event["event"] == "synthesis_members_validated"
    ]
    member_errors: Counter[str] = Counter()
    for event in member_rounds:
        member_errors.update(event.get("error_counts", {}))
    review_counts = {"events": len(reviews), "includes_cache_replays": True}
    for key in (
        "draft_count",
        "finding_count",
        "rejected_count",
        "added_count",
        "needs_context",
    ):
        review_counts[key] = sum(event.get(key, 0) for event in reviews)
    usage_complete = (
        not incomplete
        and not unresolved
        and not missing["prompt_tokens"]
        and not missing["completion_tokens"]
    )
    return {
        "stage": stage,
        "calls": counts["call_finished"],
        "requests": len(starts),
        "requests_failed": sum(
            event.get("http_success") is False for event in requests
        ),
        "requests_unresolved": unresolved,
        "retries": sum(event.get("attempt", 1) > 1 for event in starts),
        "cache_hits": counts["cache_hit"],
        "cache_misses": counts["cache_miss"],
        "usage": usage,
        "usage_missing": missing,
        "usage_is_complete": usage_complete,
        "request_queue_wait_seconds": _latencies(
            [
                event["queue_wait_seconds"]
                for event in starts
                if "queue_wait_seconds" in event
            ]
        ),
        "request_concurrency_peak": max(
            (event.get("global_inflight", 0) for event in starts), default=0
        ),
        "http_latency_seconds": _latencies(
            [event["elapsed_seconds"] for event in requests]
        ),
        "review_observations": review_counts,
        # 逐项失败不再触发整批parse_failed；另计观察量，避免误报精度提升。
        # 恢复读取缓存仍会产生观察事件，因此不能视为唯一成员数或HTTP次数。
        "member_validation_observations": {
            "includes_cache_replays": True,
            "rounds": len(member_rounds),
            "accepted": sum(event.get("accepted", 0) for event in member_rounds),
            "remaining": sum(event.get("remaining", 0) for event in member_rounds),
            "error_counts": dict(member_errors),
        },
        # 这里按执行观察计数，可能含缓存重放；输出变化不代表质量提升。
        "verification_observations": {
            "includes_cache_replays": True,
            "selected": counts["verification_selected"],
            "complete": sum(
                event.get("status") == "complete" for event in verification
            ),
            "partial": sum(event.get("status") == "partial" for event in verification),
            "changed": sum(event.get("changed") is True for event in verification),
        },
        **_stage_intervals(events),
    }


def _book_statistics(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """合并最近一次整书起止事件并计算端到端速率；

    无结束事件返回 None，不冒充 GPU 吞吐。
    """
    starts = [event for event in events if event["event"] == "book_started"]
    ends = [event for event in events if event["event"] == "book_finished"]
    if not ends:
        return None
    book = {}
    for key in ("status", "elapsed_seconds", "records", "candidates", "chunks"):
        book[key] = ends[-1].get(key)
    first = starts[-1] if starts else {}
    for key in (
        "source_characters",
        "source_bytes",
        "model",
        "workers",
        "chunk_tokens",
    ):
        book[key] = first.get(key)
    elapsed = book["elapsed_seconds"]
    for field, metric in (
        ("source_characters", "pipeline_characters_per_second"),
        ("records", "pipeline_records_per_second"),
    ):
        book[metric] = None
        if elapsed and elapsed > 0 and book[field] is not None:
            book[metric] = book[field] / elapsed
    return book


def summarize_events(
    path: Path, *, execution_id: str | None = None, run_id: str | None = None
) -> dict[str, Any]:
    """读取并筛选事件，返回用量、延迟、阶段和整书统计；重复事件抛异常，缺失量不补零。"""
    recorded, incomplete = _read_events(Path(path))
    events = []
    seen = set()
    for event in recorded:
        if execution_id is not None and event.get("execution_id") != execution_id:
            continue
        if run_id is not None and event.get("run_id") != run_id:
            continue
        if event["event_id"] in seen:
            raise ValueError("Duplicate telemetry event_id would double-count usage")
        seen.add(event["event_id"])
        events.append(event)
    counts = Counter(event["event"] for event in events)
    starts = [event for event in events if event["event"] == "request_started"]
    requests = [event for event in events if event["event"] == "request_finished"]
    calls = [event for event in events if event["event"] == "call_finished"]
    finished_ids = {event["request_id"] for event in requests}
    unresolved = sum(event["request_id"] not in finished_ids for event in starts)
    usage, missing = _usage_totals(requests, unresolved)
    stages = []
    names = sorted({event["stage"] for event in events if event.get("stage")})
    for stage in names:
        stage_events = [event for event in events if event.get("stage") == stage]
        stages.append(_summarize_stage(stage, stage_events, finished_ids, incomplete))
    usage_complete = (
        not incomplete
        and not unresolved
        and not missing["prompt_tokens"]
        and not missing["completion_tokens"]
    )
    totals = {
        "requests_queued": counts["request_queued"],
        "requests_started": len(starts),
        "requests_finished": len(requests),
        "http_success": sum(event.get("http_success") is True for event in requests),
        "http_failed": sum(event.get("http_success") is False for event in requests),
        "requests_unresolved": unresolved,
        "retries": sum(event.get("attempt", 1) > 1 for event in starts),
        "calls": len(calls),
        "parse_success": sum(event.get("parse_success") is True for event in calls),
        "parse_failed": sum(event.get("parse_success") is False for event in calls),
        "budget_rejections": counts["budget_rejected"],
        "output_budget_reductions": counts["output_budget_reduced"],
        "reask_regenerations": counts["reask_regenerated"],
        "cache_hits": counts["cache_hit"],
        "cache_misses": counts["cache_miss"],
    }
    return {
        "events": len(events),
        "incomplete_final_line": incomplete,
        "totals": totals,
        "usage": usage,
        "usage_missing": missing,
        "usage_is_complete": usage_complete,
        "request_queue_wait_seconds": _latencies(
            [
                event["queue_wait_seconds"]
                for event in starts
                if "queue_wait_seconds" in event
            ]
        ),
        "request_concurrency_peak": max(
            (event.get("global_inflight", 0) for event in starts), default=0
        ),
        "http_latency_seconds": _latencies(
            [event["elapsed_seconds"] for event in requests]
        ),
        "call_latency_seconds": _latencies(
            [event["elapsed_seconds"] for event in calls]
        ),
        "stages": stages,
        "book": _book_statistics(events),
        "pricing": None,
    }
