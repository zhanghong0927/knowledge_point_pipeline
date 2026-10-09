# 重要书籍版型分类与知识点抽取模块（v2.0.1）

本目录是可独立交付的最小模块。它先调用模型对**非辞海类书籍**做书籍级版型分类，再对判为 N1 或 N3 的书籍做**本地规则抽取**。交付代码只包含 **N1、N3 两个抽取器**；N2、N4、N5 仅作为 `OTHER` 的分类子类型出现，**没有对应抽取器，也不会自动抽取**。

## 1. 能做什么、不能做什么

| 分类主类 | 判定对象 | 交付模块的处理 |
| --- | --- | --- |
| `N1` | 章节·多级标题型，标题可稳定划定知识单元 | 调用 `n1_heading_extractor.py` 抽取标题知识点，并执行标题过滤 |
| `N3` | 条列·步骤规则型，步骤、条件或规则是正文主体 | 调用 `n3_rules_extractor.py` 抽取有原文依据的名词性名称 |
| `OTHER` | 可靠判为其他版型 | 记录 `other_subtype`：`N2` 连续叙述、`N4` 表格对照、`N5` 图文解析；不抽取 |

证据不足、正文损坏或范围外辞海/词典式词条标为 `needs_review`；模型请求或结果校验失败标为 `technical_failed`。两者均不等于 `OTHER`。这是**书籍级**分类，不是逐页或逐章分类；`verify` 只能核查结构和证据可追溯性，不能替代人工准确率评估。

## 2. 交付文件与代码职责

```text
交付模块/
├─ README.md
├─ VERSION                    # 2.0.1
├─ module.json                # 模块入口与输入输出声明
├─ requirements.txt           # PyMuPDF、jieba
├─ scripts/
│  ├─ layout_model_validation.py  # 分类、校验、N1/N3 路由；唯一管线入口
│  ├─ n1_heading_extractor.py     # N1 标题/层级/过滤/Index 规则
│  └─ n3_rules_extractor.py       # N3 条列与规则名称提取
├─ examples/
│  ├─ books.json              # 输入书目占位示例
│  └─ config_layout.json      # 模型服务占位配置
└─ tests/                     # N1、N3 规则及交付路由测试
```

`layout_model_validation.py` 的主要接口：

| 接口 | 作用 |
| --- | --- |
| `run_module(books, config, out, workers=4, radius=8, max_line_chars=700)` | 一次完成准备、模型分类、证据校验及 N1/N3 抽取；推荐接入点 |
| `prepare(books_path, root, pdf_root, out, radius, max_line_chars)` | 为分类准备 MD 采样窗口、可选 PDF 版面信息和来源哈希 |
| `run(base, out, config_path, workers)` | 检查模型 ID、调用模型、保存逐书分类并复验结果 |
| `verify(out)` | 不调用模型，复验已保存的分类与来源一致性 |
| `route_extractors(prepared_dir, classification_dir, output)` | 按成功分类结果调用 N1/N3 抽取器；OTHER 等状态仅记录路由 |

`module.json` 中的 `module_id` 为历史兼容标识 `book_layout_classifier_n1_n5`；**它不表示交付包含五个抽取器**。实际自动抽取范围以本 README 和 `route_extractors()` 为准。

## 3. 运行环境

- Windows PowerShell；Python **3.10+**。源文件按 UTF-8 读取，输出 CSV 使用 UTF-8 BOM，便于中文 Excel 打开。
- `PyMuPDF` 用于可选 PDF 版面信息；没有 PDF 也可仅用 MD 分类。
- `jieba` 用于 N3 名称的中文词性筛选；N1 抽取本身不依赖 jieba。
- 模型服务需兼容 `GET /v1/models` 与 `POST /v1/chat/completions`。配置中的模型 ID 必须与 `/v1/models` 返回值一致。

在**交付模块目录**运行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Resolve-Path '.\scripts').Path
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

本模块不附带模型权重、模型服务、书籍 MD/PDF 或 `.venv`。交付测试用本地模拟数据，不要求模型服务在线。

## 4. 输入格式和配置

输入书目是非空 JSON 数组，结构如下。运行前把示例路径换成真实书籍：

```json
[
  {
    "identifier": "book_001",
    "title": "示例书名",
    "md_path": "D:/books/MD/book_001.md",
    "pdf_path": "D:/books/PDF/book_001.pdf"
  }
]
```

