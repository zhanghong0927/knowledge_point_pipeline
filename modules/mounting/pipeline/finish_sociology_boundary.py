"""Apply the independently reviewed final sociology boundary card."""

import json
from pathlib import Path

import generate_semantic_boundaries as boundary


RUN = Path("/home/wangqiyuan/work/single_endpoint_12subjects_run_v2_20260922/sociology/boundaries_v5")
GROUP = RUN / "groups" / "6ea9643000a61bd70b3a1563.json"
EVIDENCE = RUN / "concise_single_retry" / "N_8cb4882e2cecfaed.json"


def main():
    saved = json.loads(GROUP.read_text(encoding="utf-8"))
    assert saved["output"]["status"] == "technical_failure"
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["review"]["result"]["verdict"] == "pass"
    assert evidence["review"]["result"]["checked_codes"] == ["N_8cb4882e2cecfaed"]
    payload = saved["payload"]
    cards = boundary.validate_cards({"cards": evidence["generation"]["result"]},
                                    payload["targets"], payload["sibling_context"])
    backup = RUN / "failed_group_backups" / GROUP.name
    assert not backup.exists(), f"Backup already exists: {backup}"
    backup.parent.mkdir(exist_ok=True)
    boundary.atomic_json(backup, saved)
    boundary.atomic_json(GROUP, {"payload": payload, "output": {"status": "accepted_candidate", "cards": cards,
                           "rounds": [], "automatic_recovery": "concise_single_independent_review",
                           "evidence": str(EVIDENCE)}})
    print(json.dumps({"status": "accepted_candidate", "codes": [card["node_code"] for card in cards]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
