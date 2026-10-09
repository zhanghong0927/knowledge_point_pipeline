"""Recover auditable language-only false deletions without additional API calls."""

import argparse
import collections
import csv
import hashlib
import json
import random
import re
import shutil
from pathlib import Path


LANGUAGE_FLAGS = {
    "language_mismatch", "name_language_mismatch", "missing_chinese_name",
    "name_language_error", "non_chinese_name", "language_swap",
    "wrong_language_name", "language_swap_needed", "language_swap_required",
    "field_language_mismatch", "wrong_language", "english_name_in_chinese_field",
    "name_in_wrong_language",
}
ALLOWED_FLAGS = LANGUAGE_FLAGS | {"field_drop_or_pair_inconsistent"}
CJK = re.compile(r"[\u3400-\u9fff]")
OTHER_PROBLEM = re.compile(
    r"残缺|残片|截断|乱码|错别字|拼写错误|不完整|不明确|歧义|过于宽泛|"
    r"过于笼统|上下文|依赖原文|一次性|特定案例|具体案例|特定实验|特定研究|"
    r"章节|栏目|目录|图表标题|文献引用|参考文献|书目|出版信息|"
    r"非独立|非通用|不是独立|不是通用|不构成|不是知识点|非知识点|"
    r"不属于|无关|越界|边界不清|交叉较弱|偏离|缺乏定义|定义不足|"
    r"定义.{0,12}(?:不符|错误|错配|不匹配|缺失)|"
    r"(?:非|而非|不是).{0,18}(?:核心|概念|知识单元)|"
    r"out.of.scope|truncat|incomplete|bibliograph|context.depend",
    re.I,
)
LANGUAGE_REASON = re.compile(r"英文|英语|中文|语言|language|English|Chinese", re.I)
FIELDS = (
    "id", "knowledge_point", "name", "definition", "en_definition",
    "description", "en_description", "main_tags", "related_tags", "tag", "source",
)


