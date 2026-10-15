#!/usr/bin/env bash
# 16-card accounting reference; not a measured optimal concurrency.
# Default plan prints capacities without starting a gateway or model request.
set -euo pipefail
action="${1:-plan}"
case "$action" in
  plan)
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    python3 "$script_dir/../staged_benchmark.py" plan \
      --gpu-cards 16 --target-per-track 30 \
      --limit 512 --front-limit 512 --extraction-limit 512 --model-limit 512
    ;;
  run)
    : "${RUN:?Set RUN to a new frozen run directory with code, source proofs and service preflight}"
    python3 "$RUN/code/staged_benchmark.py" run --root "$RUN" \
      --dictionary "$RUN/inputs/dictionary_candidates_verified.json" \
      --important "$RUN/inputs/important_candidates_verified.json" \
      --gpu-cards 16 --target-per-track 30 \
      --limit 512 --front-limit 512 --extraction-limit 512 --model-limit 512
    ;;
  *)
    printf '%s\n' 'Usage: staged_16card.sh [plan|run]' >&2
    exit 2
    ;;
esac
