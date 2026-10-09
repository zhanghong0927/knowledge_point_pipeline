"""Combine independently reviewed cards for the last philosophy boundary group."""

import argparse
import json
from pathlib import Path

import generate_semantic_boundaries as boundary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run = args.run
    group = run / "groups" / "53ad13f6e27d6a5e725a4020.json"
    saved = json.loads(group.read_text(encoding="utf-8"))
    assert saved["output"]["status"] == "technical_failure"
    payload = saved["payload"]
    target_codes = [target["code"] for target in payload["targets"]]
    single = json.loads((run / "concise_single_retry" / "N_e1c459b5564721cc.json").read_text(encoding="utf-8"))
    assert single["review"]["result"]["verdict"] == "pass"
    assert single["review"]["result"]["checked_codes"] == ["N_e1c459b5564721cc"]
    cards = list(single["generation"]["result"])
    others = json.loads((run / "compact_retry_evidence" / group.name).read_text(encoding="utf-8"))
    for item in others:
        if item["node_code"] == "N_e1c459b5564721cc":
            continue
        assert item["result"]["status"] == "accepted_candidate"
        assert len(item["result"]["cards"]) == 1
        cards.extend(item["result"]["cards"])
    assert {card["node_code"] for card in cards} == set(target_codes)
    cards = boundary.validate_cards({"cards": cards}, payload["targets"], payload["sibling_context"])
    config = json.loads((run / "config.json").read_text(encoding="utf-8"))
    review = boundary.call(args.base, config["model"], boundary.REVIEW_PROMPT, dict(payload, cards=cards),
                           lambda value: boundary.validate_review(value, target_codes))
    boundary.atomic_json(run / "philosophy_final_group_review.json", review)
    verdict = (review["result"] or {}).get("verdict")
    print(json.dumps({"all_cards_valid": True, "review": verdict, "codes": target_codes}, ensure_ascii=False), flush=True)
    if not args.apply or verdict != "pass":
        return
    backup = run / "failed_group_backups" / group.name
    assert not backup.exists(), f"Backup already exists: {backup}"
    boundary.atomic_json(backup, saved)
    boundary.atomic_json(group, {"payload": payload, "output": {"status": "accepted_candidate", "cards": cards,
                          "rounds": [], "automatic_recovery": "concise_target_full_group_review",
                          "evidence": str(run / "philosophy_final_group_review.json")}})


if __name__ == "__main__":
    main()
