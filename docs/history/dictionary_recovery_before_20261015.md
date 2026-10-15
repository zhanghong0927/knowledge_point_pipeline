> 历史说明，保留当时原文；不作为当前运行指引。现行约定见项目README。

# 辞海 V5 独立恢复与 64k 测试

本版本补齐小批内容修复、截断后拆批、受原文约束的词头修正，以及 `/tokenize` 瞬时故障恢复。当前生产任务使用自己的冻结代码，不会因为本地代码修改而热更新。原生产目录的 INPUT、缓存、完成标记均保留。

## 修复策略

- 每次最多 8 个候选，每个候选最多 2 次内容修复，每块最多 128 个修复请求。
- 输入放不下的候选不会让其余候选失去修复机会。修复回复被截断时减小批量，不丢弃已通过条目；单项仍截断时记录输出预算不足。
- 未解决项记录 `repair_attempts` 与 `repair_stop_reason`，书级汇总写入 `repair_stop_reasons`，区分 `input_budget`、`output_budget`、`attempts_exhausted`、`request_budget`、`transport_deferred`。
- 正常首次发现与三轮全局 HTTP 504 恢复仍分开。504 不在本地小批循环立即反复调用；后续降低全局额度后重放相同载荷，保留哈希校验。
- `/tokenize` 的 429、502、503、504 与网络超时最多请求 3 次。413 仍交给结构细分；401 等非瞬时错误直接保留技术失败。重试用尽不会把技术失败当成内容不合格。
- 词头裁剪必须保持相同原文位置，已有名称字段必须精确支持裁剪后的词头，后缀须有定义语法证据。缩略词补全只允许同一源单元的相邻原文括号缩略词，且与全称首字母匹配。跨书、换位置、任意词组裁剪继续拒绝。

## 独立补跑入口

原生书籍目录是包含 `INPUT.json`、`units.json`、`chunks/` 的目录，而不是最外层全流程任务目录。`--source-run` 可以重复，最多选定需要恢复的书籍；下面示例先生成恢复计划，不调用模型。

```bash
python3 modules/dictionary/src/recover_fullbook_v5.py \
  --source-run /mnt/nas_si002991c1cm/wenke/PDF/wangqiyuan/development/pipeline_staged_30books_16card_20261012_01/dictionary/production/civil_engineering/14937260/run/03_extraction/raw/5ce4b168cd5f3ad1 \
  --out /mnt/nas_si002991c1cm/wenke/PDF/wangqiyuan/development/dictionary_recovery_separate_01 \
  --api-url http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job.v5000-prod.nhss.zhejianglab.com \
  --model /mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B \
  --context 65536 \
  --output-tokens 8192 \
  --repair-batch-size 8 \
  --repair-attempts 2 \
  --repair-max-requests 128 \
  --tokenize-attempts 3 \
  --round-workers 8 2 1
```

准备成功写出 `MANIFEST.json` 与 `RECOVERY_PLAN.json`。同一命令添加 `--run` 才执行；更换上下文、模型地址、源码或修复参数时须使用新的输出目录。执行时沿用既有三轮全局请求额度：8 → 2 → 1 是保守恢复默认值，实际后续批量额度仍需标定。

入口核验 MD 哈希、结构单元、旧缓存模式和旧条目的原文引用；缺失字段不会被当成空缓存。已验证条目复用，已有修复回包只有通过当前原文与身份校验后才复用，重复 repair_id 不会绕过原回包协议。整书中断、未写出书级 SUMMARY 的目录也可作为恢复来源，前提是原输入和缓存仍可核验。它不会修改旧驱动的 aborted 状态或清除旧缓存。

入口会从旧 `ROUNDS.json` 自动定位原生输入清单，以保留 PDF 与布局输入；无法定位时可指定 `--original-manifest`。无法重建同样结构会明确拒绝，不会偷偷忽略原布局信息。

新原生结果位于 `native/`。`completed` 表示本次抽取与原文验证结束；不是语义批准，也不是清洗、挂载和去重完成。后续按正常管线处理新结果，再与已接受成果合并。

## 新测试服务启动命令

每副本 DP4 × TP2 使用 8 卡，平台部署两个副本合计 16 卡。请在新服务使用以下命令；它不切换旧任务地址。

```bash
vllm serve /mnt/si002647a3lv/zhanghong/model/modelscope/Qwen/Qwen3.8-27B \
  --data-parallel-size 4 \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 65536 \
  --max-num-seqs 256 \
  --max-num-batched-tokens 32768 \
  --enable-chunked-prefill
```

`max-model-len` 控制输入加输出总长度，脚本 `--context` 要同步设置，输出仍单独由 `--output-tokens` 控制。分块预填充允许将长输入分步处理，所以批处理 token 预算先保持 32k，便于比较；参数定义与约束见 [vLLM serve](https://docs.vllm.ai/en/stable/cli/serve/) 和 [调优文档](https://docs.vllm.ai/en/stable/configuration/optimization/)。服务当前报告 vLLM 0.22.0 与 65,536 上下文，实际长请求结果另以测试收据为准。

16 卡增加服务整体吞吐，不会将两个副本的 KV 缓存自动合并给单个请求。64k 尚不能替代实测并发标定，也不能修复缺少原文证据的候选。
