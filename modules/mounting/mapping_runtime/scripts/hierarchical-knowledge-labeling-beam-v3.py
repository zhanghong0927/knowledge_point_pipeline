"""Configurable evidence-bounded subject knowledge-tree routing with token accounting.

V3 distinguishes a leaf attachment from an evidence-supported parent
attachment. It records token use per record, source type, top-level subject
domain, and decision. Subject-specific trees, semantic cards, prompts, and
routing constraints are loaded from a profile directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional display dependency
    tqdm = None

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = PACKAGE_DIR / "profiles" / "mechanical_engineering"
DEFAULT_API_URL = "http://jb-aionlineinferenceservice-160533055188521088-8000-nhss-job.v5000-prod.nhss.zhejianglab.com/v1/chat/completions"
DEFAULT_MODEL = "/mnt/si002647a3lv/zhanghong/model/huggingface/Qwen/Qwen3.6-35B-A3B"
CONSTRAINT_FUNCTIONS = (
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
)
RETRYABLE_ERROR_MARKERS = (
    "http 429",
    "http 5",
    "timed out",
    "timeout",
    "connection reset",
    "temporarily unavailable",
    "incompleteread",
    "incomplete read",
    "remote end closed",
    "connection aborted",
    "connection broken",
    "server disconnected",
    "urlopen error",
    "ssl",
    "record layer failure",
    "jsondecodeerror",
    "expecting value",
    "extra data",
)


class SimpleProgress:
    """Tiny tqdm fallback so batch runs still show progress and ETA."""

    def __init__(self, total: int, desc: str, unit: str = "rec") -> None:
        self.total = max(total, 1)
        self.desc = desc
        self.unit = unit
        self.count = 0
        self.failed = 0
        self.start = time.perf_counter()
        self.last_draw = 0.0
        self._draw(force=True)

    @staticmethod
    def _format_seconds(seconds: float | None) -> str:
        if seconds is None or math.isinf(seconds):
            return "?:??"
        seconds = max(0, int(seconds))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:d}:{sec:02d}"

    def set_postfix(self, **kwargs: Any) -> None:
        self.failed = int(kwargs.get("failed", self.failed))

    def update(self, n: int = 1) -> None:
        self.count += n
        self._draw()

    def _draw(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and self.count < self.total and now - self.last_draw < 0.5:
            return
        self.last_draw = now
        elapsed = max(now - self.start, 1e-9)
        rate = self.count / elapsed
        eta = (self.total - self.count) / rate if rate > 0 else None
        width = 30
        filled = min(width, int(width * self.count / self.total))
        bar = "█" * filled + " " * (width - filled)
        percent = 100 * self.count / self.total
        line = (
            f"\r{self.desc}: {percent:5.1f}%|{bar}| "
            f"{self.count}/{self.total} "
            f"[{self._format_seconds(elapsed)}<{self._format_seconds(eta)}, "
            f"{rate:.2f}{self.unit}/s, failed={self.failed}]"
        )
        sys.stderr.write(line)
        sys.stderr.flush()

    def close(self) -> None:
        self._draw(force=True)
        sys.stderr.write("\n")
        sys.stderr.flush()

@dataclass
class TokenUsage:
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_input_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.api_calls += other.api_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_hit_input_tokens += other.cache_hit_input_tokens
        self.total_tokens += other.total_tokens

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class TreeNode:
    code: str
    name_zh: str
    name_en: str
    path: str
    semantic_card: str
    examples: list[str]
    children: list["TreeNode"] = field(default_factory=list)


@dataclass
class Beam:
    node: TreeNode | None
    path_codes: list[str]
    path_names: list[str]
    log_score: float
    level_scores: list[dict[str, Any]]
    rejected_next_candidates: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    truncated: bool = False
    stopped_by_evidence: bool = False
    stop_reason: str = ""

    @property
    def path_score(self) -> float:
        return math.exp(self.log_score / len(self.level_scores)) if self.level_scores else 0.0


def load_python_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_prompt_module(path: Path) -> Any:
    module = load_python_module(path, "subject_mapping_prompt_v3")
    for name in ("LEVEL_SYSTEM_PROMPT", "build_level_user_payload"):
        if not hasattr(module, name):
            raise RuntimeError(f"Prompt module missing {name}: {path}")
    return module


def install_constraint_module(path: Path) -> None:
    module = load_python_module(path, "subject_routing_constraints_v1")
    missing = [name for name in CONSTRAINT_FUNCTIONS if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"Routing constraints missing functions {missing}: {path}")
    for name in CONSTRAINT_FUNCTIONS:
        globals()[name] = getattr(module, name)


def resolve_profile(args: argparse.Namespace) -> None:
    profile_dir = args.profile_dir.resolve()
    profile_file = profile_dir / "profile.json"
    if not profile_file.exists():
        raise FileNotFoundError(f"Missing subject profile: {profile_file}")
    profile = json.loads(profile_file.read_text(encoding="utf-8"))

    def profile_path(cli_value: Path | None, key: str) -> Path:
        value = cli_value or Path(str(profile.get(key, "")))
        if not str(value):
            raise ValueError(f"Subject profile missing path setting: {key}")
        return value if value.is_absolute() else profile_dir / value

    args.subject_name = args.subject_name or str(profile.get("subject_name") or "目标学科")
    args.subject_scope = args.subject_scope or str(profile.get("subject_scope") or "")
    args.knowledge_tree = profile_path(args.knowledge_tree, "knowledge_tree")
    args.semantic_cards = profile_path(args.semantic_cards, "semantic_cards")
    args.prompt_module = profile_path(args.prompt_module, "prompt_module")
    args.routing_constraints = profile_path(args.routing_constraints, "routing_constraints")

    for label, path in (
        ("knowledge tree", args.knowledge_tree),
        ("prompt module", args.prompt_module),
        ("routing constraints", args.routing_constraints),
    ):
        if not path.exists():
            raise FileNotFoundError(f"Missing {label}: {path}")


def clip(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(result, 0.0), 1.0) if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return default


def usage_from_api(raw: dict[str, Any]) -> TokenUsage:
    details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    cache_tokens = integer(
        details.get("cached_tokens", details.get("cache_read_input_tokens", 0))
        or raw.get("cached_input_tokens", raw.get("cache_read_input_tokens", 0))
    )
    input_tokens = integer(raw.get("prompt_tokens", raw.get("input_tokens", 0)))
    output_tokens = integer(raw.get("completion_tokens", raw.get("output_tokens", 0)))
    total_tokens = integer(raw.get("total_tokens", input_tokens + output_tokens))
    return TokenUsage(1, input_tokens, output_tokens, cache_tokens, total_tokens)


def extract_json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def load_cards(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {item["node_code"]: item for item in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())}


def build_tree(raw: dict[str, Any], cards: dict[str, dict[str, Any]]) -> TreeNode:
    code = raw["code"]
    card = cards.get(code, {})
    return TreeNode(
        code=code,
        name_zh=raw.get("name_zh", ""),
        name_en=raw.get("name_en", ""),
        path=raw.get("path", ""),
        semantic_card=card.get("semantic_card", raw.get("path", "")),
        examples=[item.get("term", "") for item in card.get("seed_examples", [])],
        children=[build_tree(child, cards) for child in raw.get("children", [])],
    )


def node_payload(node: TreeNode) -> dict[str, Any]:
    return {"node_code": node.code, "name_zh": node.name_zh, "name_en": node.name_en, "path": node.path, "semantic_card": clip(node.semantic_card, 700)}


def candidate_payload(nodes: list[TreeNode], max_examples: int) -> list[dict[str, Any]]:
    return [node_payload(node) | {"verified_examples": node.examples[:max_examples], "has_children": bool(node.children)} for node in nodes]


def call_api(args: argparse.Namespace, system: str, payload: dict[str, Any]) -> tuple[dict[str, Any], TokenUsage]:
    headers = {"Content-Type": "application/json"}
    if args.send_auth and args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    body = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": args.enable_thinking},
    }
    last_error = ""
    for attempt in range(args.retries + 1):
        try:
            request = urlrequest.Request(args.api_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
            with urlrequest.urlopen(request, timeout=args.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return extract_json_object(content), usage_from_api(result.get("usage", {}))
        except urlerror.HTTPError as exc:
            try:
                error_text = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception as read_exc:
                partial = getattr(read_exc, "partial", b"") or b""
                if isinstance(partial, str):
                    partial_text = partial
                else:
                    partial_text = partial.decode("utf-8", errors="replace")[:300]
                error_text = f"<failed_to_read_error_body: {type(read_exc).__name__}: {read_exc}; partial={partial_text!r}>"
            last_error = f"HTTP {exc.code}: {error_text}"
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"API request failed: {last_error}")


def routing_decision(raw: Any, *, is_root: bool) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    action = str(raw.get("action", "descend"))
    allowed = {"descend", "stop_at_current", "insufficient_evidence"}
    if is_root:
        allowed.add("out_of_scope")
    if action not in allowed:
        action = "insufficient_evidence" if is_root else "descend"
    if action == "stop_at_current" and is_root:
        action = "insufficient_evidence"
    return {"action": action, "confidence": number(raw.get("confidence")), "reason": clip(str(raw.get("reason", "")), 80)}


def threshold_for_level(level: int, current: TreeNode, args: argparse.Namespace) -> float:
    """Return the effective threshold for this expansion level.

    Default behavior is unchanged: root uses root_threshold and all non-root
    levels use threshold.  When --routing-threshold is set, L1-LN can be used
    as a loose routing stage while L4/L5 remain strict mount stages.
    """
    if args.routing_threshold is not None and level <= args.routing_max_depth:
        return args.routing_threshold
    is_root = level == 1 and current.code == args.root_code
    return args.root_threshold if is_root else args.threshold


def threshold_role_for_level(level: int, current: TreeNode, args: argparse.Namespace) -> str:
    if args.routing_threshold is not None and level <= args.routing_max_depth:
        return "routing"
    if level == 1 and current.code == args.root_code:
        return "root"
    return "mount"


def score_level(knowledge_card: dict[str, Any], current: TreeNode, level: int, args: argparse.Namespace, prompt: Any) -> tuple[list[dict[str, Any]], TokenUsage, list[dict[str, Any]], dict[str, Any]]:
    is_root = level == 1 and current.code == args.root_code
    effective_threshold = threshold_for_level(level, current, args)
    threshold_role = threshold_role_for_level(level, current, args)
    payload = prompt.build_level_user_payload(
        threshold=effective_threshold,
        threshold_role=threshold_role,
        root_threshold=args.root_threshold,
        routing_threshold=args.routing_threshold,
        routing_max_depth=args.routing_max_depth,
        mount_threshold=args.threshold,
        knowledge_card=knowledge_card,
        current_node=node_payload(current),
        is_root_level=is_root,
        candidates=candidate_payload(current.children, args.max_seed_examples),
    )
    payload["subject_name"] = args.subject_name
    payload["subject_scope"] = args.subject_scope
    parsed, usage = call_api(args, prompt.LEVEL_SYSTEM_PROMPT, payload)
    by_code = {node.code: node for node in current.children}
    valid: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for item in parsed.get("candidates", []):
        code = item.get("node_code")
        if code not in by_code:
            warnings.append({"type": "unknown_candidate", "level": level, "candidate": item})
            continue
        confidence = number(item.get("absolute_confidence"))
        probability = number(item.get("local_probability"), 1e-8)
        valid.append({"node": by_code[code], "absolute_confidence": confidence, "local_probability": max(probability, 1e-8), "evidence": item.get("evidence", []), "below_threshold": confidence < effective_threshold})
    missing = sorted(set(by_code) - {item["node"].code for item in valid})
    if missing:
        warnings.append({"type": "missing_candidates", "level": level, "count": len(missing)})
    decision = routing_decision(parsed.get("routing_decision"), is_root=is_root)
    if is_root:
        valid, type_warnings, forced_rule = constrain_root_candidates(knowledge_card, valid, by_code, effective_threshold)
        warnings.extend(type_warnings)
        if forced_rule:
            decision = {"action": "descend", "confidence": max(decision["confidence"], effective_threshold), "reason": f"root_type_constraint:{forced_rule}"}
    else:
        valid, type_warnings, forced_rule = constrain_subtree_candidates(knowledge_card, current.code, valid, by_code, effective_threshold)
        warnings.extend(type_warnings)
        if forced_rule:
            decision = {"action": "descend", "confidence": max(decision["confidence"], effective_threshold), "reason": f"subtree_type_constraint:{forced_rule}"}
    return sorted(valid, key=lambda item: (item["local_probability"], item["absolute_confidence"]), reverse=True), usage, warnings, decision


def serialise_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "node"} | {"node_code": item["node"].code, "node_name": item["node"].name_zh} for item in candidates]


BOUNDARY_NEGATIVE_REASON_TERMS = ['非核心',
 '不是核心',
 '并非',
 '不属于',
 '不直接',
 '不对应',
 '不匹配',
 '只是辅助',
 '仅为辅助',
 'not directly',
 'not the core',
 'not a core',
 'rather than']
BOUNDARY_NEGATIVE_REASON_PATTERN = re.compile("|".join(re.escape(term) for term in BOUNDARY_NEGATIVE_REASON_TERMS), re.IGNORECASE)


def forced_leaf_boundary_negative_evidence(best: Beam) -> str | None:
    """Detect model evidence that contradicts a forced 0805/P2 leaf boundary."""
    if not best.level_scores:
        return None
    last = best.level_scores[-1]
    rule = str(last.get("type_constraint") or "")
    if not (rule.startswith("p2_") or rule.startswith("taxonomy0805_")):
        return None
    evidence = last.get("evidence") or []
    if isinstance(evidence, list):
        evidence_text = " ".join(str(item) for item in evidence)
    else:
        evidence_text = str(evidence)
    if BOUNDARY_NEGATIVE_REASON_PATTERN.search(evidence_text):
        return evidence_text[:240]
    return None



def compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def join_unique_texts(*values: Any, sep: str = "\n") -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = compact_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return sep.join(parts)


def normalise_input_record(record: dict[str, Any]) -> dict[str, Any]:
    """Accept both wrapped mapping records and cleaned entity JSONL rows."""
    if isinstance(record.get("knowledge_card"), dict):
        return record
    knowledge_point = compact_text(record.get("knowledge_point") or record.get("foreign_name") or record.get("name_en") or record.get("english"))
    chinese_name = compact_text(record.get("name") or record.get("chinese_name") or record.get("name_zh") or record.get("chinese") or record.get("zh"))
    definition = compact_text(record.get("definition"))
    en_definition = compact_text(record.get("en_definition"))
    description = compact_text(record.get("description"))
    en_description = compact_text(record.get("en_description"))
    structured_evidence = join_unique_texts(definition, en_definition, description, en_description)
    legacy_evidence = join_unique_texts(record.get("cleaned_explanation"), record.get("explanation"), record.get("definition_or_evidence"), record.get("definition"), record.get("description"))
    evidence = structured_evidence or legacy_evidence
    if not knowledge_point and not chinese_name and not evidence:
        return record
    final_entity_id = record.get("final_entity_id") or record.get("entity_id") or record.get("id") or record.get("record_id")
    source_book_ids = record.get("source_book_ids") or record.get("book_id") or record.get("source_book") or ""
    source_type = record.get("source_type") or "heading_definition_final_usable"
    normalised = dict(record)
    normalised.setdefault("record_id", f"ENT{final_entity_id}" if final_entity_id not in (None, "") else compact_text(record.get("entity_key") or knowledge_point or chinese_name)[:80])
    normalised.setdefault("semantic_key", compact_text(record.get("entity_key") or "|".join(part for part in (knowledge_point, chinese_name) if part)))
    normalised.setdefault("source_row", dict(record))
    normalised["knowledge_card"] = {
        "knowledge_point": knowledge_point or chinese_name,
        "foreign_name": knowledge_point or chinese_name,
        "chinese_name": chinese_name or knowledge_point,
        "chinese_aliases": compact_text(record.get("aliases") or record.get("chinese_aliases")),
        "source_type": source_type,
        "source_book": compact_text(source_book_ids),
        "raw_heading": compact_text(record.get("raw_heading") or " ".join(part for part in (knowledge_point, chinese_name) if part)),
        "definition": definition,
        "en_definition": en_definition,
        "description": description,
        "en_description": en_description,
        "definition_or_evidence": evidence,
        "evidence_priority": "definition_first_description_auxiliary" if structured_evidence else "legacy_evidence",
        "original_entity_id": str(final_entity_id or ""),
        "confidence": record.get("confidence", ""),
        "status": record.get("status", ""),
        "final_quality_group": record.get("final_quality_group", ""),
        "final_quality_action": record.get("final_quality_action", ""),
        "final_quality_flags": record.get("final_quality_flags", []),
        "final_metadata_flags": record.get("final_metadata_flags", []),
        "final_cleanup_actions": record.get("final_cleanup_actions", []),
        "mounting_scope_decision": record.get("mounting_scope_decision", ""),
        "mounting_scope_flag": record.get("mounting_scope_flag", ""),
        "mounting_scope_reason": record.get("mounting_scope_reason", ""),
    }
    return normalised


def route_record(record: dict[str, Any], root: TreeNode, args: argparse.Namespace, prompt: Any) -> tuple[dict[str, Any], TokenUsage]:
    card = record.get("knowledge_card")
    if not isinstance(card, dict):
        raise ValueError("missing_knowledge_card")
    if is_malformed_abbreviation_entry(card):
        return {
            "status": "ok", "decision": "needs_review", "attachment_type": "none", "attachment_depth": 0,
            "threshold": args.threshold, "stop_threshold": args.stop_threshold, "scope_threshold": args.scope_threshold,
            "beam_width": args.beam_width, "best_path_codes": [], "best_path": [], "path_score": 0.0,
            "truncated": False, "stop_reason": "malformed_abbreviation_or_residual_label",
            "root_routing_decision": {"action": "insufficient_evidence", "confidence": 1.0, "reason": "malformed_abbreviation_or_residual_label"},
            "level_scores": [], "rejected_next_candidates": [], "top_paths": [], "token_usage": TokenUsage().as_dict(),
            "warnings": [{"type": "malformed_abbreviation_or_residual_label"}],
        }, TokenUsage()
    if is_organization_entry(card):
        return {
            "status": "ok", "decision": "out_of_scope", "attachment_type": "none", "attachment_depth": 0,
            "threshold": args.threshold, "stop_threshold": args.stop_threshold, "scope_threshold": args.scope_threshold,
            "beam_width": args.beam_width, "best_path_codes": [], "best_path": [], "path_score": 0.0,
            "truncated": False, "stop_reason": "organization_name_not_a_subject_knowledge_point",
            "root_routing_decision": {"action": "out_of_scope", "confidence": 1.0, "reason": f"组织或机构名称，不作为{args.subject_name}知识点挂载"},
            "level_scores": [], "rejected_next_candidates": [], "top_paths": [], "token_usage": TokenUsage().as_dict(),
            "warnings": [{"type": "organization_name_excluded"}],
        }, TokenUsage()
    if is_unresolved_multisense_dictionary_entry(card):
        return {
            "status": "ok", "decision": "needs_review", "attachment_type": "none", "attachment_depth": 0,
            "threshold": args.threshold, "stop_threshold": args.stop_threshold, "scope_threshold": args.scope_threshold,
            "beam_width": args.beam_width, "best_path_codes": [], "best_path": [], "path_score": 0.0,
            "truncated": False, "stop_reason": "ambiguous_dictionary_senses",
            "root_routing_decision": {"action": "insufficient_evidence", "confidence": 1.0, "reason": "multiple_unresolved_dictionary_senses"},
            "level_scores": [], "rejected_next_candidates": [], "top_paths": [], "token_usage": TokenUsage().as_dict(),
            "warnings": [{"type": "ambiguous_dictionary_senses"}],
        }, TokenUsage()
    local_exclusion = local_scope_exclusion(card)
    if local_exclusion:
        flag, local_decision, reason = local_exclusion
        return {
            "status": "ok", "decision": local_decision, "attachment_type": "none", "attachment_depth": 0,
            "threshold": args.threshold, "stop_threshold": args.stop_threshold, "scope_threshold": args.scope_threshold,
            "beam_width": args.beam_width, "best_path_codes": [], "best_path": [], "path_score": 0.0,
            "truncated": False, "stop_reason": flag,
            "root_routing_decision": {"action": local_decision, "confidence": 1.0, "reason": reason},
            "level_scores": [], "rejected_next_candidates": [], "top_paths": [], "token_usage": TokenUsage().as_dict(),
            "warnings": [{"type": flag, "source": "local_scope_exclusion"}],
        }, TokenUsage()
    beams = [Beam(node=root, path_codes=[], path_names=[], log_score=0.0, level_scores=[])]
    usage = TokenUsage()
    warnings: list[dict[str, Any]] = []
    root_decision: dict[str, Any] = {"action": "unknown", "confidence": 0.0, "reason": ""}
    level = 1
    while level <= args.max_depth and any(not beam.finished and beam.node and beam.node.children for beam in beams):
        expanded: list[Beam] = []
        for beam in beams:
            if beam.finished or not beam.node or not beam.node.children:
                expanded.append(beam)
                continue
            candidates, call_usage, local_warnings, decision = score_level(card, beam.node, level, args, prompt)
            usage.add(call_usage)
            warnings.extend(local_warnings)
            if level == 1 and not beam.path_codes:
                root_decision = decision
            if not beam.path_codes and decision["action"] == "out_of_scope":
                expanded.append(Beam(node=beam.node, path_codes=[], path_names=[], log_score=0.0, level_scores=[], finished=True, stop_reason=decision["reason"] or "out_of_scope"))
                continue
            if not beam.path_codes and decision["action"] == "insufficient_evidence" and decision["confidence"] >= args.scope_threshold:
                expanded.append(Beam(node=beam.node, path_codes=[], path_names=[], log_score=0.0, level_scores=[], finished=True, stop_reason=decision["reason"] or "insufficient_evidence"))
                continue
            if beam.path_codes and decision["action"] == "stop_at_current" and not (args.force_routing_through_depth and level <= args.force_routing_through_depth):
                expanded.append(Beam(node=beam.node, path_codes=beam.path_codes, path_names=beam.path_names, log_score=beam.log_score, level_scores=beam.level_scores, rejected_next_candidates=list(beam.rejected_next_candidates), finished=True, stopped_by_evidence=True, stop_reason=decision["reason"] or "evidence_supported_parent"))
                continue
            accepted = [item for item in candidates if not item["below_threshold"]][:args.beam_width]
            rejected = serialise_candidates([item for item in candidates if item["below_threshold"]])
            if not accepted:
                expanded.append(Beam(node=beam.node, path_codes=beam.path_codes, path_names=beam.path_names, log_score=beam.log_score, level_scores=beam.level_scores, rejected_next_candidates=beam.rejected_next_candidates + rejected, finished=True, truncated=True, stop_reason=decision["reason"] or "no_child_above_threshold"))
                continue
            for item in accepted:
                node = item["node"]
                score = {key: value for key, value in item.items() if key != "node"} | {"level": level, "node_code": node.code, "node_name": node.name_zh}
                expanded.append(Beam(node=node, path_codes=beam.path_codes + [node.code], path_names=beam.path_names + [node.name_zh], log_score=beam.log_score + math.log(item["local_probability"]), level_scores=beam.level_scores + [score], rejected_next_candidates=list(beam.rejected_next_candidates) + rejected, finished=not node.children))
        beams = sorted(expanded, key=lambda item: item.log_score, reverse=True)[:args.beam_width]
        level += 1
    best = max(beams, key=lambda item: item.path_score)
    if best.path_codes and best.stopped_by_evidence:
        attachment_type, decision = "parent", "accepted_parent" if len(best.path_codes) >= args.min_parent_depth else "needs_review"
        if decision == "needs_review":
            best.stop_reason = f"parent_depth_below_minimum_{args.min_parent_depth}"
    elif best.path_codes and not best.truncated and (not best.node or not best.node.children):
        if len(best.path_codes) >= args.min_mount_depth:
            attachment_type, decision = "leaf", "accepted_leaf"
        else:
            attachment_type, decision = "none", "needs_review"
            best.stop_reason = f"leaf_depth_below_min_mount_depth_{args.min_mount_depth}"
    elif best.path_codes and level > args.max_depth:
        attachment_type, decision = "parent", "accepted_parent" if len(best.path_codes) >= args.min_parent_depth else "needs_review"
        best.stop_reason = best.stop_reason or "max_depth_reached"
    elif best.path_codes:
        attachment_type, decision = "parent", "needs_review"
    elif root_decision["action"] == "out_of_scope":
        attachment_type, decision = "none", "out_of_scope"
    elif root_decision["action"] == "insufficient_evidence":
        attachment_type, decision = "none", "insufficient_evidence"
    else:
        attachment_type, decision = "none", "needs_review"
    if decision == "accepted_parent" and has_reason_path_conflict(best.stop_reason, best.path_names):
        decision = "needs_review"
        warnings.append({"type": "parent_reason_path_conflict", "reason": best.stop_reason, "path": best.path_codes})
    if decision == "accepted_leaf" and has_component_to_whole_path_conflict(card, best.path_names):
        # A record such as "lathe part" proves the broader equipment family,
        # not that the whole lathe is the most specific attached entity.
        best.path_codes = best.path_codes[:-1]
        best.path_names = best.path_names[:-1]
        best.level_scores = best.level_scores[:-1]
        attachment_type = "parent"
        if len(best.path_codes) >= args.min_parent_depth:
            decision = "accepted_parent"
            best.stop_reason = "component_term_stops_before_whole_equipment_leaf"
        else:
            decision = "needs_review"
            best.stop_reason = "component_term_path_below_min_parent_depth"
        warnings.append({"type": "component_to_whole_leaf_prevented", "path": best.path_codes})
    elif decision == "accepted_leaf" and has_tool_to_whole_path_conflict(card, best.path_names):
        # A tool, die or fixture is not evidence for a complete-machine leaf.
        cut_index = first_whole_equipment_path_index(best.path_names)
        if cut_index is not None:
            best.path_codes = best.path_codes[:cut_index]
            best.path_names = best.path_names[:cut_index]
            best.level_scores = best.level_scores[:cut_index]
        attachment_type = "parent"
        if len(best.path_codes) >= args.min_parent_depth:
            decision = "accepted_parent"
            best.stop_reason = "tool_term_stops_before_whole_equipment_branch"
        else:
            decision = "needs_review"
            best.stop_reason = "tool_term_path_below_min_parent_depth"
        warnings.append({"type": "tool_to_whole_leaf_prevented", "path": best.path_codes})
    if decision == "accepted_leaf":
        forced_boundary_negative = forced_leaf_boundary_negative_evidence(best)
        if forced_boundary_negative:
            decision = "needs_review"
            attachment_type = "none"
            best.stop_reason = "forced_leaf_boundary_negative_model_evidence"
            warnings.append({"type": "forced_leaf_boundary_negative_model_evidence", "reason": forced_boundary_negative, "path": best.path_codes})
    if decision in {"accepted_leaf", "accepted_parent"}:
        parent_fallback = safe_parent_fallback(card, best.path_names)
        if parent_fallback:
            fallback_depth, warning_type, reason = parent_fallback
            best.path_codes = best.path_codes[:fallback_depth]
            best.path_names = best.path_names[:fallback_depth]
            best.level_scores = best.level_scores[:fallback_depth]
            attachment_type = "parent" if best.path_codes else "none"
            if len(best.path_codes) >= args.min_parent_depth:
                decision = "accepted_parent"
            else:
                decision = "needs_review"
            best.stop_reason = warning_type
            warnings.append({"type": warning_type, "reason": reason, "path": best.path_codes})
    if decision in {"accepted_leaf", "accepted_parent"}:
        path_conflict = has_post_route_path_conflict(card, best.path_names)
        if path_conflict:
            warning_type, reason = path_conflict
            decision = "needs_review"
            best.stop_reason = warning_type
            warnings.append({"type": warning_type, "reason": reason, "path": best.path_codes})
    return {
        "status": "ok",
        "decision": decision,
        "attachment_type": attachment_type,
        "attachment_depth": len(best.path_codes),
        "threshold": args.threshold,
        "stop_threshold": args.stop_threshold,
        "scope_threshold": args.scope_threshold,
        "beam_width": args.beam_width,
        "best_path_codes": best.path_codes,
        "best_path": best.path_names,
        "path_score": round(best.path_score, 6),
        "truncated": best.truncated,
        "stop_reason": best.stop_reason,
        "root_routing_decision": root_decision,
        "level_scores": best.level_scores,
        "rejected_next_candidates": best.rejected_next_candidates,
        "top_paths": [{"path_codes": beam.path_codes, "path": beam.path_names, "path_score": round(beam.path_score, 6), "truncated": beam.truncated, "stopped_by_evidence": beam.stopped_by_evidence} for beam in sorted(beams, key=lambda item: item.path_score, reverse=True)],
        "token_usage": usage.as_dict(),
        "warnings": warnings,
    }, usage


def source_type(record: dict[str, Any]) -> str:
    return record.get("knowledge_card", {}).get("source_type") or "unknown"


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, dict[str, Any]]] = {"by_source_type": {}, "by_top_level_node": {}, "by_decision": {}}
    for record in records:
        labeling = record.get("knowledge_labeling", {})
        usage = TokenUsage(**{key: integer(labeling.get("token_usage", {}).get(key, 0)) for key in TokenUsage.__dataclass_fields__})
        keys = {
            "by_source_type": source_type(record),
            "by_top_level_node": (labeling.get("best_path_codes") or ["unrouted"])[0],
            "by_decision": labeling.get("decision", "failed"),
        }
        for group_name, key in keys.items():
            bucket = groups[group_name].setdefault(key, {"records": 0, "token_usage": TokenUsage(), "decisions": Counter()})
            bucket["records"] += 1
            bucket["token_usage"].add(usage)
            bucket["decisions"][labeling.get("decision", "failed")] += 1
    for group in groups.values():
        for bucket in group.values():
            bucket["token_usage"] = bucket["token_usage"].as_dict()
            bucket["decisions"] = dict(bucket["decisions"])
    return groups


def is_retryable_api_error(error: str) -> bool:
    lowered = error.lower()
    if "http 4" in lowered and "http 429" not in lowered:
        return False
    return any(marker in lowered for marker in RETRYABLE_ERROR_MARKERS)


def run_api_preflight(records: list[dict[str, Any]], root: TreeNode, args: argparse.Namespace, prompt: Any) -> TokenUsage:
    if args.skip_preflight or not records:
        return TokenUsage()
    for record in records:
        card = record.get("knowledge_card")
        if not isinstance(card, dict):
            continue
        if is_malformed_abbreviation_entry(card) or is_organization_entry(card) or is_unresolved_multisense_dictionary_entry(card):
            continue
        print("API mapping v3: preflight; one root-level request", flush=True)
        _, usage, _, _ = score_level(card, root, 1, args, prompt)
        print(f"API mapping v3: preflight ok; api_calls={usage.api_calls}; total_tokens={usage.total_tokens}", flush=True)
        return usage
    print("API mapping v3: preflight skipped; no API-backed records", flush=True)
    return TokenUsage()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR, help="Subject profile directory containing profile.json.")
    parser.add_argument("--subject-name", default="", help="Override subject name from profile.json.")
    parser.add_argument("--subject-scope", default="", help="Override subject scope description from profile.json.")
    parser.add_argument("--knowledge-tree", type=Path, default=None, help="Override profile knowledge-tree JSON.")
    parser.add_argument("--semantic-cards", type=Path, default=None, help="Override profile semantic-card JSONL.")
    parser.add_argument("--prompt-module", type=Path, default=None, help="Override profile prompt Python module.")
    parser.add_argument("--routing-constraints", type=Path, default=None, help="Override profile routing-constraint Python module.")
    parser.add_argument("--api-url", default=os.environ.get("KNOWLEDGE_LABELING_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-key", default=os.environ.get("KNOWLEDGE_LABELING_API_KEY", ""))
    parser.add_argument("--send-auth", action="store_true", help="Send Authorization: Bearer header; off by default for the current Qwen3.6 service.")
    parser.add_argument("--model", default=os.environ.get("KNOWLEDGE_LABELING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--beam-width", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--root-threshold", type=float, default=0.6)
    parser.add_argument("--routing-threshold", type=float, default=None, help="Optional loose threshold for L1-LN routing; unset keeps legacy v3 behavior.")
    parser.add_argument("--routing-max-depth", type=int, default=3, help="Highest candidate depth that uses --routing-threshold when set.")
    parser.add_argument("--force-routing-through-depth", type=int, default=0, help="Ignore stop_at_current before/equal this depth when rescuing needs_review records.")
    parser.add_argument("--stop-threshold", type=float, default=0.8)
    parser.add_argument("--scope-threshold", type=float, default=0.8)
    parser.add_argument("--min-parent-depth", type=int, default=2)
    parser.add_argument("--min-mount-depth", type=int, default=1, help="Minimum path depth allowed for any accepted mount; use 4 for strict L4/L5 rescue.")
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-failed-rounds", type=int, default=2, help="Retry transient API failures after the main batch.")
    parser.add_argument("--retry-failed-workers", type=int, default=32, help="Workers used only for failed-record retry rounds.")
    parser.add_argument("--retry-failed-cooldown", type=int, default=10, help="Seconds to wait before each failed-record retry round.")
    parser.add_argument("--api-failure-stop-window", type=int, default=0, help="Stop the current phase when this many recent records are mostly retryable API failures. Set 0 to disable.")
    parser.add_argument("--api-failure-stop-rate", type=float, default=0.85, help="Recent retryable API failure rate that triggers early stop.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the single root-level API check before submitting the batch.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--enable-thinking", action="store_true", help="Enable Qwen thinking mode; disabled by default for stable JSON.")
    parser.add_argument("--max-seed-examples", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-progress", action="store_true", help="Disable tqdm progress bar and use periodic text logs.")
    parser.add_argument("--progress-interval", type=int, default=10, help="Fallback text-log interval when tqdm is unavailable or disabled.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    resolve_profile(args)
    threshold_values = (args.root_threshold, args.threshold, args.stop_threshold, args.scope_threshold, args.api_failure_stop_rate)
    routing_threshold_ok = args.routing_threshold is None or 0 <= args.routing_threshold <= 1
    if (
        args.beam_width < 1
        or args.min_parent_depth < 1
        or args.min_mount_depth < 1
        or args.routing_max_depth < 1
        or args.force_routing_through_depth < 0
        or args.retry_failed_rounds < 0
        or args.retry_failed_workers < 1
        or args.retry_failed_cooldown < 0
        or args.api_failure_stop_window < 0
        or args.progress_interval < 1
        or not all(0 < value <= 1 for value in threshold_values)
        or not routing_threshold_ok
    ):
        raise ValueError("Invalid routing, retry, or progress parameter.")
    prompt = load_prompt_module(args.prompt_module)
    install_constraint_module(args.routing_constraints)
    cards = load_cards(args.semantic_cards)
    root = build_tree(json.loads(args.knowledge_tree.read_text(encoding="utf-8")), cards)
    args.root_code = root.code
    records = [normalise_input_record(json.loads(line)) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        records = records[:args.limit]
    try:
        preflight_usage = run_api_preflight(records, root, args, prompt)
    except Exception as exc:
        raise RuntimeError(f"API preflight failed before batch submission: {exc}") from exc
    start = time.perf_counter()
    results: list[dict[str, Any] | None] = [None] * len(records)
    total_usage = TokenUsage()
    retry_summary: dict[str, Any] = {"initial_retryable_failures": 0, "rounds": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def is_retryable(index: int) -> bool:
        item = results[index]
        if not item or item.get("knowledge_labeling", {}).get("status") != "failed":
            return False
        error = str(item["knowledge_labeling"].get("error", ""))
        return is_retryable_api_error(error)

    def run_batch(indexes: list[int], workers: int, phase: str, handle: Any) -> list[int]:
        if not indexes:
            return []
        phase_failed = 0
        recent_retryable_failures: deque[int] = deque(maxlen=args.api_failure_stop_window or 1)
        print(f"API mapping v3: {phase}; records={len(indexes)}; workers={workers}", flush=True)
        progress = None
        if not args.no_progress:
            if tqdm is not None:
                progress = tqdm(total=len(indexes), desc=f"API mapping v3 {phase}", unit="rec", dynamic_ncols=True)
            else:
                progress = SimpleProgress(total=len(indexes), desc=f"API mapping v3 {phase}", unit="rec")
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(route_record, records[index], root, args, prompt): index for index in indexes}
                for completed, future in enumerate(as_completed(futures), 1):
                    index = futures[future]
                    record = dict(records[index])
                    try:
                        result, usage = future.result()
                        record["knowledge_labeling"] = result
                        total_usage.add(usage)
                    except Exception as exc:
                        record["knowledge_labeling"] = {"status": "failed", "decision": "needs_review", "error": str(exc), "token_usage": TokenUsage().as_dict()}
                    if record.get("knowledge_labeling", {}).get("status") == "failed":
                        phase_failed += 1
                    error_text = str(record.get("knowledge_labeling", {}).get("error", ""))
                    is_retryable_failure = bool(record.get("knowledge_labeling", {}).get("status") == "failed" and is_retryable_api_error(error_text))
                    if args.api_failure_stop_window:
                        recent_retryable_failures.append(1 if is_retryable_failure else 0)
                    results[index] = record
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    if progress is not None:
                        progress.set_postfix(failed=phase_failed)
                        progress.update(1)
                    elif completed % args.progress_interval == 0 or completed == len(futures):
                        print(f"API mapping v3: {phase}; {completed}/{len(indexes)} completed; failed={phase_failed}", flush=True)
                    if args.api_failure_stop_window and len(recent_retryable_failures) == args.api_failure_stop_window:
                        recent_failure_rate = sum(recent_retryable_failures) / args.api_failure_stop_window
                        if recent_failure_rate >= args.api_failure_stop_rate:
                            for pending in futures:
                                pending.cancel()
                            raise RuntimeError(
                                f"API failure circuit breaker triggered in {phase}: "
                                f"recent retryable API failure rate {recent_failure_rate:.1%} "
                                f"over {args.api_failure_stop_window} records. Stop now and rerun later or lower --workers."
                            )
        finally:
            if progress is not None:
                progress.close()
        return [index for index in indexes if is_retryable(index)]

    with args.output.open("w", encoding="utf-8") as handle:
        retryable_indexes = run_batch(list(range(len(records))), args.workers, "main", handle)
        retry_summary["initial_retryable_failures"] = len(retryable_indexes)
        for round_number in range(1, args.retry_failed_rounds + 1):
            if not retryable_indexes:
                break
            print(f"API mapping v3: retry round {round_number}; waiting {args.retry_failed_cooldown}s before retrying {len(retryable_indexes)} transient failures", flush=True)
            time.sleep(args.retry_failed_cooldown)
            attempted = len(retryable_indexes)
            retryable_indexes = run_batch(retryable_indexes, args.retry_failed_workers, f"retry_{round_number}", handle)
            retry_summary["rounds"].append({"round": round_number, "attempted": attempted, "remaining_retryable_failures": len(retryable_indexes)})
    failed = sum(1 for item in results if item and item.get("knowledge_labeling", {}).get("status") == "failed")
    ordered_results = [item for item in results if item is not None]
    with args.output.open("w", encoding="utf-8") as handle:
        for item in ordered_results:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    report = {
        "strategy_version": "configurable_subject_parent_or_leaf_v3_relaxed_routing" if args.routing_threshold is not None else "configurable_subject_parent_or_leaf_v3",
        "subject_profile": {
            "subject_name": args.subject_name,
            "subject_scope": args.subject_scope,
            "profile_dir": str(args.profile_dir),
            "knowledge_tree": str(args.knowledge_tree),
            "semantic_cards": str(args.semantic_cards),
            "prompt_module": str(args.prompt_module),
            "routing_constraints": str(args.routing_constraints),
            "root_code": args.root_code,
        },
        "input_records": len(records),
        "output": str(args.output),
        "elapsed_seconds": round(time.perf_counter() - start, 3),
        "preflight_token_usage": preflight_usage.as_dict(),
        "token_usage": total_usage.as_dict(),
        "failed_records": failed,
        "retry_failed": retry_summary,
        "parameters": {"beam_width": args.beam_width, "root_threshold": args.root_threshold, "routing_threshold": args.routing_threshold, "routing_max_depth": args.routing_max_depth, "force_routing_through_depth": args.force_routing_through_depth, "threshold": args.threshold, "stop_threshold": args.stop_threshold, "scope_threshold": args.scope_threshold, "min_parent_depth": args.min_parent_depth, "min_mount_depth": args.min_mount_depth, "workers": args.workers, "retry_failed_rounds": args.retry_failed_rounds, "retry_failed_workers": args.retry_failed_workers, "retry_failed_cooldown": args.retry_failed_cooldown, "model": args.model},
        "aggregates": aggregate(ordered_results),
    }
    args.output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
