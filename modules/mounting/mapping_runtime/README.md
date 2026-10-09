# 通用学科知识点分层挂载运行包

## 1. 用途

本包将知识点逐条挂载到任意学科的层级分类树，并对挂载结果进行后置信度分级。机械工程配置已经完整内置；其他学科不需要修改主程序，只需新增一个学科配置目录。

当前默认使用 Qwen3.6 接口。API 接入、服务部署和模型调用相关问题，统一查看包内《南湖平台部署模型教程》。

## 2. 完整包结构

```text
hierarchical_knowledge_mapping_runtime_v1/
├─ scripts/
│  ├─ hierarchical-knowledge-labeling-beam-v3.py   通用挂载主程序
│  └─ calibrate_mount_confidence_v2.py              挂载后置信度分级
├─ profiles/
│  ├─ mechanical_engineering/                       可直接运行的机械工程配置
│  │  ├─ profile.json
│  │  ├─ routing_constraints.py
│  │  ├─ prompts/hierarchical_mapping_prompt_v3.py
│  │  └─ data/
│  │     ├─ knowledge_tree.json
│  │     └─ semantic_cards.jsonl
│  └─ template/                                     其他学科复制模板
├─ examples/input.example.jsonl
└─ 南湖平台部署模型教程.docx                        API 与模型服务参考
```

挂载主程序不是单文件脚本。Prompt、规则插件、分类树和语义卡片都属于运行依赖，必须随包保留。

## 3. API 与模型服务

挂载脚本已经写入当前 Qwen3.6 的默认调用参数。API 接入、模型部署、服务地址和运行环境相关问题，请查看包内 [南湖平台部署模型教程.docx](./南湖平台部署模型教程.docx)。

接口或模型发生变化时，可通过 `--api-url`、`--model` 覆盖脚本默认值。
## 4. 直接运行机械工程配置

Windows PowerShell：

```powershell
$pkg = 'D:\七月工作\机械工程知识点抽取\packages\hierarchical_knowledge_mapping_runtime_v1'
$py = 'C:\Users\ZJ\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

& $py "$pkg\scripts\hierarchical-knowledge-labeling-beam-v3.py" `
  --input 'D:\path\input.jsonl' `
  --output 'D:\path\output.jsonl' `
  --profile-dir "$pkg\profiles\mechanical_engineering" `
  --workers 64 `
  --retry-failed-workers 32 `
  --retries 2 `
  --retry-failed-rounds 2 `
  --retry-failed-cooldown 10 `
  --timeout 600
```

Linux / 开发机终端：

```bash
PKG=/path/to/hierarchical_knowledge_mapping_runtime_v1

python3 "$PKG/scripts/hierarchical-knowledge-labeling-beam-v3.py" \
  --input /path/to/input.jsonl \
  --output /path/to/output.jsonl \
  --profile-dir "$PKG/profiles/mechanical_engineering" \
  --workers 64 \
  --retry-failed-workers 32 \
  --retries 2 \
  --retry-failed-rounds 2 \
  --retry-failed-cooldown 10 \
  --timeout 600
```

建议第一次增加 `--limit 20` 做连通性和字段测试。

## 5. 复用到其他学科

复制 `profiles/template`，例如改名为 `profiles/civil_engineering`，然后只修改该目录内的内容。

### 5.1 profile.json

填写学科名称、总体范围和四项资产路径。相对路径以该学科配置目录为基准。

### 5.2 knowledge_tree.json

分类树每个节点至少包含：

```json
{
  "code": "唯一节点编码",
  "name_zh": "中文节点名",
  "name_en": "英文节点名，可为空",
  "path": "从根到当前节点的完整路径",
  "children": []
}
```

根节点编码不要求为 `ME`；通用脚本会自动读取实际根编码。同级节点编码必须唯一。

### 5.3 semantic_cards.jsonl

每行对应一个分类节点：

```json
{
  "node_code": "与分类树 code 一致",
  "semantic_card": "该节点收录什么、排除什么、与同级节点如何区分",
  "seed_examples": [{"term": "已人工确认的示例术语"}]
}
```

语义卡片可以暂时为空，但缺失会明显降低相近节点的区分能力。不能只写节点名称释义，应结合完整层级路径说明边界。

### 5.4 hierarchical_mapping_prompt_v3.py

模板 Prompt 已去除机械工程专属规则。其他学科应补充少量稳定的学科级判断原则，但不要把零散样例持续堆成大量特例。

模块必须提供：

```text
LEVEL_SYSTEM_PROMPT
build_level_user_payload(...)
```

### 5.5 routing_constraints.py

模板默认全部为无操作规则，不会套用机械工程的公司名、设备、刀具、模具等专属判断。只有经过抽检确认能够覆盖一类稳定错误时，才增加本地规则。

不得直接把 `profiles/mechanical_engineering/routing_constraints.py` 用到其他学科。

## 6. 输入格式

输入为 JSONL，每行一条。最低要求是 `knowledge_point` 或 `name` 至少一个非空。推荐格式：

```json
{
  "id": "唯一 ID",
  "knowledge_point": "知识点名称",
  "name": "知识点名称",
  "definition": "定义",
  "description": "补充描述，可为空",
  "source": "数据来源",
  "clean_decision": "direct_use"
}
```

定义优先于描述；描述只用于补充语境。前置清洗中的 `needs_review`、`drop` 是否进入挂载，应由各学科组自行决定。

## 7. 输出

主输出每行保留原记录，并新增：

```text
knowledge_card
knowledge_labeling
token_usage
```

常见决策：

```text
accepted_leaf
accepted_parent
needs_review
insufficient_evidence
out_of_scope
failed
```

同时生成 `输出文件.report.json`，记录耗时、参数、模型、Token、失败重试和分类统计。报告还会保存本次使用的学科配置路径，便于复现。

## 8. 挂载后置信度分级

```bash
python3 scripts/calibrate_mount_confidence_v2.py \
  --input /path/to/output.jsonl \
  --output-dir /path/to/confidence_output
```

输出五档：

```text
high_confidence_leaf
high_confidence_parent
medium_confidence_mount
low_confidence_mount
not_accepted
```

分级脚本不调用 API，也不改变原挂载路径。

## 9. 新学科上线前验收

1. 分类树 JSON 能解析，所有 code 唯一。
2. 语义卡片 node_code 均能在树中找到。
3. 输入先测 20 条，再测 200 条。
4. 分别抽检叶子挂载、父节点挂载、未挂载和范围外结果。
5. 确认 Prompt 与规则中没有残留机械工程专属词。
6. 保存 profile、输入、输出和 report，保证结果可复现。