| 字段 | 要求 |
| --- | --- |
| `identifier` | 必填；本批次唯一；只含 ASCII 字母、数字、`_`、`-`；用于结果文件名 |
| `md_path` | 必填；存在的 UTF-8/UTF-8 BOM `.md` 文件；模型证据和抽取行号均以它为准 |
| `title` | 可选；显示书名，默认 MD 文件名 |
| `pdf_path` | 可选；匹配的 PDF。仅提供格式辅助，不允许代替 MD 原文证据 |

直接调用 `run_module()` 时请使用**绝对路径**。分步 `prepare --books` 可用相对路径，此时相对书目 JSON 所在目录。`prepare --root` 可递归发现 MD，配合 `--pdf-root` 寻找对应 PDF。

模型配置示例，保存为 `config.local.json` 后填入实际服务地址及模型 ID：

```json
{
  "api_url": "http://MODEL_HOST:8000",
  "model": "EXACT_MODEL_ID",
  "context_limit": 32768,
  "max_tokens": 4000,
  "timeout": 120,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

需要鉴权时添加 `"api_key_env": "LAYOUT_MODEL_API_KEY"`，并在进程环境中设置该变量。**不要**在 JSON 中写 `api_key`，代码会拒绝明文密钥字段。`api_url` 带或不带末尾 `/v1` 都可。`context_limit`、`max_tokens` 和 `timeout` 应按实际服务能力设置。

## 5. 推荐接入方式：一次运行

将下面示例保存为交付模块目录中的 `run_local.py`；先准备 `books.local.json` 和 `config.local.json`，再运行它：

```python
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here / "scripts"))
from layout_model_validation import run_module

books = json.loads((here / "books.local.json").read_text(encoding="utf-8"))
config = json.loads((here / "config.local.json").read_text(encoding="utf-8"))
result = run_module(
    books=books,
    config=config,
    out=here / "runs" / "batch_001",
    workers=4,
    radius=8,
    max_line_chars=700,
)
print(result["primary_classes"])
print(result["extraction"]["knowledge_points_extracted"])
```

```powershell
.\.venv\Scripts\python.exe .\run_local.py
```

`out` 必须是**不存在或空的目录**。`run_module()` 默认 4 个并发模型请求；准备阶段按前、中、后窗口采样用于**分类**，抽取器随后读取**完整 MD**。它执行的顺序为：

```text
书目 MD / 可选 PDF
  → prepare：采样、PDF 辅助格式、来源 SHA-256
  → run：模型分类、JSON/证据/跨窗口校验
  → route_extractors：N1 或 N3 本地规则抽取；OTHER 等状态跳过
  → 逐书 JSON + 合并 knowledge_points.csv
```

## 6. 分步运行与模型服务恢复后重试

分类和抽取可分开执行。PowerShell 示例（在交付模块目录）：

```powershell
$python = '.\.venv\Scripts\python.exe'
$entry = '.\scripts\layout_model_validation.py'
& $python $entry prepare --books '.\books.local.json' --out '.\runs\batch_001\prepared' --radius 8 --max-line-chars 700
& $python $entry run --base '.\runs\batch_001\prepared' --config '.\config.local.json' --out '.\runs\batch_001\classification' --workers 4
& $python $entry verify --out '.\runs\batch_001\classification'
```

命令行 `prepare` 默认采样半径 24、单行上限 3000，与 `run_module()` 默认值**不同**；上例显式传入 `8/700` 以保持一致。模型预检阶段若返回 404/502，已经准备的 MD/PDF 样本可复用，服务恢复后重新执行相同 `run` 命令。相同配置和输出目录下重跑，会复用已保存且校验通过的逐书分类结果。`verify` 不调用模型。`run` 退出码 1 也可能只是批次中含有部分 `technical_failed`；查看 `classification/summary.json` 和逐书结果判断。

分步命令只做分类；随后调用抽取路由：

```python
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here / "scripts"))
from layout_model_validation import route_extractors

