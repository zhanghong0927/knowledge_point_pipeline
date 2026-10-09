from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

REQUIRED_CONSTRAINTS = {
    "constrain_root_candidates",
    "constrain_subtree_candidates",
    "first_whole_equipment_path_index",
    "has_component_to_whole_path_conflict",
    "has_post_route_path_conflict",
    "has_reason_path_conflict",
    "has_tool_to_whole_path_conflict",
    "is_malformed_abbreviation_entry",
    "is_organization_entry",
    "is_unresolved_multisense_dictionary_entry",
    "local_scope_exclusion",
    "safe_parent_fallback",
}


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", type=Path, required=True)
    args = parser.parse_args()
    base = args.profile_dir.resolve()
    profile = json.loads((base / "profile.json").read_text(encoding="utf-8"))

    def p(key: str) -> Path:
        value = Path(profile[key])
        return value if value.is_absolute() else base / value

    tree_path = p("knowledge_tree")
    cards_path = p("semantic_cards")
    prompt_path = p("prompt_module")
    constraints_path = p("routing_constraints")
    tree = json.loads(tree_path.read_text(encoding="utf-8"))

    codes: set[str] = set()
    duplicate_codes: list[str] = []
    missing_fields: list[dict[str, str]] = []

    def walk(node: dict[str, Any], parent: str = "") -> None:
        code = str(node.get("code") or "")
        if not code:
            missing_fields.append({"parent": parent, "field": "code"})
        elif code in codes:
            duplicate_codes.append(code)
        codes.add(code)
        if not str(node.get("name_zh") or node.get("name_en") or ""):
            missing_fields.append({"node_code": code, "field": "name_zh/name_en"})
        for child in node.get("children") or []:
            walk(child, code)

    walk(tree)
    card_codes: set[str] = set()
    parse_errors = 0
    if cards_path.exists():
        for line in cards_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                card_codes.add(str(json.loads(line).get("node_code") or ""))
            except json.JSONDecodeError:
                parse_errors += 1

    prompt = load_module(prompt_path, "profile_prompt")
    constraints = load_module(constraints_path, "profile_constraints")
    missing_prompt = [name for name in ("LEVEL_SYSTEM_PROMPT", "build_level_user_payload") if not hasattr(prompt, name)]
    missing_constraints = sorted(name for name in REQUIRED_CONSTRAINTS if not hasattr(constraints, name))
    report = {
        "profile_dir": str(base),
        "subject_name": profile.get("subject_name"),
        "root_code": tree.get("code"),
        "tree_nodes": len(codes),
        "semantic_cards": len(card_codes),
        "nodes_without_cards": len(codes - card_codes),
        "unknown_card_codes": sorted(card_codes - codes)[:50],
        "duplicate_codes": sorted(set(duplicate_codes)),
        "missing_tree_fields": missing_fields[:50],
        "semantic_card_parse_errors": parse_errors,
        "missing_prompt_contract": missing_prompt,
        "missing_constraint_contract": missing_constraints,
    }
    report["valid"] = not any(
        (
            report["duplicate_codes"],
            report["missing_tree_fields"],
            parse_errors,
            report["unknown_card_codes"],
            missing_prompt,
            missing_constraints,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()