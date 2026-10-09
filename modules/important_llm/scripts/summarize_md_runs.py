"""汇总已保存的执行事件，区分累计工作量、并行时间跨度和最新书籍结果。

不重新提取、不猜测价格或缺失用量；只在显式调用入口时写统计文件。"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from book_extractor.telemetry import _read_events, summarize_events


def _timestamp(value: str) -> float:
    """解析带时区的 value 时间字符串并返回时间戳。

    缺少时区时抛出 ValueError，不根据本机时区猜测执行顺序。"""
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("Execution timestamps must include a timezone")
    return date.timestamp()


def _unique(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 event_id 去重 events，返回按时间排序的唯一事件列表。

    空 ID 或同 ID 的不同内容抛出 ValueError，不任意选取冲突副本。"""
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        identity = event.get("event_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Every accounting event must have a nonempty event_id")
        if identity in found and found[identity] != event:
            raise ValueError(f"Conflicting copies of event_id {identity}")
        found[identity] = event
    return sorted(found.values(), key=lambda event: _timestamp(event["timestamp_utc"]))


def _aggregate(
    events: list[dict[str, Any]],
    *,
    incomplete: bool = False,
    missing_logs: int = 0,
    combined: bool = True,
) -> dict[str, Any]:
    """聚合 events 并返回标准用量和延迟统计，保留缺失覆盖标记。

    incomplete、missing_logs 描述输入缺口，combined 决定是否移除单书字段；
    只使用临时文件复用既有聚合器，不向被统计目录写合成事件。"""
    unique = _unique(events)
    # A transient file lets the existing single-log reducer own usage and latency
    # semantics. The measured directories receive no synthetic logs or rewrites.
    with tempfile.TemporaryDirectory(prefix="md-statistics-") as folder:
        path = Path(folder) / "events.jsonl"
        path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in unique),
            encoding="utf-8",
        )
        result = summarize_events(path)
    result["incomplete_final_line"] = incomplete
    result["missing_execution_logs"] = missing_logs
    result["usage_is_complete"] = (
        result["usage_is_complete"] and not incomplete and not missing_logs
    )
    for stage in result["stages"]:
        stage["usage_is_complete"] = (
            stage["usage_is_complete"] and not incomplete and not missing_logs
        )
    if missing_logs and not unique:
        result["usage"] = {field: None for field in result["usage"]}
    if combined:
        result.pop("book", None)
    return result


def _timing(events: list[dict[str, Any]], expected_executions: int) -> dict[str, Any]:
    """由 events 与 expected_executions 计算工作时长和并行时间跨度。

    返回已知累计耗时、缺失次数与跨度完整性；不把并行跨度当作时长之和。"""
    starts = [event for event in events if event["event"] == "book_started"]
    ends = [event for event in events if event["event"] == "book_finished"]
    elapsed = [
        event["elapsed_seconds"]
        for event in ends
        if event.get("elapsed_seconds") is not None
    ]
    span = None
    if starts and ends:
        first_start = min(_timestamp(event["timestamp_utc"]) for event in starts)
        last_end = max(_timestamp(event["timestamp_utc"]) for event in ends)
        span = last_end - first_start
    start_ids = {event["execution_id"] for event in starts}
    end_ids = {event["execution_id"] for event in ends}
    span_complete = bool(starts) and len(starts) == expected_executions
    span_complete = span_complete and start_ids == end_ids
    return {
        "execution_wall_seconds_known_sum": sum(elapsed) if elapsed else None,
        "execution_wall_seconds_missing": max(0, expected_executions - len(elapsed)),
        "observed_batch_span_seconds": span,
        "span_is_complete": span_complete,
    }


def _complete_records(
    directory: Path, manifest: dict[str, Any], status: str
) -> tuple[int | None, str | None]:
    """检查 directory 中 status 对应的记录数量，返回数量与可选问题码。

    manifest 用于核对保存数量；未完成运行返回零，完成但输出缺失或损坏时返回未知。"""
    if status != "complete":
        return 0, None
    path = directory / "records.jsonl"
    if not path.exists():
        return None, "complete_run_missing_records"
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (ValueError, UnicodeError):
        return None, "invalid_records_jsonl"
    if any(not isinstance(row, dict) for row in rows):
        return None, "invalid_record_object"
    if manifest.get("records") is not None and manifest["records"] != len(rows):
        return None, "record_count_disagrees_with_manifest"
    return len(rows), None


