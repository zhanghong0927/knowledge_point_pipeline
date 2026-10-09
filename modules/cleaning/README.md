# Books / Textbooks Two-Stage Cleaning Pipeline

整合副本修正（2026-10-08）：规则版为 `books_two_stage_rule_v3_numbered_layout_20261008`，模型本地清洗策略为 `books_two_stage_model_v6_numbered_layout_20261008`。图表引用要求带数字编号，避免损坏 Stable、Configuration、table tennis 等正常英文。仅修改 knowledge_point_pipeline 内的副本；下文保留历史版本说明，旧检查点不要混用于新策略。

该目录是从 `Books_textbooks_pipeline` 拆出的独立两阶段清洗流程，适用于已经完成候选抽取或BM25召回的数据。它不负责解析Markdown，也不在内部重新执行BM25、Embedding或知识树挂载。

**输入候选不限定为百科或辞典类词条。** BM25只提供字面相关候选，流程同时接纳概念、对象、方法、工艺步骤、计算过程、技术要求、原理、公式、定律、现象、故障模式、试验方法和稳定的课程知识单元。是否存在显式定义不是保留前提。

目标：

1. 使用保守通用规则删除确定性格式垃圾，尽量保留真实知识点，减少模型输入量。
2. 使用OpenAI兼容模型完成学科范围、名称质量、标题残留、双语字段和标准格式审核。

## 1. 目录结构

```text
Books_textbooks_cleaning_pipeline/
├── README.md
├── requirements.txt
├── .env.example
├── run.py
├── scripts/
│   ├── rule_clean.py
│   └── model_clean.py
├── schemas/
│   └── knowledge_point_standard.schema.json
├── prompts/
│   └── model_clean_policy.md
├── examples/
│   └── input_sample.jsonl
└── tests/
    ├── test_rule_clean.py
    ├── test_model_clean.py
    └── test_run_all.py
```

另有：

```text
Books_textbooks_cleaning_pipeline/
├── tools/                     运行编排、统计抽检、吞吐标定与本地后处理工具
│   ├── README.md              用法与约定
│   ├── run_subjects.py        按 JSON 配置跑多学科（健康检查/断点续跑/跳过已完成）
│   ├── finalize_when_done.py  轮询完成状态并自动统计
│   ├── summarize_and_sample.py 统计 + 100 条分层抽检
│   ├── recover_name_only.py   本地回收"名称有效/定义不可用"记录（A2）
│   ├── demote_review.py       删除 review 队列（merge 保留审计 / purge 直接丢弃）
│   └── calibrate_throughput.py 真实 payload 的并发-吞吐标定
└── runs/                      清洗运行记录（数据与溯源）
    └── three_subject_v4_20260923/   三学科（土木/建筑/机械）未清洗数据 v4 清洗
```

## 2. 输入格式

输入为一行一条记录的JSONL。规则阶段兼容下列字段：

| 标准字段 | 兼容字段 |
|---|---|
| `id` | `global_id`、`record_id`、`raw_collection_id` |
| `name` | `key_zh` |
| `knowledge_point` | `key_en` |
| 正文 | `definition`、`description`、`explanation`、`raw_text` |
| 来源 | `source`、`source_id`、`input_file` |

至少应有 `name` 或 `knowledge_point`。其他字段可以为空。

## 3. 第一阶段：保守规则清洗

### 3.1 处理原则

规则阶段只硬删除确定性错误：

- 两个名称均为空；
- 单个拉丁字母，默认删除，可配置保留；
- 裸数字、裸年份或日期；
- URL、邮箱；
- 明显乱码、替换字符、重复符号；
- 纯章节、目录、习题、小结等结构标题；
- 明确图表标题和独立例题编号；
- 极端超长且无法作为单一名称处理的内容；
- 没有任何可用文字、数字或专业符号的内容。

规则阶段会安全修复：

- Unicode NFKC和简繁转换；
- Markdown标题符、列表符、序号；
- 行末LaTeX目录点线和页码，例如 `创新文化\(\ldots \ldots\)117` 清洗为 `创新文化`；
- 完整包裹名称的单双引号；
- 两端孤立的半个括号；
- HTML、图片标记、图表引用和段落符号。

