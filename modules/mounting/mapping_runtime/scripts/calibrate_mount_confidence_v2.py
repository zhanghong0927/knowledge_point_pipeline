from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ACCEPTED = {"accepted_leaf", "accepted_parent"}
NOT_ACCEPTED_DECISIONS = {"needs_review", "out_of_scope", "insufficient_evidence", "failed", "error"}
HARD_CLEAN_DECISIONS = {"drop", "needs_review_drop"}
RISK_CLEAN_DECISIONS = {"recall_with_risk", "needs_review", "need_review"}
HARD_WARNING_KEYWORDS = (
    "out_of_scope",
    "organization",
    "product",
    "model_number",
    "malformed",
    "residual",
    "non_mechanical",
    "out_of_subject_scope",
    "pure_it",
    "network",
    "database",
)
PATH_CONFLICT_KEYWORDS = (
    "path_conflict",
    "reason_path_conflict",
    "component_to_whole",
    "tool_to_whole",
    "process_route_path",
)
NEUTRAL_WARNING_TYPES = {
    "root_type_forced",
    "subtree_type_forced",
}


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def clean_decision_of(row: dict[str, Any]) -> str:
    card = row.get("knowledge_card") if isinstance(row.get("knowledge_card"), dict) else {}
    return str(card.get("clean_decision") or row.get("clean_decision") or "").strip()


def source_type_of(row: dict[str, Any]) -> str:
    card = row.get("knowledge_card") if isinstance(row.get("knowledge_card"), dict) else {}
    return str(card.get("source_type") or row.get("source_type") or "").strip()


