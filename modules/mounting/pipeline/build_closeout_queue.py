"""Snapshot the unfinished technical and semantic closeout work without API calls."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922")
OUT = ROOT / "closeout_queue_20260923"
SUBJECTS = [
    "mechanical_engineering", "architecture", "history", "literature",
    "economy", "military", "art", "civil_engineering", "management", "education",
]


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def pending_ids(subject_dir, stage):
    report = subject_dir / f"auto_resolved_{stage}" / "report.json"
    if report.exists():
        return set(json.loads(report.read_text(encoding="utf-8")).get("remaining_ids", []))
    resolved = subject_dir / f"resolved_{stage}_20260923" / "report.json"
    if resolved.exists():
        return set(json.loads(resolved.read_text(encoding="utf-8")).get("technical_ids", []))
    raise ValueError(f"No resolution report: {subject_dir.name}/{stage}")


def main():
    assert not OUT.exists(), f"Refusing to overwrite {OUT}"
    OUT.mkdir()
    buckets = {stage: [] for stage in ("audit", "review", "mount")}
    summary = {}
    for subject in SUBJECTS:
        d = ROOT / subject
        assert (d / "done.json").exists(), f"Not completed: {subject}"
        subject_counts = {}
        for stage in ("audit", "review"):
            ids = pending_ids(d, stage)
            originals = {row["item"]["request_id"]: row for row in read_jsonl(d / stage / "final.jsonl")}
            assert ids <= set(originals), f"Missing IDs in {subject}/{stage}"
            for ident in sorted(ids):
                row = originals[ident]
                buckets[stage].append({"subject": subject, "stage": stage, "request_id": ident,
                                       "item": row["item"], "prior_review": row["review"],
                                       "source": str(d / stage / "final.jsonl")})
            subject_counts[stage] = len(ids)
        report = d / "auto_retry_mount_excerpt" / "summary.json"
        if report.exists():
            mount_ids = {row["record_id"] for row in read_jsonl(d / "auto_retry_mount_excerpt" / "with_cards.jsonl")
                         if row.get("knowledge_labeling", {}).get("status") != "ok"}
            assert len(mount_ids) == json.loads(report.read_text(encoding="utf-8"))["remaining_technical"]
        else:
            old = d / "technical_retry_alt_mount_excerpt_v1" / "summary.json"
            mount_ids = set()
            if old.exists():
                assert json.loads(old.read_text(encoding="utf-8")).get("remaining") == 0
        if mount_ids:
            originals = {row["record_id"]: row for row in read_jsonl(d / "remount" / "input.jsonl")}
            assert mount_ids <= set(originals), f"Missing mount IDs: {subject}"
            for ident in sorted(mount_ids):
                buckets["mount"].append({"subject": subject, "stage": "mount", "record_id": ident,
                                         "item": originals[ident], "source": str(d / "remount" / "input.jsonl")})
        subject_counts["mount"] = len(mount_ids)
        original_summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        subject_counts["remount_review_unreasonable"] = original_summary["final"].get("unreasonable", 0)
        subject_counts["remount_review_uncertain"] = original_summary["final"].get("uncertain", 0)
        summary[subject] = subject_counts
    for stage, rows in buckets.items():
        write_jsonl(OUT / f"{stage}_pending.jsonl", rows)
    totals = {stage: len(rows) for stage, rows in buckets.items()}
    assert totals == {"audit": 11, "review": 10, "mount": 76}, totals
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {"generated": generated, "root": str(ROOT), "totals": totals, "subjects": summary,
               "note": "This is a technical queue snapshot, not a quality-pass certificate. Model-unreasonable rows are a separate semantic review backlog."}
    (OUT / "queue_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 12学科挂载质检收尾队列（运行中快照）", "",
        f"生成时间：{generated}。当前10个学科已完成主流程；社会学正在运行，哲学边界卡片已补齐、待主程序续跑。",
        "", "## 先完成主流程，再使用同一接口补技术失败", "",
        "1. 等社会学主流程完成，再让自动程序从检查点续跑哲学。两者均不切换接口。",
        "2. 已完成学科仍有技术失败97条：审核11条、重挂结果复核10条、重挂76条。优先处理21条审核/复核失败，再处理76条重挂失败；新结果写独立覆盖层，核对ID后才能合并。",
        "3. 教育学占重挂失败74条。已有两轮独立尝试，其中缩短输出预算恢复1条；精简候选卡片首轮恢复8条，但这些结果尚未完成独立复核，因此仍按原74条计入待办。等主接口负载下降后仅重试失败ID。",
        "4. 模型判为不合理或不确定的内容，与技术失败分开。已完成10学科的重挂后模型判不合理合计",
        f"   {sum(x['remount_review_unreasonable'] for x in summary.values())}条；这是待内容复核线索，不是已确认错误，不能直接批量删除或替换。",
        "", "## 按学科的未清技术失败", "",
        "| 学科 | 审核 | 重挂复核 | 重挂 | 合计 |", "| --- | ---: | ---: | ---: | ---: |",
    ]
    for subject, count in summary.items():
        total = sum(count[key] for key in ("audit", "review", "mount"))
        if total:
            lines.append(f"| {subject} | {count['audit']} | {count['review']} | {count['mount']} | {total} |")
    lines += ["", "详细ID和原始记录分别见 `audit_pending.jsonl`、`review_pending.jsonl`、`mount_pending.jsonl`。这些文件是只读快照，未修改正式交付。", ""]
    (OUT / "收尾待办_20260923.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "totals": totals, "subjects": len(summary)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
