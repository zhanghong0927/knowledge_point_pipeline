#!/usr/bin/env bash
set -euo pipefail

subject_key=""
dictionary_csv=""
important_csv=""
metadata_results_csv=""
output_dir=""
subject_config=""
api_base=""
credentials_file=""
workers=50

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject-key) subject_key="$2"; shift 2 ;;
    --dictionary-csv) dictionary_csv="$2"; shift 2 ;;
    --important-csv) important_csv="$2"; shift 2 ;;
    --metadata-results-csv) metadata_results_csv="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --subject-config) subject_config="$2"; shift 2 ;;
    --api-base) api_base="$2"; shift 2 ;;
    --credentials-file) credentials_file="$2"; shift 2 ;;
    --workers) workers="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for value in \
  "$subject_key" "$dictionary_csv" "$important_csv" "$metadata_results_csv" \
  "$output_dir" "$subject_config" "$api_base" "$credentials_file"; do
  if [[ -z "$value" ]]; then
    echo "missing required argument" >&2
    exit 2
  fi
done

root="/mnt/nas2/home/wangqiyuan/subject_broad_recall_0611_20260903"
prepare_script="$root/bin/prepare_post_metadata_md_audit.py"
audit_script="/mnt/nas2/home/wangqiyuan/general_book_screening_20260903/bin/general_book_screening_pipeline_v7_subject_boundary.py"
source_endpoint="http://oss-cn-hangzhou-zjy-d01-a.ops.cloud.zhejianglab.com"
cache_dir="/mnt/nas2/home/wangqiyuan/general_book_screening_20260903/cache/pipeline_result1_md"
combined_csv="$output_dir/MD审核试验样本.csv"
summary_json="$output_dir/MD审核输入合并汇总.json"
progress_jsonl="$output_dir/模型原始结果/progress.jsonl"
log_file="$output_dir/workflow.log"
metadata_summary_json="${metadata_results_csv%/*}/书目大模型精筛汇总.json"

mkdir -p "$output_dir" "$cache_dir"
exec >>"$log_file" 2>&1
echo "$(date -Is) waiting_for_metadata subject=$subject_key"

while [[ ! -s "$metadata_summary_json" || ! -s "$metadata_results_csv" ]]; do
  sleep 60
done

python3 "$prepare_script" \
  --dictionary-csv "$dictionary_csv" \
  --important-csv "$important_csv" \
  --metadata-results-csv "$metadata_results_csv" \
  --output-csv "$combined_csv" \
  --summary-json "$summary_json"

if [[ "$api_base" == */v1/chat/completions ]]; then
  api_chat="$api_base"
  models_url="${api_base%/v1/chat/completions}/v1/models"
else
  api_chat="${api_base%/}/v1/chat/completions"
  models_url="${api_base%/}/v1/models"
fi

while ! curl -fsS --max-time 20 "$models_url" >/dev/null; do
  echo "$(date -Is) waiting_for_api subject=$subject_key"
  sleep 60
done

credential_lines="$(grep -E '^(export )?(SOURCE_OSS_ACCESS_ID|SOURCE_OSS_ACCESS_KEY|OSS_ACCESS_KEY_ID|OSS_ACCESS_KEY_SECRET)=' "$credentials_file" || true)"
if [[ -z "$credential_lines" ]]; then
  echo "credentials file has no supported source OSS assignments" >&2
  exit 3
fi
set +x
source <(printf '%s\n' "$credential_lines")
unset credential_lines
export SOURCE_OSS_ACCESS_ID="${SOURCE_OSS_ACCESS_ID:-${OSS_ACCESS_KEY_ID:-}}"
export SOURCE_OSS_ACCESS_KEY="${SOURCE_OSS_ACCESS_KEY:-${OSS_ACCESS_KEY_SECRET:-}}"
if [[ -z "$SOURCE_OSS_ACCESS_ID" || -z "$SOURCE_OSS_ACCESS_KEY" ]]; then
  echo "source OSS credentials are empty" >&2
  exit 3
fi
trap 'unset SOURCE_OSS_ACCESS_ID SOURCE_OSS_ACCESS_KEY OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET' EXIT

while true; do
  while ! curl -fsS --max-time 20 "$models_url" >/dev/null; do
    echo "$(date -Is) waiting_for_api subject=$subject_key"
    sleep 60
  done

  python3 "$audit_script" audit \
    --input-csv "$combined_csv" \
    --output "$output_dir" \
    --subject-config "$subject_config" \
    --workers "$workers" \
    --sample-count 16 \
    --chunk-chars 1200 \
    --api-url "$api_chat" \
    --timeout 240 \
    --retries 2 \
    --max-tokens 2400 \
    --cache-dir "$cache_dir" \
    --source-oss-endpoint "$source_endpoint"

  retryable_failed="$(python3 - "$progress_jsonl" <<'PY'
import json
import sys
from pathlib import Path

latest = {}
path = Path(sys.argv[1])
if path.is_file():
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                item = json.loads(line)
                latest[item["identifier"]] = item
retryable = {"api_failed", "unresolved_md_path"}
print(sum(item.get("audit_status") in retryable for item in latest.values()))
PY
)"
  echo "$(date -Is) audit_round_complete subject=$subject_key retryable_failed=$retryable_failed"
  if [[ "$retryable_failed" == "0" ]]; then
    break
  fi
  sleep 60
done

python3 "$audit_script" materialize \
  --input-csv "$combined_csv" \
  --output "$output_dir" \
  --subject-config "$subject_config"

python3 - "$subject_key" "$combined_csv" "$progress_jsonl" "$output_dir/workflow_complete.json" <<'PY'
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

subject, input_csv, progress_jsonl, output_json = sys.argv[1:]
with Path(input_csv).open(encoding="utf-8-sig", newline="") as handle:
    input_rows = sum(1 for _ in csv.DictReader(handle))
latest = {}
with Path(progress_jsonl).open(encoding="utf-8") as handle:
    for line in handle:
        if line.strip():
            item = json.loads(line)
            latest[item["identifier"]] = item
status_counts = Counter(item.get("audit_status", "") for item in latest.values())
payload = {
    "subject": subject,
    "completed_at": datetime.now().astimezone().isoformat(),
    "input_rows": input_rows,
    "recorded_rows": len(latest),
    "audit_status_counts": dict(status_counts),
    "complete": (
        len(latest) == input_rows
        and status_counts.get("api_failed", 0) == 0
        and status_counts.get("unresolved_md_path", 0) == 0
    ),
}
Path(output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
if not payload["complete"]:
    raise SystemExit(4)
PY

echo "$(date -Is) workflow_complete subject=$subject_key"
