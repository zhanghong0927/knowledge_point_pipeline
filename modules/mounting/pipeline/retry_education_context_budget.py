"""Retry only failed education remount records with a smaller output budget."""

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922")
SOURCE = ROOT / "education" / "auto_retry_mount_excerpt"
TARGET = ROOT / "education" / "context_budget_retry_20260923"


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main():
    source_rows = read_jsonl(SOURCE / "with_cards.jsonl")
    failed = [row for row in source_rows if row.get("knowledge_labeling", {}).get("status") == "failed"]
    failed_ids = [str(row["record_id"]) for row in failed]
    assert len(source_rows) == 78 and len(failed) == 74 and len(set(failed_ids)) == 74
    assert not TARGET.exists(), f"Refusing to overwrite {TARGET}"
    TARGET.mkdir()
    with (TARGET / "pending.jsonl").open("w", encoding="utf-8") as stream:
        for row in failed:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    shutil.copytree(SOURCE / "profile", TARGET / "profile")
    command = json.loads((SOURCE / "command.json").read_text(encoding="utf-8"))
    for option, value in (
        ("--input", TARGET / "pending.jsonl"),
        ("--output", TARGET / "new.jsonl"),
        ("--profile-dir", TARGET / "profile"),
        ("--workers", 32),
        ("--retry-failed-workers", 32),
        ("--max-tokens", 3072),
    ):
        command[command.index(option) + 1] = str(value)
    (TARGET / "command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Retrying {len(failed)} failed records with max_tokens=3072", flush=True)
    with (TARGET / "run.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
    print(f"exit_code={result.returncode}", flush=True)
    output = read_jsonl(TARGET / "new.jsonl") if (TARGET / "new.jsonl").exists() else []
    output_ids = [str(row["record_id"]) for row in output]
    assert len(output) == len(failed) and set(output_ids) == set(failed_ids), "ID coverage mismatch"
    counts = {}
    for row in output:
        label = row.get("knowledge_labeling", {})
        status = str(label.get("status", "missing"))
        counts[status] = counts.get(status, 0) + 1
    report = {"source_failed": len(failed), "output": len(output), "status": counts, "exit_code": result.returncode}
    (TARGET / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
