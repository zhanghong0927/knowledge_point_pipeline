"""Run small unresolved audit/review jobs independently on the designated API."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922/closeout_queue_20260923")
BASE = "http://jb-aionlineinferenceservice-161925863191374656-8000-nhss-job.v5000-prod.nhss.zhejianglab.com"
TIMEOUT_SECONDS = 600


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_ids(expected, actual):
    if len(actual) != len(expected) or set(actual) != set(expected) or len(set(actual)) != len(actual):
        raise ValueError("Result ID coverage mismatch")


def run_job(job):
    import runner

    job_dir = Path(job)
    items = read_jsonl(job_dir / "pending.jsonl")
    expected = [row["request_id"] for row in items]
    assert len(expected) == len(set(expected))
    judgments = runner.review_batch(items, job_dir / "run", chunk_size=80)
    validate_ids(expected, list(judgments))
    return {"records": len(items), "remaining_technical": sum(
        judgment["judgment"] == "technical_failure" for judgment in judgments.values()
    )}


def execute_one(job_dir):
    status_file = job_dir / "status.json"
    if status_file.exists():
        return json.loads(status_file.read_text(encoding="utf-8"))
    started = time.time()
    env = dict(os.environ, KNOWLEDGE_ENDPOINT_OVERRIDE=BASE, KNOWLEDGE_REVIEW_WORKERS="8")
    cmd = [sys.executable, __file__, "--job", str(job_dir)]
    with (job_dir / "run.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=TIMEOUT_SECONDS, check=False)
            exit_code = process.returncode
            state = "completed" if exit_code == 0 else "error"
        except subprocess.TimeoutExpired:
            exit_code = None
            state = "timeout"
    final = job_dir / "run" / "final.jsonl"
    rows = read_jsonl(final) if final.exists() else []
    expected = [row["request_id"] for row in read_jsonl(job_dir / "pending.jsonl")]
    if state == "completed":
        validate_ids(expected, [row["item"]["request_id"] for row in rows])
    remaining = sum(row["review"]["judgment"] == "technical_failure" for row in rows)
    result = {"job": job_dir.name, "state": state, "records": len(expected), "result_rows": len(rows),
              "remaining_technical": remaining, "exit_code": exit_code,
              "seconds": round(time.time() - started, 1), "endpoint": BASE}
    atomic_json(status_file, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path)
    args = parser.parse_args()
    if args.job:
        result = run_job(args.job)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    out = ROOT / "small_review_jobs"
    assert not out.exists(), f"Refusing to overwrite {out}"
    out.mkdir()
    grouped = defaultdict(list)
    for stage in ("audit", "review"):
        for row in read_jsonl(ROOT / f"{stage}_pending.jsonl"):
            grouped[(row["subject"], stage)].append(row["item"])
    jobs = []
    for (subject, stage), items in sorted(grouped.items()):
        job_dir = out / f"{subject}_{stage}"
        job_dir.mkdir()
        with (job_dir / "pending.jsonl").open("w", encoding="utf-8") as stream:
            for item in items:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        jobs.append(job_dir)
    assert sum(len(items) for items in grouped.values()) == 21
    print(json.dumps({"jobs": len(jobs), "records": 21, "max_parallel_jobs": 4,
                      "workers_per_job": 8, "timeout_seconds": TIMEOUT_SECONDS}, ensure_ascii=False), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(execute_one, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failure = {"job": job.name, "state": "orchestrator_error", "error": str(exc)}
                atomic_json(job / "status.json", failure)
                results.append(failure)
                print(json.dumps(failure, ensure_ascii=False), flush=True)
            atomic_json(out / "queue_status.json", {"completed_jobs": len(results), "total_jobs": len(jobs),
                                                    "results": results})


if __name__ == "__main__":
    main()