以下内容不会因为规则直接删除：

- 没有显式定义；
- 名称较短但不是单个拉丁字母；
- 罕见专业术语；
- `CAD/CAM`、`PID控制`等正常中英混合术语；
- 学科边界不明确但可能有效的概念；
- 定义不完整但名称仍然成立的记录；
- “齿轮强度计算”“轴承选择”“焊接缺陷分析”等稳定的方法或过程型知识点；
- “如何选择轴承”等可能具有教材表达形式、但仍需模型结合正文判断的候选。
- 完整句子、辞典交叉引用、同行定义和异常中英混排，规则只标记并交模型处理；
- `词条1.4-3`、`英译`、`含义`等正文中可能仍含真实词条的可恢复抽取标签。

### 3.2 运行命令

```bash
cd /mnt/nas2/home/zh_data/test/code
source /root/dataprocess/dclm/venv/bin/activate

python Books_textbooks_cleaning_pipeline/run.py rule \
  --input INPUT.jsonl \
  --output-dir OUTPUT/01_rule_clean \
  --sample-size 200 \
  --overwrite
```

如果词典中的单字母本身是合法词条，例如字母索引辞典中的`A`，增加：

```bash
--keep-single-letter
```

### 3.3 输出

| 文件 | 含义 |
|---|---|
| `rule_pass.jsonl` | 进入模型阶段的记录，包含规则清洗审计字段 |
| `rule_rejected.jsonl` | 确定性格式垃圾 |
| `rule_pass_sample.jsonl` | 前N条快速抽检样本 |
| `rule_clean_report.json` | 数量、原因、标记和字段移除统计 |

第一阶段不产生“语义不合格”结论。可疑但不确定的数据保留在 `rule_pass.jsonl`。

## 4. 第二阶段：模型清洗与标准化

### 4.1 模型职责

模型阶段处理：

1. 目标学科范围筛选及直接交叉学科判断；记录若带混合召回的候选节点，则该节点的"接纳/排除"范围是判定边界的第一依据（见 §4.7）。
2. 可独立组织、检索或教学的知识单元与标题、句子、一次性任务、图表说明、出版元数据的区分，不要求采用百科或辞典定义格式。
3. 残余序号、Markdown、教学脚手架和图号标志清理。
4. 中文名称映射到 `name`，英文名称映射到 `knowledge_point`；本地会校验语言位置，位置颠倒的记录降为 `review`。
5. `definition`、`description`及中英文字段的清洗和重组。
6. 输出 `keep`、`drop` 或 `review`，不确定记录不会被强行删除。
7. 分别审核原始 `name` 和 `knowledge_point`，保存字段级 `keep/drop/empty` 及理由；模型未按协议返回的字段标记 `invalid`，不再写出协议外的 `review` 取值。
8. 两个名称同时存在时判断 `pair_consistency`，明确错配整条删除。

模型不得使用外部知识翻译名称或补写事实，只能根据输入字段清洗、截取和压缩。每次运行生成的完整Prompt保存到输出目录。

BM25排名或得分不作为 `keep` 的直接证据，也不作为 `drop` 的理由；模型必须根据名称、上下文和目标学科独立判断内容质量。

### 4.2 安全机制

新流程吸收了 `通用词条名清洗脚本包_v5_20260907` 的三项机制（第 1–3 项），并新增两项本地守卫（第 4–5 项）：

