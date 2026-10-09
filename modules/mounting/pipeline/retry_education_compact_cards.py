"""Retry failed education mounts with compacted candidate semantic cards."""

import json
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922")
SOURCE = ROOT / "education" / "context_budget_retry_20260923"
TARGET = ROOT / "education" / "compact_cards_retry_20260923"
SCRIPT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_20260922/mapping_runtime_compact_card_20260923.py")


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def run(command, log_path):
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False).returncode


def main():
    for _ in range(120):
        if (SOURCE / "report.json").exists():
            break
        time.sleep(5)
    assert (SOURCE / "report.json").exists(), "Prior retry did not finish"
    previous = read_jsonl(SOURCE / "new.jsonl")
    failed = [row for row in previous if row.get("knowledge_labeling", {}).get("status") == "failed"]
    expected = {str(row["record_id"]) for row in failed}
    assert len(previous) == 74 and expected and len(expected) == len(failed)
    assert not TARGET.exists(), f"Refusing to overwrite {TARGET}"
    TARGET.mkdir()
    shutil.copytree(SOURCE / "profile", TARGET / "profile")
    with (TARGET / "pending.jsonl").open("w", encoding="utf-8") as stream:
        for row in failed:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    command = json.loads((SOURCE / "command.json").read_text(encoding="utf-8"))
    command[1] = str(SCRIPT)
    for option, value in (
        ("--input", TARGET / "pending.jsonl"),
        ("--output", TARGET / "new.jsonl"),
        ("--profile-dir", TARGET / "profile"),
        ("--workers", 32),
        ("--retry-failed-workers", 32),
        ("--max-tokens", 4096),
    ):
        command[command.index(option) + 1] = str(value)
    (TARGET / "command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Retrying {len(failed)} records with 250-character candidate cards", flush=True)
    code = run(command, TARGET / "run.log")
    output = read_jsonl(TARGET / "new.jsonl") if (TARGET / "new.jsonl").exists() else []
    ids = [str(row["record_id"]) for row in output]
    assert len(output) == len(failed) and set(ids) == expected, "ID coverage mismatch"
    counts = {}
    for row in output:
        status = str(row.get("knowledge_labeling", {}).get("status", "missing"))
        counts[status] = counts.get(status, 0) + 1
    report = {"source_failed": len(failed), "output": len(output), "status": counts, "exit_code": code}
    (TARGET / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
