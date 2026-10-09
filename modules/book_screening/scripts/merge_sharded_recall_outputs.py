from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


OUTPUT_NAMES = (
    "{subject}_宽召回全集.csv",
    "{subject}_满足后续处理条件.csv",
    "{subject}_硬规则排除清单.csv",
)


def load_subject(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return str(data["subject_name"]), str(data["subject_slug"])


def merge_csv_parts(parts: list[Path], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    expected_header = None
    with temporary.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                header = source.readline()
                if not header:
                    raise ValueError(f"CSV 分片为空: {part}")
                if expected_header is None:
                    expected_header = header
                    output.write(header)
                elif header != expected_header:
                    raise ValueError(f"CSV 分片字段不一致: {part}")
                shutil.copyfileobj(source, output, length=1024 * 1024)
    os.replace(temporary, target)


def merge_shards(
    shards_root: Path,
    output_root: Path,
    config_paths: list[Path],
    *,
    expected_shards: int,
) -> dict[str, Any]:
    shard_dirs = [shards_root / f"shard_{index:02d}" for index in range(expected_shards)]
    missing = [str(path) for path in shard_dirs if not path.is_dir()]
    if missing:
        raise FileNotFoundError(f"缺少分片目录: {missing}")

    shard_summaries = [
        json.loads((path / "全库宽召回汇总.json").read_text(encoding="utf-8"))
        for path in shard_dirs
    ]
    source_row_counts = {
        int(summary.get("source_rows_seen", summary.get("total_rows", 0)))
        for summary in shard_summaries
    }
    if len(source_row_counts) != 1:
        raise ValueError(f"各分片读取的源记录数不一致: {sorted(source_row_counts)}")
    source_rows = source_row_counts.pop()
    shard_indexes = {int(summary.get("shard_index", -1)) for summary in shard_summaries}
    shard_counts = {int(summary.get("shard_count", 0)) for summary in shard_summaries}
    processed_rows = sum(int(summary.get("processed_rows", 0)) for summary in shard_summaries)
    coverage_ok = (
        shard_indexes == set(range(expected_shards))
        and shard_counts == {expected_shards}
        and processed_rows == source_rows
    )
    if not coverage_ok:
        raise ValueError(
            "分片覆盖不完整: "
            f"indexes={sorted(shard_indexes)}, counts={sorted(shard_counts)}, "
            f"processed={processed_rows}, source={source_rows}"
        )

    output_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "input_csv": shard_summaries[0].get("input_csv", ""),
        "source_rows": source_rows,
        "processed_rows": processed_rows,
        "expected_shards": expected_shards,
        "coverage_ok": True,
        "subjects": {},
    }
    for config_path in config_paths:
        subject_name, subject_slug = load_subject(config_path)
        subject_summaries = []
        for shard in shard_dirs:
            summary_path = shard / subject_slug / f"{subject_name}_宽召回统计.json"
            subject_summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))

        subject_output = output_root / subject_slug
        for pattern in OUTPUT_NAMES:
            file_name = pattern.format(subject=subject_name)
            merge_csv_parts(
                [shard / subject_slug / file_name for shard in shard_dirs],
                subject_output / file_name,
            )

        numeric = Counter()
        channels = Counter()
        reasons = Counter()
        for summary in subject_summaries:
            for key in ("recalled", "strong_recall", "weak_recall", "eligible", "hard_rule_excluded"):
                numeric[key] += int(summary.get(key, 0))
            channels.update(summary.get("channel_counts") or {})
            reasons.update(summary.get("hard_rule_reason_counts") or {})
        if numeric["recalled"] != numeric["eligible"] + numeric["hard_rule_excluded"]:
            raise ValueError(f"{subject_name} 宽召回与硬规则分区数量不守恒")
        if numeric["recalled"] != numeric["strong_recall"] + numeric["weak_recall"]:
            raise ValueError(f"{subject_name} 强弱召回数量不守恒")
        subject_summary = {
            "subject_name": subject_name,
            **dict(numeric),
            "channel_counts": dict(channels),
            "hard_rule_reason_counts": dict(reasons),
            "outputs": {
                "all_recalled": str(subject_output / OUTPUT_NAMES[0].format(subject=subject_name)),
                "eligible": str(subject_output / OUTPUT_NAMES[1].format(subject=subject_name)),
                "hard_rule_excluded": str(subject_output / OUTPUT_NAMES[2].format(subject=subject_name)),
            },
        }
        (subject_output / f"{subject_name}_宽召回统计.json").write_text(
            json.dumps(subject_summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report["subjects"][subject_slug] = subject_summary

    (output_root / "全库宽召回汇总.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="合并流式学科宽召回的记录分片")
    parser.add_argument("--shards-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument("--expected-shards", type=int, required=True)
    args = parser.parse_args()
    report = merge_shards(
        args.shards_root,
        args.output,
        args.config,
        expected_shards=args.expected_shards,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