1. **字段级审核**：模型必须分别返回 `field_results.name` 和 `field_results.knowledge_point`。原字段非空时只能为 `keep/drop`，原字段为空时必须为 `empty`；缺项、无理由或枚举不符时该字段标记 `invalid=true`，本地不再写出协议外的 `review` 取值，整条记录降为 `review`。
2. **双语对应关系**：两个名称均非空时必须返回 `consistent/inconsistent/uncertain`；明确 `inconsistent` 时整条记录删除，`uncertain` 不会仅因此删除。
3. **严格修复校验**：标准化名称必须能回溯到原名称。允许的修改仅包括安全标题符清理、原字段内明确的中英文拆分、通用栏目后缀删除，以及把较长末尾括号说明逐字迁移到描述。其他名称改写转入 `review`。
4. **语言位置守卫**：两项都有内容，且模型把英文放进 `name`、中文放进 `knowledge_point` 时，本地标记 `language_assignment_swapped_by_model` 并把整条降为 `review`。加 `--fix-swapped-languages` 时自动换位、记录 `language_assignment_corrected`，并写入 `model_repair_audit.jsonl`。`PID控制`、`CAD/CAM技术` 这类中英混合术语不会被触发。
5. **逐条候选节点范围**：输入带混合召回的 `candidate_paths`（或 `hybrid_consensus.node_path`）时，该记录的前 N 个知识树节点及其"接纳/排除"文本随记录发给模型；节点路径不在当前知识树中会被计数，便于发现知识树版本不匹配。

定义和描述修改还会计算词汇重合率。中文或英文定义与输入内容重合不足72%，描述重合不足60%时，记录转入 `review`，防止模型引入外部事实。

### 4.3 200条测试

```bash
python Books_textbooks_cleaning_pipeline/run.py model \
  --input OUTPUT/01_rule_clean/rule_pass.jsonl \
  --output-dir OUTPUT/02_model_clean_test200 \
  --subject "机械工程" \
  --subject-description "机械设计、制造、力学、材料、机电、测量、动力装备及其直接相关知识" \
  --taxonomy mechanical_data_pool/mechanical_engineering_bok_base-20260805.json \
  --api-url "http://YOUR_SERVICE/v1/chat/completions" \
  --model "/path/or/model-name" \
  --workers 8 \
  --batch-size 4 \
  --limit 200 \
  --overwrite
```

默认发送 `response_format={"type":"json_object"}`。若服务不支持，`auto`模式会自动去除该参数重试；也可显式使用：

```bash
--response-format off
```

### 4.4 全量与续跑

确认200条结果后换一个正式输出目录运行：

```bash
python Books_textbooks_cleaning_pipeline/run.py model \
  --input OUTPUT/01_rule_clean/rule_pass.jsonl \
  --output-dir OUTPUT/02_model_clean \
  --subject "机械工程" \
  --taxonomy mechanical_data_pool/mechanical_engineering_bok_base-20260805.json \
  --api-url "http://YOUR_SERVICE/v1/chat/completions" \
  --model "/path/or/model-name" \
  --workers 32 \
  --batch-size 8 \
  --overwrite
```

任务中断后使用完全相同的输入和输出目录：

```bash
python Books_textbooks_cleaning_pipeline/run.py model \
  ...原参数... \
  --resume \
  --retry-errors
```

模型结果先写入SQLite检查点，再按输入顺序生成JSONL。SQLite只保存模型判断，不修改源文件；检查点带 `policy_version` 列，只有与当前策略标识一致的结果才会被 `--resume` 复用，不匹配的旧行会被计数并在报告中体现（报告字段 `ignored_other_policy_version`）。

2026-09-23运行修正：`--context-chars`现在是定义、描述和补充context共享的正文字符预算，避免原字段全文和context重复发送而超过接口上下文上限。输入截断会标记`input_truncated`，原始字段仍保存在输入文件中。模型输出字段级审核或双语对应关系缺项时，本地转为`review`。此版本策略标识为`books_two_stage_model_v3_bounded_input_20260923`；旧检查点只用于同版本续跑，策略变化时使用新的输出目录。

2026-09-23 v4 更新（策略标识 `books_two_stage_model_v4_language_guard_taxonomy_l4_20260923`）：

