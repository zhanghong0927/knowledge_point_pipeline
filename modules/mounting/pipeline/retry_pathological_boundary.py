"""Retry a small set of repetitive boundary generations without relaxing validation."""

import argparse
import json
from pathlib import Path

import generate_semantic_boundaries as boundary


SHORT_RULE = """
本次为技术性重试。上次输出发生循环重复。请对每个字段只写一次要点，禁止重复词语、重复短句或长段扩写。
每个目标节点仅用约200-350个中文字完成所有字段。definition和boundary各1-2句；includes和excludes各1-2条；
sibling_distinctions只选最易混淆的1个兄弟节点，可以为空数组。所有祖先和兄弟信息仍须逐项参考。
若发现自己重复措辞，立即结束该字段并完成JSON。只返回完整JSON，不加说明。
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    group = args.group
    saved = json.loads(group.read_text(encoding="utf-8"))
    assert saved["output"]["status"] == "technical_failure"
    payload = saved["payload"]
    config = json.loads((group.parent.parent / "config.json").read_text(encoding="utf-8"))
    model = config["model"]
    boundary.PROMPT += SHORT_RULE
    attempts = []
    cards = []
    for target in payload["targets"]:
        one = dict(payload, targets=[target])
        result = boundary.process_group(args.base, model, one, config["max_context_bytes"])
        attempts.append({"node_code": target["code"], "result": result})
        if result["status"] not in ("accepted_candidate", "advisory_candidate"):
            continue
        cards.extend(result["cards"])
    report_dir = group.parent.parent / "compact_retry_evidence"
    report_dir.mkdir(exist_ok=True)
    boundary.atomic_json(report_dir / group.name, attempts)
    if len(cards) != len(payload["targets"]):
        print(json.dumps({"complete": False, "statuses": [x["result"]["status"] for x in attempts]}, ensure_ascii=False))
        return
    cards = boundary.validate_cards({"cards": cards}, payload["targets"], payload["sibling_context"])
    review = boundary.call(args.base, model, boundary.REVIEW_PROMPT, dict(payload, cards=cards),
                           lambda value: boundary.validate_review(value, [x["code"] for x in payload["targets"]]))
    boundary.atomic_json(report_dir / (group.stem + ".review.json"), review)
    approved = bool(review["result"] and review["result"]["verdict"] == "pass")
    print(json.dumps({"complete": True, "approved": approved, "statuses": [x["result"]["status"] for x in attempts]}, ensure_ascii=False))
    if not approved or not args.apply:
        return
    backup = group.parent.parent / "failed_group_backups" / group.name
    backup.parent.mkdir(exist_ok=True)
    assert not backup.exists(), f"Backup already exists: {backup}"
    boundary.atomic_json(backup, saved)
    boundary.atomic_json(group, {"payload": payload, "output": {"status": "accepted_candidate", "cards": cards,
                          "rounds": [], "automatic_recovery": "compact_retry_independent_review",
                          "evidence": str(report_dir / group.name)}})


if __name__ == "__main__":
    main()
