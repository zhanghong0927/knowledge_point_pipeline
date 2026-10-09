#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export PYTHONPATH="$root/scripts:$root${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING=utf-8
: "${SUBJECT_CONFIG:?Set absolute SUBJECT_CONFIG}"
: "${RUN_DIR:?Set a new absolute RUN_DIR}"
readarray -t scope < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1],encoding="utf-8-sig")); print(c["subject_name"]); print(c["subject_slug"])' "$SUBJECT_CONFIG")
name="${scope[0]}"
slug="${scope[1]}"
recalled="${RECALLED_CSV:-$RUN_DIR/01_recall/$slug/${name}_满足后续处理条件.csv}"
dictionary="$RUN_DIR/02_metadata/辞海类/初筛/进入MD审核0611字段.csv"
important="$RUN_DIR/02_metadata/其他重要书籍/初筛/严格保留0611字段.csv"
path_args=()
if [[ -n "${MD_PREFIX:-}" ]]; then
  : "${MD_ROOT:?Set MD_ROOT together with MD_PREFIX}"
  path_args=(--path-map "$MD_PREFIX=$MD_ROOT")
fi
oss_args=()
if [[ -n "${SOURCE_OSS_ENDPOINT:-}" ]]; then
  oss_args=(--source-oss-endpoint "$SOURCE_OSS_ENDPOINT")
fi
stage() {
  case "$1" in
    1)
      : "${CORPUS:?Set CORPUS}"
      python3 scripts/stream_subject_recall_0611.py --input-csv "$CORPUS" --config "$SUBJECT_CONFIG" --output "$RUN_DIR/01_recall"
      ;;
    2)
      python3 scripts/general_book_screening_pipeline.py metadata --input-csv "$recalled" --output "$RUN_DIR/02_metadata" --audit-track 辞海类 --audit-all --subject "$name"
      ;;
    3)
      : "${API_URL:?Set full chat-completions API_URL}"
      : "${MODEL:?Set exact MODEL}"
      python3 scripts/llm_refine_important_book_metadata.py --input-csv "$important" --subject-config "$SUBJECT_CONFIG" --output-dir "$RUN_DIR/03_important_metadata" --api-url "$API_URL" --model "$MODEL" --sample-size 0 --workers "${META_WORKERS:-20}" --retries 2
      ;;
    4)
      python3 scripts/prepare_post_metadata_md_input.py --dictionary-csv "$dictionary" --important-csv "$important" --llm-result-csv "$RUN_DIR/03_important_metadata/书目大模型精筛结果.csv" --output-csv "$RUN_DIR/04_input/MD审核输入.csv" --summary-json "$RUN_DIR/04_input/SUMMARY.json"
      python3 screening_io.py split --input-csv "$RUN_DIR/04_input/MD审核输入.csv" --out "$RUN_DIR/04_tracks"
      ;;
    5)
      : "${API_URL:?Set full chat-completions API_URL}"
      : "${MODEL:?Set exact MODEL}"
      python3 -c 'from pathlib import Path; import sys; from screening_io import read_csv,write_csv; fields,rows=read_csv(Path(sys.argv[1])); write_csv(Path(sys.argv[2]),fields,rows)' "$RUN_DIR/04_tracks/important.csv" "$RUN_DIR/05_important_md/MD审核试验样本.csv"
      python3 scripts/general_book_screening_pipeline.py audit --input-csv "$RUN_DIR/04_tracks/important.csv" --output "$RUN_DIR/05_important_md" --subject-config "$SUBJECT_CONFIG" --api-url "$API_URL" --model "$MODEL" --workers "${MD_WORKERS:-50}" --sample-count 16 --chunk-chars 1200 --retries 2 "${path_args[@]}" "${oss_args[@]}"
      python3 scripts/general_book_screening_pipeline.py materialize --input-csv "$RUN_DIR/04_tracks/important.csv" --output "$RUN_DIR/05_important_md"
      ;;
    6)
      : "${API_URL:?Set full chat-completions API_URL}"
      : "${MODEL:?Set exact MODEL}"
      python3 screening_io.py dictionary-prepare --input-csv "$RUN_DIR/04_tracks/dictionary.csv" --subject-config "$SUBJECT_CONFIG" --out "$RUN_DIR/05_dictionary_input" "${path_args[@]}" "${oss_args[@]}"
      ready="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ready"])' "$RUN_DIR/05_dictionary_input/PREPARED.json")"
      if [[ "$ready" -gt 0 ]]; then
        python3 dictionary_md_v3/dictionary_md_audit.py all --root "$RUN_DIR/05_dictionary_input" --config "$RUN_DIR/05_dictionary_input/audit_config.json" --output "$RUN_DIR/05_dictionary_md" --api-url "$API_URL" --model "$MODEL" --workers "${DICT_WORKERS:-16}" --sample-count 16 --chunk-chars 1400 --retries 2 --max-tokens 5500 --knowledge-value-mode --pass-challenge --skip-priority-workbook --symlink-classified
      else
        python3 -c 'from pathlib import Path; import sys; from screening_io import write_csv; write_csv(Path(sys.argv[1]),["subject","file_name","source_path","decision"],[])' "$RUN_DIR/05_dictionary_md/全学科审核结果.csv"
      fi
      python3 screening_io.py dictionary-finalize --prepared "$RUN_DIR/05_dictionary_input" --audit-results "$RUN_DIR/05_dictionary_md/全学科审核结果.csv" --out "$RUN_DIR/06_dictionary_final"
      ;;
    7)
      python3 screening_io.py merge-results --input-csv "$RUN_DIR/05_important_md/最终审核结果.csv" --input-csv "$RUN_DIR/06_dictionary_final/最终审核结果.csv" --out "$RUN_DIR/07_final"
      ;;
    *) echo 'Usage: bash examples/run_stages.sh {all|1|2|3|4|5|6|7}' >&2; exit 2 ;;
  esac
}
if [[ "${1:-}" == all ]]; then
  if [[ -e "$RUN_DIR" ]]; then
    echo 'all requires a new RUN_DIR; use a numbered stage to resume unchanged inputs' >&2
    exit 2
  fi
  for current in 1 2 3 4 5 6 7; do stage "$current"; done
else
  stage "${1:-}"
fi