1. 新增语言位置守卫与逐条候选节点范围注入（§4.2、§4.7）。
2. `--taxonomy-chars` 默认由 5000 提高到 12000，新增 `--taxonomy-depth`（默认 3）、`--taxonomy-scope-chars`（默认 240）、`--taxonomy-candidate-nodes`（默认 3）。
3. SQLite 检查点新增 `policy_version` 列，旧库自动迁移并标记为上一版策略，因此不会与新策略混用。
4. 新增 `--retry-decisions`（`keep,drop,review,error` 子集）用于重跑指定档位，`--retry-errors` 保留为其简写；`review` 因此可以被批量重跑。
5. 新增 `--strict-checkpoint`：最终化时若存在没有检查点结果的输入记录，直接报错而不是静默写入 `model_errors.jsonl`。
6. 报告新增 `review_causes`、`quality_flags`、`field_result_counts`、`title_repair_types`、`grounding_min_overlap_bins`、`taxonomy_summary`、`batch_stats` 与 `missing_checkpoint_result`。

Prompt 已改变，**必须使用新的输出目录**运行全量，不能续用 v3 目录。

2026-09-30 v5 更新（策略标识 `books_two_stage_model_v5_layout_token_boundary_20260930`）：

- 英文 `Figure/Table` 布局引用清理增加单词边界，避免误截断 `Stable`、`Configuration`、`Configurable` 等正常术语。
- 策略版本已更新；涉及该缺陷的旧检查点不应直接复用。

复核与重跑：

```bash
# 只重跑上一轮被判为 review 的记录（建议配更小的 batch-size）
python Books_textbooks_cleaning_pipeline/run.py model \
  ...原参数... \
  --resume --retry-decisions review

# 无 API 时按检查点重建 JSONL 产物
python Books_textbooks_cleaning_pipeline/run.py model \
  --input OUTPUT/01_rule_clean/rule_pass.jsonl \
  --output-dir OUTPUT/02_model_clean \
  --subject "机械工程" \
  --finalize-only
```

重跑会以 `INSERT OR REPLACE` 覆盖同一 `record_key` 的判断；最终化按输入顺序重新生成全部 JSONL。

### 4.5 鉴权

默认按无鉴权本地接口请求。需要Bearer Key时设置：

```bash
export KNOWLEDGE_CLEAN_API_KEY='YOUR_KEY'
```

并增加 `--with-auth`。不要把真实Key提交到代码仓库。

### 4.6 输出

| 文件 | 含义 |
|---|---|
| `clean_standard.jsonl` | 模型确认并经本地校验的标准知识点 |
| `model_dropped.jsonl` | 明确学科不符或名称不可用记录 |
| `model_review.jsonl` | 信息不足、低置信或模型修复未通过本地校验 |
| `model_errors.jsonl` | 请求或JSON解析最终失败记录 |
| `model_judgments.jsonl` | 每条记录的模型判断，不复制完整源记录 |
| `model_repair_audit.jsonl` | 名称或内容发生变化的原值、标准值及本地验证结果 |
| `raw_api_responses.jsonl` | 原始模型响应，供审计 |
| `model_clean_checkpoint.sqlite` | 断点续跑状态 |
| `model_clean_prompt.txt` | 本轮实际系统Prompt |
| `model_clean_report.json` | 配置、数量、置信度、Token 统计及全部本地校验聚合 |

`model_clean_report.json` 除配置与数量外，还包含：

| 字段 | 含义 |
|---|---|
| `review_causes` | `review`/`error` 的归因：本地协议失败、本地置信不足、名称不可回溯、内容接地不足、语言位置颠倒、模型不确定、接口失败 |
| `quality_flags` | 全部标记的计数 |
| `field_result_counts` | 字段级判定分布，含 `invalid` 计数 |
| `title_repair_types` | 名称修改类型分布（安全标准化、双语拆分、括号迁移、栏目前后缀删除等） |
| `grounding_min_overlap_bins` | 每条记录最低词汇重合率分布，用于校准接地阈值 |
| `taxonomy_summary` | 全局知识树摘要的字符数、纳入节点数、深度与是否被预算截断 |
| `batch_stats` | 逐条知识树注入命中率、无候选节点记录数、未知节点路径数、输入截断数 |
| `ignored_other_policy_version` | 因策略标识不一致而未被复用的检查点行数 |
| `missing_checkpoint_result` | 最终化时缺少检查点结果的记录数 |