base = here / "runs" / "batch_001"
summary = route_extractors(
    base / "prepared",
    base / "classification",
    base / "extraction",
)
print(summary["knowledge_points_extracted"])
```

`run_module()` 不接受非空输出目录用于恢复；需要断点恢复时用分步方式。改动模型配置、提示词或输入准备集时，分类应写到新目录。仅重抽已分类的 N1/N3 时可复用 v2 分类结果，但抽取应输出到新目录，保留旧结果便于比较。v1 与 v2 准备集格式不兼容，不能直接复用。

## 7. 分类和抽取的实际规则

分类器根据 MD 的正文主体组织方式判断版型，不凭书名或关键词计数。N1 与 N2 的关键边界是标题能否稳定划出知识单元；N3 要求步骤、条件、规则在正文中占主体。OTHER 必须可靠归入一个细分版型。成功分类的 `evidence` 至少有两条实际 MD 行的连续引文；多窗口输入还需跨窗口覆盖。N2/N4/N5 虽无抽取器，分类器仍能记录它们为 OTHER 的 `other_subtype`。完整细分判断依据见主脚本的 `PROMPT` 常量。

**N1：** `n1_heading_extractor.py` 识别 Markdown 标题及编号标题，构建层级与 `parent_path`。输出的 `knowledge_point` 只取当前标题的名称，不拼接父级路径；图片独占行、代码块、前后附属内容不会直接作为知识点。管线会在原始抽取后执行 `filter_result()`：高置信噪声被排除，模糊标题进入待复核，原标题、行号和候选仍留在逐书 JSON 中。显式书末 Index 单独保存在 `index_entries`，**不进入合并知识点 CSV**。

**N3：** `n3_rules_extractor.py` 识别编号条列和规则引导语，从原文中抽取名词性名称；条列层级、条件、例外、原文片段和行号单独记录。不能可靠命名的项目进入逐书 JSON 的 `review_candidates`，不计入确定知识点。N1/N3 的规则抽取都不再次调用模型。

## 8. 输出文件与关键字段

一次运行后的主要目录如下：

```text
runs/batch_001/
├─ prepared/
│  ├─ PREPARED.json                 # 准备成功/失败数量
│  ├─ manifest.json                 # 书目、采样参数、准备文件哈希
│  └─ prepared/<identifier>.json    # 前/中/后样本、行号、可选 PDF 格式
├─ classification/
│  ├─ results/<identifier>.json     # 状态、主类、子类型、原文证据
│  ├─ raw/ 和 sent/                 # 模型原始响应与发送记录
│  ├─ summary.csv / summary.json    # 逐书和全批次统计
│  └─ verification.json             # 保存结果的复验记录
└─ extraction/
   ├─ results/<identifier>.json          # 仅 N1/N3 抽取成功的书
   ├─ results/<identifier>_routing.json  # 每本书都有的路由状态
   ├─ knowledge_points.csv               # N1/N3 合并知识点
   └─ summary.json                       # 抽取数量及逐书路由
```

分类 JSON 的 `status` 是 `classified`、`needs_review`、`technical_failed` 之一。`classified` 才有 `primary_class`；`primary_class=OTHER` 时 `other_subtype` 必须为 N2/N4/N5 之一。混合结构用 `is_mixed` 和 `secondary_classes` 记录，次类及 `evidence.class_code` 仍使用 N1–N5 细分代码。辞海/词典式范围外文本记为 `needs_review` 且 `review_reason=out_of_scope`。

抽取路由状态包括 `extracted`、`extracted_empty`、`skipped_other`、`skipped_needs_review`、`skipped_classification_failed` 和抽取阶段的 `technical_failed`。`knowledge_points.csv` 的主要列有 `identifier`、`title`、`knowledge_point`、`level`、`parent_path`、`heading_line`、`source_line_start`、`source_line_end`；N3 另填写 `item_type`、`marker`、`conditions`、`exceptions`、`source_quote` 等。CSV **没有全局唯一知识点 ID**，若下游数据库需要 ID，应在导入时另行分配。

## 9. 交付验收与已知限制

在交付目录运行上面的 `unittest` 命令，应得到 **30 项测试通过**。测试覆盖 N1 标题及过滤、N3 名称与条件、路由只调用 N1/N3、OTHER 不触发抽取。上线前还应使用真实模型服务、真实 MD/PDF 做一次小批量连通性验证。

v2.0.1 相比 v2.0.0 修复了管线路由漏调 N1 标题过滤的问题；原分类提示词和三主类结构未变。既有历史结果不会自动改写。N1 规则仍可能保留整句标题、目录残留或 OCR 噪声；抽取条数不等于人工确认的有效知识点条数。PDF 格式解析失败会记录异常，但不必然阻断 MD 分类。N2/N4/N5 在本交付模块中仅有分类子类型信息，没有规则抽取能力。