def read(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_rows(path, rows):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def key(row):
    return str(row.get("id", row.get("record_id")))


def english_only(text):
    return bool(re.search(r"[A-Za-z]", text)) and not CJK.search(text)


def check(row):
    judgment = row["model_cleaning"]
    flags = set(judgment.get("quality_flags") or [])
    if judgment.get("subject_relevant") is not True:
        return "subject_not_confirmed"
    if not any("language" in f or "non_chinese" in f or "missing_chinese" in f for f in flags):
        return "not_language_candidate"
    if flags - ALLOWED_FLAGS or not flags & LANGUAGE_FLAGS:
        return "additional_or_unrecognized_flags"
    if judgment.get("pair_consistency") != "not_applicable":
        return "pair_not_confirmed"
    if float(judgment.get("confidence") or 0) < 0.8:
        return "confidence_below_existing_threshold"
    field_results = judgment.get("field_results") or {}
    name_result = field_results.get("name") or {}
    en_result = field_results.get("knowledge_point") or {}
    if name_result.get("decision") != "drop" or en_result.get("decision") != "empty":
        return "field_contract_not_confirmed"
    reasons = [str(judgment.get("reason") or ""), str(name_result.get("reason") or "")]
    if not all(LANGUAGE_REASON.search(reason) for reason in reasons):
        return "language_only_reason_not_confirmed"
    if OTHER_PROBLEM.search(" ".join(reasons)):
        return "other_problem_in_reason"
    name = str(row.get("name") or "").strip()
    if not english_only(name) or str(row.get("knowledge_point") or "").strip():
        return "cannot_move_name_without_conflict"
    if len(name) == 1 or len(name) > 160 or re.search(r"https?://|www\.|@|[\ufffd\x00-\x08]", name, re.I):
        return "invalid_title_format"
    for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
        if name.count(left) != name.count(right):
            return "unbalanced_title_brackets"
    if not row.get("tag"):
        return "missing_subject_tag"
    for source, target in (("definition", "en_definition"), ("description", "en_description")):
        value = str(row.get(source) or "")
        existing = str(row.get(target) or "")
        if english_only(value) and existing and existing != value:
            return "content_language_field_conflict"
    return None


def convert(row):
    result = {field: row.get(field, [] if field == "related_tags" else "") for field in FIELDS}
    result["knowledge_point"] = row["name"]
    result["name"] = ""
    for source, target in (("definition", "en_definition"), ("description", "en_description")):
        if english_only(str(result[source])):
            result[target] = result[source]
            result[source] = ""
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    directory = args.final_dir
    drops = list(read(directory / "model_dropped.jsonl"))
    recovered, remaining, audits = [], [], []
    reasons = collections.Counter()
    for row in drops:
        failure = check(row)
        if failure:
            remaining.append(row)
            reasons[failure] += 1
        else:
            result = convert(row)
            recovered.append(result)
            audits.append({"id": row["id"], "tag": row["tag"],
                           "action": "restore_language_only", "original_record": row,
                           "restored_standard": result})
    keep = list(read(directory / "clean_standard.jsonl"))
    review = list(read(directory / "model_review.jsonl"))
    rule = list(read(directory / "rule_rejected.jsonl"))
    errors = list(read(directory / "model_errors.jsonl"))
    total = len(keep) + len(drops) + len(review) + len(rule) + len(errors)
    output = keep + recovered
    deleted = rule + remaining + review
    all_rows = output + deleted + errors
    assert len(all_rows) == total
    assert len({key(row) for row in all_rows}) == total
    assert all(row.get("tag") for row in all_rows)
    assert all(row["name"] == "" and row["knowledge_point"] for row in recovered)
    by_tag = collections.Counter(row["tag"] for row in recovered)
    report = {"input": total, "previous_keep": len(keep), "restored": len(recovered),
              "keep": len(output), "remaining_model_drop": len(remaining),
              "review_counted_deleted": len(review), "rule_rejected": len(rule),
              "all_deleted": len(deleted), "errors": len(errors),
              "retention_ratio": len(output) / total, "restored_by_tag": dict(by_tag),
              "exclusion_reasons": dict(reasons), "api_calls": 0,
              "allowed_flags": sorted(ALLOWED_FLAGS),
              "scope": "existing subject-related language-flagged model drops only",
              "content_policy": "move original fields without translation or rewriting"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.apply:
        return
    backup = directory / "before_language_only_recovery"
    if backup.exists():
        raise FileExistsError(f"Recovery already started; inspect backup before rerun: {backup}")
    backup.mkdir()
    manifest = {}
    for path in directory.iterdir():
        if path.is_file():
            shutil.copy2(path, backup / path.name)
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    dump(backup / "backup_sha256.json", manifest)
    write_rows(directory / "language_only_restored_standard.jsonl", recovered)
    write_rows(directory / "language_only_recovery_audit.jsonl", audits)
    write_rows(directory / "clean_standard.jsonl", output)
    write_rows(directory / "model_dropped.jsonl", remaining)
    write_rows(directory / "all_rejected.jsonl", rule + remaining)
    write_rows(directory / "all_deleted.jsonl", deleted)
    dump(directory / "language_only_recovery_report.json", report)
    random.Random(20260930).shuffle(output)
    write_rows(directory / "clean_sample_100.jsonl", output[:100])

    def counts(rows):
        return dict(sorted(collections.Counter(row["tag"] for row in rows).items()))

    stats = []
    for tag in sorted(counts(all_rows)):
        get = lambda rows: sum(row["tag"] == tag for row in rows)
        inp, kept, rr, md, rv, er = map(get, (all_rows, output, rule, remaining, review, errors))
        assert inp == kept + rr + md + rv + er
        stats.append({"学科": tag, "输入数": inp, "规则删除": rr, "模型输入": inp - rr,
                      "输出保留": kept, "Review（计入删除）": rv, "模型删除": md,
                      "模型错误": er, "总删除": rr + md + rv,
                      "保留比例": f"{kept / inp:.2%}", "删除比例": f"{(rr + md + rv) / inp:.2%}"})
    columns = list(stats[0])
    sums = {c: sum(r[c] for r in stats) for c in columns[1:-2]}
    stats.append({"学科": "合计", **sums, "保留比例": f"{len(output) / total:.2%}",
                  "删除比例": f"{len(deleted) / total:.2%}"})
    with (directory / "subject_input_output_retention_statistics.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(stats)
    table = "| " + " | ".join(columns) + " |\n| " + " | ".join(["---"] + ["---:"] * (len(columns)-1)) + " |\n"
    table += "".join("| " + " | ".join(str(row[c]) for c in columns) + " |\n" for row in stats)
    table += "\nReview 计入删除；输出保留包含通过语言字段迁移恢复的记录。模型原判定保存在备份和恢复审计中。\n"
    (directory / "subject_input_output_retention_statistics.md").write_text(table, encoding="utf-8")
    summary = json.loads((directory / "cleaning_summary.json").read_text())
    summary["original_model_counts_before_language_recovery"] = summary["model_counts"]
    summary["model_counts"] = {"keep": len(output), "drop": len(remaining), "review": len(review), "error": len(errors)}
    summary["language_only_recovery"] = report
    summary["all_rejected_rule_plus_model_drop"] = len(rule) + len(remaining)
    summary["accounted_total"] = total
    summary["count_conserved"] = True
    summary["final_subject_statistics"] = stats
    summary["by_subject_note"] = "by_subject retains original API decisions; use final_subject_statistics for current delivery counts"
    dump(directory / "cleaning_summary.json", summary)
    dump(directory / "subject_tag_report.json", {"total": len(output), "missing_tag": 0, "tag_counts": counts(output)})
    deleted_report = {"counts": {"model_dropped": len(remaining), "rule_rejected": len(rule), "review": len(review)},
                      "tag_counts": {"model_dropped": counts(remaining), "rule_rejected": counts(rule), "review": counts(review)},
                      "all_deleted_including_review": len(deleted),
                      "all_rejected_rule_plus_model_drop": len(rule) + len(remaining)}
    dump(directory / "deleted_subject_tag_report.json", deleted_report)
    validation = {"rows": len(output), "missing_required_fields": sum(not set(FIELDS) <= row.keys() for row in output),
                  "both_names_empty": sum(not row.get("name") and not row.get("knowledge_point") for row in output),
                  "duplicate_id_count": len(output) - len({key(row) for row in output}),
                  "empty_definition": sum(not row.get("definition") and not row.get("en_definition") for row in output),
                  "missing_tag": sum(not row.get("tag") for row in output)}
    dump(directory / "validation_report.json", validation)
    summary["validation"] = validation
    summary["deleted_subject_tag"] = deleted_report
    summary["subject_tag"] = {"field": "tag", "total": len(output), "counts": counts(output)}
    summary["outputs"]["language_only_restored"] = str(directory / "language_only_restored_standard.jsonl")
    dump(directory / "cleaning_summary.json", summary)


if __name__ == "__main__":
    main()