`clean_standard.jsonl`字段为：

```json
{
  "id": 1,
  "knowledge_point": "English name",
  "name": "中文名称",
  "definition": "中文定义",
  "en_definition": "English definition",
  "description": "中文描述",
  "en_description": "English description",
  "main_tags": "",
  "related_tags": [],
  "source": "来源"
}
```

允许 `name` 或 `knowledge_point` 其中一个为空，但不能同时为空。定义和描述可为空，因为本流程是清洗而不是外部知识补全。

### 4.7 知识树注入与预算

实测（`mechanical_data_pool/mechanical_engineering_bok_base-20260805.json`，共 1,641 个节点；`depth 4/5` 是混合召回的目标层）：

| 节点 depth | 节点数 | 带接纳/排除范围 | 名称字符量 | 范围文本字符量 |
|---|---:|---:|---:|---:|
| 0–2（学科框架） | 107 | 9 | 1,364 | 1,028 |
| 3 | 370 | 40 | 4,604 | 3,552 |
| 4（召回目标层） | 1,164 | 286 | 17,109 | 25,003 |
| 5（召回目标层） | 120 | 30 | 1,609 | 2,453 |

L4/L5 的范围文本合计约 27k 字符，无法整体放进每次请求，因此注入分两部分：

1. **全局摘要**（每个请求发送一次）：depth ≤ `--taxonomy-depth`（默认 3）的节点名称及其接纳/排除范围；装填顺序为"浅层名称 → 浅层范围 → 深层范围 → 深层名称"，超出 `--taxonomy-chars`（默认 12000）即停。默认参数下实测摘要 11,999 字符（49 条范围 + 337 个名称），系统 Prompt 合计 14,398 字符。
2. **逐条候选节点**（随记录发送）：从 `candidate_paths`（或 `hybrid_consensus.node_path`）取前 `--taxonomy-candidate-nodes`（默认 3）个节点，附加路径与接纳/排除范围，单节点受 `--taxonomy-scope-chars`（默认 240）限制。示例：两个 L4 节点合计约 212 字符。

成本参考（300k 条，中文约 1 字符 ≈ 1 token；全局块在每个请求中重复）：

| 配置 | 每请求输入 | 全量输入 token |
|---|---:|---:|
| 默认（`--taxonomy-chars 12000`，batch 8） | 约 16.1k | 约 600M |
| 默认 + `--batch-size 16` | 约 17.8k | 约 335M |
| `--taxonomy-chars 6000`，batch 8 | 约 10.1k | 约 380M |

建议：全量时用 `--batch-size 12~16` 摊薄全局块，并相应提高 `--max-tokens` 以容纳输出，同时检查 `model_errors.jsonl` 是否出现截断；接口若支持前缀缓存，全局块可被复用，成本会明显下降。调整 `--taxonomy-candidate-nodes`、`--taxonomy-scope-chars`、`--taxonomy-chars` 或 `--taxonomy-depth` 不需要重建任何索引。

输入若不带候选节点路径（例如直接使用词条式数据），逐条注入自动失效，`batch_stats.records_without_candidate_nodes` 会等于全部记录数，此时只能依靠全局摘要，需要按需提高 `--taxonomy-depth` 或 `--taxonomy-chars`。

## 5. 一键运行两阶段

```bash
python Books_textbooks_cleaning_pipeline/run.py all \
  --input INPUT.jsonl \
  --output-dir OUTPUT \
  --subject "机械工程" \
  --taxonomy mechanical_data_pool/mechanical_engineering_bok_base-20260805.json \
  --api-url "http://YOUR_SERVICE/v1/chat/completions" \
  --model "/path/or/model-name" \
  --workers 32 \
  --batch-size 8 \
  --overwrite
```

一键模式输出：

```text
OUTPUT/
├── 01_rule_clean/
└── 02_model_clean/
```