def summarize_runs(root: Path) -> dict[str, Any]:
    """读取 root 下的运行与事件，返回去重历史和每书最新执行统计。

    不改写源日志；身份冲突会抛出 ValueError，缺失日志或记录不会被冒充零成本。"""
    root = root.resolve()
    runs: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    execution_owners: dict[str, str] = {}
    run_ids: set[str] = set()
    incomplete_any = False
    missing_runs = 0
    missing_logs = 0
    for manifest_path in sorted(root.glob("*/manifest.json")):
        directory = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = manifest["run_id"]
        if run_id in run_ids:
            raise ValueError(f"Duplicate run manifest: {run_id}")
        run_ids.add(run_id)
        executions: dict[str, dict[str, Any]] = {}
        for path in sorted((directory / "executions").glob("*/events.jsonl")):
            events, incomplete = _read_events(path)
            ids = {event.get("execution_id") for event in events}
            if len(ids) > 1 or (ids and (None in ids or "" in ids)):
                raise ValueError(f"Mixed or missing execution IDs in {path}")
            execution_id = next(iter(ids)) if ids else path.parent.name
            if any(
                event.get("run_id") != run_id
                or event.get("book_id") != manifest["book_id"]
                for event in events
            ):
                raise ValueError(f"Wrong run/book identity in {path}")
            if (
                execution_id in execution_owners
                and execution_owners[execution_id] != run_id
            ):
                raise ValueError(
                    f"Execution {execution_id} is attributed to multiple runs"
                )
            execution_owners[execution_id] = run_id
            entry = executions.setdefault(
                execution_id,
                {"events": [], "paths": [], "incomplete": False, "mtime": 0},
            )
            entry["events"].extend(events)
            entry["paths"].append(str(path.relative_to(root)))
            entry["incomplete"] |= incomplete
            entry["mtime"] = max(entry["mtime"], path.stat().st_mtime)
        execution_rows = []
        run_events: list[dict[str, Any]] = []
        run_incomplete = False
        for execution_id, entry in executions.items():
            events = _unique(entry["events"])
            run_events.extend(events)
            run_incomplete |= entry["incomplete"]
            timestamps = [_timestamp(event["timestamp_utc"]) for event in events]
            starts = [event for event in events if event["event"] == "book_started"]
            if starts:
                start_time = _timestamp(starts[0]["timestamp_utc"])
                selection_basis = "execution_start"
            elif timestamps:
                start_time = min(timestamps)
                selection_basis = "first_event"
            else:
                start_time = entry["mtime"]
                selection_basis = "event_file_mtime_fallback"
            summary = _aggregate(
                events,
                incomplete=entry["incomplete"],
                missing_logs=int(not events),
                combined=False,
            )
            execution_rows.append(
                {
                    "execution_id": execution_id,
                    "event_paths": entry["paths"],
                    "start_epoch": start_time,
                    "selection_basis": selection_basis,
                    "empty_log": not events,
                    "statistics": summary,
                }
            )
        execution_rows.sort(
            key=lambda row: (
                row["start_epoch"] is not None,
                row["start_epoch"] or 0,
                row["execution_id"],
            )
        )
        latest = execution_rows[-1] if execution_rows else None
        latest_book = latest["statistics"].get("book") if latest else None
        fallback_status = "unfinished" if latest else manifest.get("status", "unknown")
        status = (latest_book or {}).get("status") or fallback_status
        records, record_issue = _complete_records(directory, manifest, status)
        characters = manifest.get("source_statistics", {}).get("characters")
        if characters is None:
            characters = (latest_book or {}).get("source_characters")
        if characters is None and (directory / "source.md").exists():
            characters = len((directory / "source.md").read_bytes().decode("utf-8-sig"))
        missing_runs += not executions
        run_missing_logs = int(not executions) + sum(
            row["empty_log"] for row in execution_rows
        )
        missing_logs += run_missing_logs
        incomplete_any |= run_incomplete
        run_events = _unique(run_events)
        all_events.extend(run_events)
        history = _aggregate(
            run_events, incomplete=run_incomplete, missing_logs=run_missing_logs
        )
        history["timing"] = _timing(run_events, len(executions))
        history["timing"]["span_is_complete"] &= (
            not run_incomplete and not run_missing_logs
        )
        if latest and latest["start_epoch"] is not None:
            selection_epoch = latest["start_epoch"]
        else:
            selection_epoch = manifest_path.stat().st_mtime
        runs.append(
            {
                "run_id": run_id,
                "book_id": manifest["book_id"],
                "title": manifest.get("title"),
                "directory": str(directory.relative_to(root)),
                "status": status,
                "manifest_status": manifest.get("status"),
                "telemetry": "present" if executions else "missing",
                "source_characters": characters,
                "complete_records": records,
                "output_count_issue": record_issue,
                "latest_execution_id": latest["execution_id"] if latest else None,
                "latest_execution": latest["statistics"] if latest else None,
                "latest_selection_epoch": selection_epoch,
                "latest_selection_basis": latest["selection_basis"]
                if latest
                else "manifest_mtime_fallback",
                "executions": execution_rows,
                "history": history,
            }
        )
    unique_events = _unique(all_events)
    history = _aggregate(
        unique_events, incomplete=incomplete_any, missing_logs=missing_logs
    )
    history["timing"] = _timing(unique_events, len(execution_owners))
    history["timing"]["span_is_complete"] &= not incomplete_any and not missing_logs
    books: dict[str, dict[str, Any]] = {}
    for run in sorted(
        runs, key=lambda row: (row["latest_selection_epoch"], row["run_id"])
    ):
        books[run["book_id"]] = run
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "definitions": {
            "history": (
                "Unique recorded events across all executions; "
                "known token sums with unknown-usage counts."
            ),
            "latest_book": (
                "Latest execution-start run per book; manifest mtime fallback "
                "is labelled when logs are missing."
            ),
            "complete_records": (
                "Records counted once per latest book only when its current "
                "status is complete; not a quality approval."
            ),
            "timing": (
                "Execution wall seconds are summed work. Batch span is min "
                "start to max finish, including gaps, not their sum."
            ),
            "missing_logs": (
                "Unknown historical cost; observed counters are not a claim "
                "of zero API requests."
            ),
        },
        "run_count": len(runs),
        "book_count": len(books),
        "execution_count": len(execution_owners),
        "runs_missing_telemetry": missing_runs,
        "run_status_counts": dict(Counter(run["status"] for run in runs)),
        "latest_book_status_counts": dict(
            Counter(run["status"] for run in books.values())
        ),
        "source_characters_known_sum": sum(
            run["source_characters"]
            for run in books.values()
            if run["source_characters"] is not None
        ),
        "source_characters_missing_books": sum(
            run["source_characters"] is None for run in books.values()
        ),
        "complete_records_known_sum": sum(
            run["complete_records"]
            for run in books.values()
            if run["complete_records"] is not None
        ),
        "complete_records_unknown_books": sum(
            run["complete_records"] is None for run in books.values()
        ),
        "latest_run_by_book": {
            book_id: run["run_id"] for book_id, run in books.items()
        },
        "history": history,
        "runs": runs,
        "pricing": None,
    }


def main() -> None:
    """读取命令行 root，写 statistics.json 并返回 None。

    只统计已保存的输出，不重新提取、不推算未提供的价格。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("root must be an existing runs directory")
    result = summarize_runs(args.root)
    path = args.root / "statistics.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "statistics": str(path.resolve()),
                "runs": result["run_count"],
                "books": result["book_count"],
                "executions": result["execution_count"],
            }
        )
    )


if __name__ == "__main__":
    main()
