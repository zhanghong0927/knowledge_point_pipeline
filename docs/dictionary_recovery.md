# 辞海V5独立抽取恢复

此入口只处理抽取恢复。整次任务接续见[运行与断点恢复](运行与断点恢复.md)。历史64k试验记录已归档，不是当前服务配置。

## 当前策略

- 每批最多8项，每项内容修复最多2次，每块最多128个修复请求；超长输入或截断输出拆小批，保留已通过项。
- 原文定位、词头裁剪、相邻括号缩略词及修复身份仍须核验，不允许任意改名、换位置或跨书混入。
- 常规抽取按原生策略识别408、429、服务端故障及超时等技术失败；`/tokenize`对明确瞬时错误最多3次，413仍交给结构细分。404不泛化放行。
- 停止原因保留input_budget、output_budget、attempts_exhausted、request_budget、transport_deferred等区别；达到上限不是已成功修复。

## 准备独立恢复

`--source-run`指包含`INPUT.json`、`units.json`、`chunks/`的原生书籍目录，不是最外层任务目录。以下只准备计划，不请求模型：

```bash
python -B modules/dictionary/src/recover_fullbook_v5.py \
  --source-run /absolute/old_native_book \
  --out /absolute/new_extraction_recovery \
  --api-url http://MODEL_HOST:8000 --model EXACT_MODEL_ID \
  --context 32768 --output-tokens 8192 \
  --repair-batch-size 8 --repair-attempts 2 --repair-max-requests 128 \
  --tokenize-attempts 3 --round-workers 8 2 1
```

准备后写出`MANIFEST.json`和`RECOVERY_PLAN.json`；同一命令加`--run`才执行。8 → 2 → 1是此独立入口的保守默认值，不能当作通用生产并发；选择其他额度前需核对服务能力、任务共享情况和全局池。

入口核对原MD、结构单元、旧缓存、已接受条目和修复身份；支持重复`--source-run`。原清单无法自动定位时可提供`--original-manifest`，用于保留PDF及布局输入。源码、模型、上下文或修复参数变化需新输出目录。

上下文参数应与实际服务一致，不因历史64k试验自动改为65536。扩大上下文不能补齐缺少的原文证据，也不能替代并发标定。

## 输出与限制

新原生结果位于`native/`，原目录与成功数据保留。已有修复回包只有通过当前协议、原文及身份校验后才复用。

本轮14,969项“每轮新目录、重新处理未通过语义项”的外部驱动属于专项任务逻辑，不能与此通用入口的原生重试规则混同。两本技术失败书与该批记录分别统计。

本入口不会自动把新增结果写回旧生产任务，也不会自动跑完后续清洗、挂载和去重。源码及输入身份无法验证时明确拒绝，不重建假清单绕过旧哈希。
