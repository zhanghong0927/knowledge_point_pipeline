"""Unmodified selected helpers from the previous dedup toolkit; no fusion code."""
import json
import re
from typing import Any

def normalize_api_url(value: str) -> str:
    value = value.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def extract_json_object(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        return json.loads(fenced.group(1))
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("response does not contain a JSON object")


def normalize_partition(raw: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    expected = [str(member["id"]) for member in task["members"]]
    expected_set = set(expected)
    assigned: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for cluster in raw.get("clusters") or []:
        member_ids = [str(value) for value in cluster.get("member_ids") or []]
        if len(member_ids) < 2 or len(member_ids) != len(set(member_ids)):
            raise ValueError("cluster must contain at least two distinct member_ids")
        if not set(member_ids) <= expected_set or assigned & set(member_ids):
            raise ValueError("cluster contains unknown or repeated member_id")
        canonical = str(cluster.get("canonical_record_id") or "")
        if canonical not in member_ids:
            raise ValueError("canonical_record_id is not in cluster")
        assigned.update(member_ids)
        clusters.append(
            {
                "member_ids": member_ids,
                "canonical_record_id": canonical,
                "reason": str(cluster.get("reason") or "")[:200],
            }
        )
    singletons = [str(value) for value in raw.get("singletons") or []]
    if len(singletons) != len(set(singletons)):
        raise ValueError("singletons contains duplicate IDs")
    if not set(singletons) <= expected_set or assigned & set(singletons):
        raise ValueError("singletons contains unknown or repeated member_id")
    assigned.update(singletons)
    if assigned != expected_set:
        raise ValueError("partition does not cover every member exactly once")
    return {"group_id": task["group_id"], "clusters": clusters, "singletons": singletons}