def warning_types(kl: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for w in kl.get("warnings") or []:
        if isinstance(w, dict):
            out.append(str(w.get("type") or ""))
        else:
            out.append(str(w))
    return [x for x in out if x]


def confidences_by_level(kl: dict[str, Any]) -> list[tuple[int, float]]:
    vals: list[tuple[int, float]] = []
    for item in kl.get("level_scores") or []:
        if not isinstance(item, dict):
            continue
        level = int(as_float(item.get("level"), 0))
        conf = clamp(as_float(item.get("absolute_confidence"), as_float(item.get("local_probability"), 0.0)))
        if level > 0:
            vals.append((level, conf))
    vals.sort(key=lambda x: x[0])
    return vals


def average(vals: list[float], default: float = 0.0) -> float:
    return sum(vals) / len(vals) if vals else default


def normalized_path_score(kl: dict[str, Any], depth: int) -> float:
    score = clamp(as_float(kl.get("path_score"), 0.0))
    if depth <= 0:
        return score
    return clamp(score ** (1.0 / depth))


def path_margin(kl: dict[str, Any]) -> float | None:
    tops = kl.get("top_paths") or []
    if not isinstance(tops, list) or len(tops) < 2:
        return None
    top_scores = [as_float(x.get("path_score"), 0.0) for x in tops[:2] if isinstance(x, dict)]
    if len(top_scores) < 2:
        return None
    return top_scores[0] - top_scores[1]


def risk_penalty_and_flags(row: dict[str, Any], kl: dict[str, Any], norm_score: float, depth: int) -> tuple[float, list[str], bool]:
    penalty = 0.0
    flags: list[str] = []
    hard = False
    clean_decision = clean_decision_of(row)
    decision = str(kl.get("decision") or "")
    warnings = warning_types(kl)
    stop_reason = str(kl.get("stop_reason") or "")

    if clean_decision == "recall_with_risk":
        penalty += 0.06
        flags.append("input_recall_with_risk")
    elif clean_decision in {"needs_review", "need_review"}:
        penalty += 0.14
        flags.append("input_needs_review")
    elif clean_decision in HARD_CLEAN_DECISIONS:
        penalty += 0.30
        flags.append(f"input_{clean_decision}")
        hard = True

    risk_warnings = [w for w in warnings if w not in NEUTRAL_WARNING_TYPES]
    neutral_warnings = [w for w in warnings if w in NEUTRAL_WARNING_TYPES]
    if risk_warnings:
        penalty += min(0.12, 0.04 * len(risk_warnings))
        flags.extend([f"warning:{w}" for w in risk_warnings])
    if neutral_warnings:
        flags.extend([f"neutral_warning:{w}" for w in neutral_warnings])

    warning_text = " ".join(risk_warnings + [stop_reason]).lower()
    if any(k in warning_text for k in PATH_CONFLICT_KEYWORDS):
        penalty += 0.10
        flags.append("path_conflict_or_boundary_warning")
    if any(k in warning_text for k in HARD_WARNING_KEYWORDS):
        penalty += 0.20
        flags.append("hard_warning")
        hard = True

    path_score = as_float(kl.get("path_score"), 0.0)
    if path_score and path_score < 0.72:
        penalty += 0.08
        flags.append("low_raw_path_score")
    if norm_score and norm_score < 0.88:
        penalty += 0.06
        flags.append("low_normalized_path_score")
    if decision == "accepted_parent" and depth <= 2:
        penalty += 0.08
        flags.append("shallow_parent_mount")

    margin = path_margin(kl)
    if margin is not None:
        if margin < 0.03:
            penalty += 0.10
            flags.append("low_top_path_margin")
        elif margin < 0.08:
            penalty += 0.05
            flags.append("medium_top_path_margin")

    return min(penalty, 0.60), flags, hard


def calibrate(row: dict[str, Any]) -> dict[str, Any]:
    kl = row.get("knowledge_labeling") if isinstance(row.get("knowledge_labeling"), dict) else {}
    decision = str(kl.get("decision") or "")
    depth = int(as_float(kl.get("attachment_depth"), len(kl.get("best_path") or []) if isinstance(kl.get("best_path"), list) else 0))
    norm_score = normalized_path_score(kl, depth)
    clean_decision = clean_decision_of(row)
    warnings = warning_types(kl)

    if decision not in ACCEPTED:
        return {
            "direction_confidence": 0.0,
            "specificity_confidence": 0.0,
            "normalized_path_score": norm_score,
            "overall_mount_confidence": 0.0,
            "confidence_tier": "not_accepted",
            "specificity_tier": "none",
            "risk_penalty": 0.0,
            "risk_flags": [f"decision:{decision or 'missing'}"],
            "calibration_version": "mount_confidence_v2_parent_directional",
        }

    level_vals = confidences_by_level(kl)
    conf_by_level = {level: conf for level, conf in level_vals}
    direction_levels = [conf_by_level[l] for l in (1, 2, 3) if l in conf_by_level]
    if not direction_levels:
        direction_levels = [norm_score]
    direction_conf = clamp(0.6 * min(direction_levels) + 0.4 * average(direction_levels, norm_score))

    specificity_levels = [conf for level, conf in level_vals if level >= 4]
    if specificity_levels:
        specificity_conf = clamp(0.5 * average(specificity_levels) + 0.5 * specificity_levels[-1])
    else:
        specificity_conf = 0.55

    penalty, flags, hard = risk_penalty_and_flags(row, kl, norm_score, depth)
    if decision == "accepted_parent":
        # Parent mounts are judged primarily by route direction. Low specificity
        # means "not deep enough", not necessarily "low confidence".
        overall = clamp(0.75 * direction_conf + 0.15 * norm_score + 0.10 * specificity_conf - penalty)
    else:
        overall = clamp(0.55 * direction_conf + 0.35 * specificity_conf + 0.10 * norm_score - penalty)

    if decision == "accepted_leaf" and depth >= 4:
        specificity_tier = "leaf"
    elif decision == "accepted_leaf":
        specificity_tier = "leaf_shallow"
    elif decision == "accepted_parent" and depth >= 3:
        specificity_tier = "parent_directional"
    else:
        specificity_tier = "parent_shallow"

    max_tier = "high_confidence"
    if clean_decision in {"needs_review", "need_review"}:
        max_tier = "medium_confidence"
    if clean_decision == "recall_with_risk" and decision == "accepted_parent":
        max_tier = "medium_confidence"
    if warnings and clean_decision in RISK_CLEAN_DECISIONS:
        max_tier = "low_confidence"

    risk_warnings_for_tier = [w for w in warnings if w not in NEUTRAL_WARNING_TYPES]
    if hard:
        tier = "not_accepted"
    elif decision == "accepted_parent" and overall >= 0.82 and direction_conf >= 0.86 and not risk_warnings_for_tier:
        tier = "high_confidence"
    elif decision == "accepted_leaf" and overall >= 0.82 and direction_conf >= 0.85 and not risk_warnings_for_tier:
        tier = "high_confidence"
    elif overall >= 0.70 and direction_conf >= 0.78:
        tier = "medium_confidence"
    else:
        tier = "low_confidence"

    if max_tier == "medium_confidence" and tier == "high_confidence":
        tier = "medium_confidence"
        flags.append("tier_capped_by_input_or_parent_risk")
    elif max_tier == "low_confidence" and tier in {"high_confidence", "medium_confidence"}:
        tier = "low_confidence"
        flags.append("tier_capped_by_combined_risk")

    return {
        "direction_confidence": round(direction_conf, 6),
        "specificity_confidence": round(specificity_conf, 6),
        "normalized_path_score": round(norm_score, 6),
        "overall_mount_confidence": round(overall, 6),
        "confidence_tier": tier,
        "specificity_tier": specificity_tier,
        "risk_penalty": round(penalty, 6),
        "risk_flags": flags,
        "path_margin": path_margin(kl),
        "calibration_version": "mount_confidence_v2_parent_directional",
    }


def open_split_files(out_dir: Path):
    return {
        "high_confidence_leaf": (out_dir / "high_confidence_leaf.jsonl").open("w", encoding="utf-8"),
        "high_confidence_parent": (out_dir / "high_confidence_parent.jsonl").open("w", encoding="utf-8"),
        "medium_confidence_mount": (out_dir / "medium_confidence_mount.jsonl").open("w", encoding="utf-8"),
        "low_confidence_mount": (out_dir / "low_confidence_mount.jsonl").open("w", encoding="utf-8"),
        "not_accepted": (out_dir / "not_accepted.jsonl").open("w", encoding="utf-8"),
    }


def split_key(row: dict[str, Any]) -> str:
    c = row.get("confidence_calibration") or {}
    tier = c.get("confidence_tier")
    kl = row.get("knowledge_labeling") or {}
    decision = kl.get("decision")
    if tier == "high_confidence" and decision == "accepted_leaf":
        return "high_confidence_leaf"
    if tier == "high_confidence" and decision == "accepted_parent":
        return "high_confidence_parent"
    if tier == "medium_confidence":
        return "medium_confidence_mount"
    if tier == "low_confidence":
        return "low_confidence_mount"
    return "not_accepted"


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate hierarchical subject-tree mounting confidence after API routing. V2 gives accepted_parent a direction-oriented score and treats type-forced warnings as neutral.")
    parser.add_argument("--input", type=Path, required=True, help="Mounted JSONL output from hierarchical-knowledge-labeling-beam-v3.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None, help="Calibrated full JSONL output. Defaults to output-dir/calibrated_all.jsonl")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output or args.output_dir / "calibrated_all.jsonl"
    split_files = open_split_files(args.output_dir)

    counts = Counter()
    by_decision = Counter()
    by_clean = Counter()
    by_clean_tier = Counter()
    by_specificity = Counter()
    risk_flags = Counter()
    score_by_tier: dict[str, list[float]] = defaultdict(list)
    total = 0
    parse_errors = 0

    try:
        with args.input.open("r", encoding="utf-8") as f, output.open("w", encoding="utf-8") as fo:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    parse_errors += 1
                    continue
                total += 1
                cal = calibrate(row)
                row["confidence_calibration"] = cal
                tier = cal["confidence_tier"]
                key = split_key(row)
                counts[key] += 1
                by_decision[(row.get("knowledge_labeling") or {}).get("decision", "")] += 1
                cd = clean_decision_of(row)
                by_clean[cd] += 1
                by_clean_tier[(cd, tier)] += 1
                by_specificity[cal["specificity_tier"]] += 1
                for flag in cal.get("risk_flags") or []:
                    risk_flags[flag] += 1
                score_by_tier[tier].append(float(cal.get("overall_mount_confidence") or 0.0))
                encoded = json.dumps(row, ensure_ascii=False) + "\n"
                fo.write(encoded)
                split_files[key].write(encoded)
    finally:
        for fp in split_files.values():
            fp.close()

    def qstats(vals: list[float]) -> dict[str, float | int] | None:
        if not vals:
            return None
        vals = sorted(vals)
        def q(x: float) -> float:
            i = (len(vals) - 1) * x
            lo = math.floor(i); hi = math.ceil(i)
            if lo == hi:
                return vals[lo]
            return vals[lo] * (hi - i) + vals[hi] * (i - lo)
        return {"n": len(vals), "min": round(vals[0], 6), "p25": round(q(0.25), 6), "p50": round(q(0.5), 6), "p75": round(q(0.75), 6), "max": round(vals[-1], 6)}

    report = {
        "input": str(args.input),
        "output": str(output),
        "output_dir": str(args.output_dir),
        "records": total,
        "parse_errors": parse_errors,
        "split_counts": dict(counts),
        "decision_counts": dict(by_decision),
        "clean_decision_counts": dict(by_clean),
        "clean_decision_by_tier": {f"{k[0]}|{k[1]}": v for k, v in by_clean_tier.items()},
        "specificity_tier_counts": dict(by_specificity),
        "risk_flags_top": risk_flags.most_common(50),
        "overall_score_by_tier": {k: qstats(v) for k, v in score_by_tier.items()},
        "files": {k: str(args.output_dir / f"{k}.jsonl") for k in split_files},
    }
    report_path = args.output_dir / "confidence_calibration_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