一键模式支持模型阶段的全部关键参数，包括 `--skip`、`--response-format`、`--retry-errors`、`--retry-decisions`、`--strict-checkpoint`、`--fix-swapped-languages`、`--taxonomy-chars`、`--taxonomy-depth`、`--taxonomy-scope-chars`、`--taxonomy-candidate-nodes`、`--timeout`、`--retries`、`--single-retries`、`--temperature`、`--enable-thinking`、`--limit`：

```bash
# 接口不支持 response_format 时，并加大 batch 摊薄知识树开销
python Books_textbooks_cleaning_pipeline/run.py all ... \
  --response-format off --batch-size 16

# 续跑并重跑 review，同时要求检查点完整
python Books_textbooks_cleaning_pipeline/run.py all ... \
  --resume --retry-decisions review --strict-checkpoint

# 不调用 API，仅按已有检查点重建 JSONL（跳过规则阶段）
python Books_textbooks_cleaning_pipeline/run.py all ... --finalize-only
```

`--limit` 会同时作用于规则阶段（`--max-records`）与模型阶段，便于小样本验证；`--finalize-only` 会跳过规则阶段与 API 阶段。

查看将执行的命令但不处理数据：

```bash
python Books_textbooks_cleaning_pipeline/run.py all ... --dry-run
```

## 6. 质量检查建议

正式全量前至少检查：

1. 从 `rule_rejected.jsonl` 随机抽取200条，确认规则误删率足够低。
2. 从 `rule_pass.jsonl` 检查正常缩写、公式名称和中英混合术语是否被保留。
3. 对模型 `keep/drop/review` 各抽取100至200条。
4. 先看 `model_clean_report.json` 的 `review_causes`：`local_contract_missing_field_results`、`local_field_result_invalid`、`local_pair_consistency_invalid` 偏高说明请求格式或模型协议失效，应先修接口或Prompt；`local_content_not_grounded` 偏高说明接地阈值偏严；只有 `model_review_uncertain` 才是真正需要人工判断的不确定样本。
5. 检查学科边界样本、人物、作品、标准和跨学科概念是否符合项目口径，重点看带 `taxonomy_candidates` 的记录是否按节点"排除"范围正确判定。
6. 检查 `pair_consistency=inconsistent` 是否确为中英文错配；并抽检 `language_assignment_swapped_by_model` 是否确为语言位置颠倒，据此决定是否启用 `--fix-swapped-languages`。
7. 从 `model_repair_audit.jsonl` 抽检名称拆分、括号迁移、语言换位和定义压缩。
8. 检查 `batch_stats`：`records_without_candidate_nodes` 等于总数、或 `candidate_paths_unknown` 偏高，说明输入没有混合召回路径或知识树版本不匹配，此时模型只能依赖全局摘要。
9. 检查 `taxonomy_summary.truncated` 与 `grounding_min_overlap_bins`，必要时调整 `--taxonomy-chars`、`--taxonomy-depth`、`--taxonomy-scope-chars` 或接地阈值。
10. 修改Prompt、学科说明、知识树或阈值后使用新的输出目录；`--resume` 只复用同一 `policy_version` 的检查点，`ignored_other_policy_version` 大于 0 说明该目录混用了旧结果。
11. 发布前用 `--strict-checkpoint` 重跑一次最终化，确认没有只有空壳错误的记录。

## 7. 与原Books流程的关系

| 原流程 | 新两阶段流程 |
|---|---|
| `06_clean_candidates.py`同时进行规则清洗和ready/repairable语义启发式分流 | 第一阶段只做确定性规则，不因缺少定义而降级或删除 |
| `07_fill_definitions_api.py`只生成定义 | 第二阶段同时处理学科范围、名称质量、字段标准化和内容清洗 |
| V5词条名脚本不判断学科范围 | 新模型Prompt显式接收学科说明，并按记录注入混合召回候选节点的接纳/排除范围 |
| V5的字段级审核、对应关系和严格修复校验 | 已吸收到新模型输出协议、本地校验和独立修复审计中，并新增语言位置守卫与字段级 `invalid` 标记 |
| JSONL结果文件作为续跑状态 | 新模型阶段使用带 `policy_version` 守卫的SQLite检查点，适合几十万条数据，`--retry-decisions` 可重跑指定档位 |

