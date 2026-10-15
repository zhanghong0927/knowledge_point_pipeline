#!/usr/bin/env bash
# Each replica uses 8 GPUs; deploy two replicas for 16 GPUs in total.
set -euo pipefail
exec vllm serve /mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B \
  --data-parallel-size 4 \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 65536 \
  --max-num-seqs 256 \
  --max-num-batched-tokens 32768 \
  --enable-chunked-prefill
