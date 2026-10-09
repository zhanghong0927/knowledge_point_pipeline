# 接入示例

以下样例直接取自2026年10月8日最新 zj `qwen3.8-max` 实测中已完成的书籍，使用 `md-v3.2` 协议，记录、候选和证据字段未作改写。输入书籍来自原120本书目。

|示例|书籍|知识点|原始记录|
|---|---|---|---|
|中文|《滨海拦污设施水动力响应试验研究》|狄克逊准则|[chinese.json](records/chinese.json)|
|英文|《Variable Features on Mars, 2, Mariner 9 Global Results》|dark collar|[english.json](records/english.json)|

两份记录均实际包含 `support_reason`、`definition_supported`、`confidence`，分别展示有来源的数值要求及保留假说语气的释义。它们用于展示当前格式与回源，不是人工语义质检金标准。

- `books/chinese.md`、`books/english.md`：覆盖相应证据的连续原文节选，保持原文文字。
- `books.json`：可直接提交的节选清单，路径相对于该文件；使用独立sample身份，不冒充整本运行。
- `records/<language>.json`：原始最终Record，保留原书book_id/run_id/record_id。
- `evidence/<language>.units.json`：记录及候选引用的原始单元；行号属于原书，不属于节选文件。
- `candidates/<language>.jsonl`：对应原始候选，展示名称和证据的关系。
- `provenance.json`：原书名称、NAS路径、原文件哈希、run_id、记录行号、模型/协议/源码版本、节选行范围和节选哈希。
- `check_installation.py`：离线分词/回拼/Schema 检查，不访问服务。
- `run_python.py`：真实服务 Python API 接入入口，运行前配置凭据。

在包根目录运行 `python examples/check_installation.py`。真实调用执行 `python examples/run_python.py --services .llm_services.json`；批量入口、验证及恢复见 [安装与运行](../docs/handoff/安装与运行.md)。

真实输出来自整本运行，节选重跑不保证生成相同条数、分组、释义或ID，不能当作逐字回归的预期结果。节选中的图片链接等原样保留，本模块不下载图片。此处不是完整run目录，不能交给validate_runs.py冒充整本验收。模型输出的语义质量仍需独立检查；不要自行改写样例来掩盖问题。
