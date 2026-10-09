"""Other-subject routing constraint template.

All functions intentionally default to no-op. Copy this profile, then add only
rules that are stable and well justified for the target subject. Keep every
public function signature unchanged because the routing engine loads this file
as a plug-in contract.
"""
from __future__ import annotations
from typing import Any


def local_scope_exclusion(card: dict[str, Any]) -> tuple[str, str, str] | None:
    return None


def is_organization_entry(card: dict[str, Any]) -> bool:
    return False


def is_malformed_abbreviation_entry(card: dict[str, Any]) -> bool:
    return False


def is_unresolved_multisense_dictionary_entry(card: dict[str, Any]) -> bool:
    return False


def constrain_root_candidates(
    card: dict[str, Any],
    candidates: list[dict[str, Any]],
    by_code: dict[str, Any],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    return candidates, [], None


def constrain_subtree_candidates(
    card: dict[str, Any],
    current_code: str,
    candidates: list[dict[str, Any]],
    by_code: dict[str, Any],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    return candidates, [], None


def has_post_route_path_conflict(
    card: dict[str, Any], path_names: list[str] | tuple[str, ...]
) -> tuple[str, str] | None:
    return None


def safe_parent_fallback(
    card: dict[str, Any], path_names: list[str] | tuple[str, ...]
) -> tuple[int, str, str] | None:
    return None


def has_reason_path_conflict(
    reason: str, path_names: list[str] | tuple[str, ...] = ()
) -> bool:
    return False


def first_whole_equipment_path_index(
    path_names: list[str] | tuple[str, ...]
) -> int | None:
    return None


def has_component_to_whole_path_conflict(
    card: dict[str, Any], path_names: list[str] | tuple[str, ...]
) -> bool:
    return False


def has_tool_to_whole_path_conflict(
    card: dict[str, Any], path_names: list[str] | tuple[str, ...]
) -> bool:
    return False