治理约定：模型策略标识变化（Prompt、学科说明、知识树摘要、阈值或输出协议调整）后必须换用新的输出目录；检查点只复用同版本结果，混合目录会在报告 `ignored_other_policy_version` 中暴露。

## 8. 运行记录：三学科 v4 清洗（2026-09-23/24）

数据目录 `runs/three_subject_v4_20260923/`，运行配置 `run_config.json`，工具用法见 `tools/README.md`。

### 8.1 输入与结果

| 学科 | 输入 | 规则拒绝 | 规则通过 | keep | drop | review | error |
|---|---:|---:|---:|---:|---:|---:|---:|
| civil_engineering（土木） | 1,542 | 55 | 1,487 | 1,263 | 224 | 0 | 0 |
| architecture（建筑） | 35,541 | 937 | 34,604 | 5,454 | 29,150 | 0 | 0 |
| mechanical_engineering（机械） | 35,874 | 1,022 | 34,852 | 10,012 | 24,840 | 0 | 0 |
| **合计** | **72,957** | **2,014** | **70,943** | **16,729** | **54,214** | **0** | **0** |

输入由 `three_subject/code/土木建筑机械辞海类知识点_47本_未清洗原始知识点_20260903.json` 按 `source_subject` 切分；知识树使用 `three_subject/taxonomy/{subject}_taxonomy.json`（这三棵树只含节点名称，没有接纳/排除范围，且输入没有混合召回路径，因此学科判断只依赖节点名称摘要）。

### 8.2 关键运行参数与吞吐

`--response-format schema --batch-size 16 --max-tokens 4096 --context-chars 1000 --taxonomy-chars 4000 --taxonomy-depth 3 --workers 96 --timeout 1200 --retries 1`；模型 `Qwen3.8-27B`（`max_model_len = 32768`）。

实测吞吐（真实 payload，2026-09-23）：

| 配置 | 记录/秒 | tokens/秒 |
|---|---:|---:|
| 16 并发 × batch 16 | 2.26 | 1,619 |
| 32 并发 × batch 16 | 4.32 | 3,045 |
| 64 并发 × batch 16 | 7.45 | 4,892 |
| 96 + 96 并行（两学科） | 3.33（合计） | — |

并发超过约 64 后收益消失；早期 128+128（叠加其他任务约 320）曾把网关打成 502，恢复时改为 64–96 并发并加大超时。

### 8.3 本地后处理与质量观察

1. **A2 本地回收**：模型把 401 条"名称有效、原文定义不可用（`见"X"条`、页码引用、仅规范编号/地名）"的记录判为 `drop`/`review`，已用 `tools/recover_name_only.py` 回收为 `keep`（`definition` 留空），原始结果备份在 `before_recovery/`。
2. **review 队列删除**：按"review 不保留"口径，用 `tools/demote_review.py` 删除三个学科共 **19,508** 条 review（并入 `model_dropped.jsonl` 并带 `model_cleaning.review_demoted` 标记，`model_review.jsonl` 已删除），备份在 `before_review_demotion/`。
3. **名称改写受控**：13,354 条记录发生名称或正文变化；其中 `unsupported_title_rewrite` 全部被拦下（进入 keep 的 0 条），keep 集合中低于本字段接地阈值的条目为 0。
4. **review 归因分布**（删除前）：`model_review_uncertain` 13,269、`local_title_not_grounded` 6,158、`local_field_result_invalid` 328、`local_content_not_grounded` 42。若后续要恢复 review 队列，从 `before_review_demotion/` 回滚即可。

细节见 `runs/three_subject_v4_20260923/README.md`、`cleaning_summary.md`、`sample_100_check.md`。

## 9. 测试

```bash
cd /mnt/nas2/home/zh_data/test/code
source /root/dataprocess/dclm/venv/bin/activate
python -m unittest discover -s Books_textbooks_cleaning_pipeline/tests -v
```

测试不调用模型API。
