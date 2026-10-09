#!/usr/bin/env python3
"""Unified launcher for the two-stage books/textbooks cleaning pipeline."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RULE_SCRIPT = ROOT / "scripts" / "rule_clean.py"
MODEL_SCRIPT = ROOT / "scripts" / "model_clean.py"


def run_command(command: list[str], dry_run: bool) -> None:
    print("+ " + shlex.join(command), flush=True)
    if not dry_run:
        subprocess.run(command, check=True)


def all_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rule cleanup followed by model cleanup")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--subject-description", default="")
    parser.add_argument("--taxonomy", type=Path)
    parser.add_argument("--taxonomy-chars", type=int, default=12000)
    parser.add_argument("--taxonomy-depth", type=int, default=3)
    parser.add_argument("--taxonomy-scope-chars", type=int, default=240)
    parser.add_argument("--taxonomy-candidate-nodes", type=int, default=3)
    parser.add_argument("--api-url", default=os.getenv("KNOWLEDGE_CLEAN_API_URL", ""))
    parser.add_argument("--model", default=os.getenv("KNOWLEDGE_CLEAN_MODEL", ""))
    parser.add_argument("--api-key", default=os.getenv("KNOWLEDGE_CLEAN_API_KEY", ""))
    parser.add_argument("--with-auth", action="store_true")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--single-retries", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--response-format",
        choices=("auto", "on", "off", "schema"),
        default="auto",
    )
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--context-chars", type=int, default=3000)
    parser.add_argument("--confidence-threshold", type=float, default=0.80)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--keep-single-letter", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--retry-decisions", default="")
    parser.add_argument("--strict-checkpoint", action="store_true")
    parser.add_argument("--fix-swapped-languages", action="store_true")
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def model_command(args: argparse.Namespace, rule_pass: Path, model_dir: Path) -> list[str]:
    command = [
        sys.executable,
        str(MODEL_SCRIPT),
        "--input",
        str(rule_pass),
        "--output-dir",
        str(model_dir),
        "--subject",
        args.subject,
        "--workers",
        str(args.workers),
        "--batch-size",
        str(args.batch_size),
        "--max-tokens",
        str(args.max_tokens),
        "--context-chars",
        str(args.context_chars),
        "--confidence-threshold",
        str(args.confidence_threshold),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
        "--single-retries",
        str(args.single_retries),
        "--temperature",
        str(args.temperature),
        "--response-format",
        args.response_format,
        "--taxonomy-chars",
        str(args.taxonomy_chars),
        "--taxonomy-depth",
        str(args.taxonomy_depth),
        "--taxonomy-scope-chars",
        str(args.taxonomy_scope_chars),
        "--taxonomy-candidate-nodes",
        str(args.taxonomy_candidate_nodes),
    ]
    if args.subject_description:
        command += ["--subject-description", args.subject_description]
    if args.taxonomy:
        command += ["--taxonomy", str(args.taxonomy)]
    if args.api_url:
        command += ["--api-url", args.api_url]
    if args.model:
        command += ["--model", args.model]
    if args.api_key:
        command += ["--api-key", args.api_key]
    if args.with_auth:
        command.append("--with-auth")
    if args.limit:
        command += ["--limit", str(args.limit)]
    if args.skip:
        command += ["--skip", str(args.skip)]
    if args.retry_errors:
        command.append("--retry-errors")
    if args.retry_decisions:
        command += ["--retry-decisions", args.retry_decisions]
    if args.strict_checkpoint:
        command.append("--strict-checkpoint")
    if args.fix_swapped_languages:
        command.append("--fix-swapped-languages")
    if args.enable_thinking:
        command.append("--enable-thinking")
    if args.finalize_only:
        command.append("--finalize-only")
    if args.resume:
        command.append("--resume")
    if args.overwrite and not args.resume:
        command.append("--overwrite")
    return command


def run_all(argv: list[str]) -> int:
    args = all_parser().parse_args(argv)
    rule_dir = args.output_dir / "01_rule_clean"
    model_dir = args.output_dir / "02_model_clean"
    rule_pass = rule_dir / "rule_pass.jsonl"
    if args.finalize_only:
        print("[finalize-only] reuse the existing checkpoint; rule stage skipped", flush=True)
    elif not args.resume or not rule_pass.is_file():
        command = [
            sys.executable,
            str(RULE_SCRIPT),
            "--input",
            str(args.input),
            "--output-dir",
            str(rule_dir),
        ]
        if args.limit:
            command += ["--max-records", str(args.limit)]
        if args.keep_single_letter:
            command.append("--keep-single-letter")
        if args.overwrite:
            command.append("--overwrite")
        run_command(command, args.dry_run)
    else:
        print(f"[resume] reuse {rule_pass}", flush=True)

    run_command(model_command(args, rule_pass, model_dir), args.dry_run)
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(
            "Usage:\n"
            "  python run.py rule [rule_clean.py arguments]\n"
            "  python run.py model [model_clean.py arguments]\n"
            "  python run.py all --input ... --output-dir ... --subject ... --api-url ... --model ..."
        )
        return 0
    stage = sys.argv[1]
    argv = sys.argv[2:]
    if stage == "rule":
        return subprocess.call([sys.executable, str(RULE_SCRIPT), *argv])
    if stage == "model":
        return subprocess.call([sys.executable, str(MODEL_SCRIPT), *argv])
    if stage == "all":
        return run_all(argv)
    raise SystemExit(f"Unknown stage: {stage}")


if __name__ == "__main__":
    raise SystemExit(main())
