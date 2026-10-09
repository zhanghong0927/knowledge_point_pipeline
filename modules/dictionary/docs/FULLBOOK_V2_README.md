# 全书模型抽取 v2：结构化输入、原文回取

## 本版范围

吸收参考项目的结构化分块、名称/正文证据分离、缓存身份和执行统计思路；不复制它的释义生成、名称补全、书内语义合并。默认仍是 100000 token 总上下文预算，包括提示词与输出；短书整书提交，长书按完整结构单元拆分并覆盖全书。

流程：完整 MD → Markdown 结构单元 + 已有 PDF 格式缓存 → 模型选择原词头及原正文范围 → 程序回取并验证 → 跨块冲突检查 → 待后续清洗的候选。

## 改进

1. 使用 markdown-it-py 解析标题、段落、表格、代码等。正常结构不在中间截断；独立 `$$` 公式块包括空行保持整体。原文拼回校验保证不丢字。非标准/OCR Markdown 不保证识别其真实结构。
2. 模型只返回名称与正文的源文选择；输出独立 `name_evidence`、`body_evidence`。名称存在不能代替解释证据，不改名、不翻译、不生成定义。
3. 同书独占锁；原文、代码、提示词、模型、预算、格式缓存内容、解析器版本参与运行身份。成功缓存重新验证，不能混用不同版本。
4. 响应先落盘；已落盘响应在恢复时可重新验证，无需再发请求。写盘错误不触发模型重试。网络错误仍可能已在服务端执行，日志不能证明未计费。
5. 每次执行单独事件与汇总：实际请求、缓存复用、请求耗时、输入/输出 token、未知用量、技术及结构错误。各执行分别保存，不能重复累加当前 SUMMARY 和历史 SUMMARY。
6. 检查所有跨块源文范围冲突，风险项 `eligible_for_name_screening=false`，不伪装成通过。

## 安装与命令

Python 3.10+：

```bash
python3 -m pip install -r requirements_fullbook_v2.txt
python3 -m unittest -v test_fullbook_llm_v2
python3 fullbook_llm_v2.py \
  --manifest books.json --out /mnt/nas2/path/new_v2_run \
  --api-url http://YOUR-ENDPOINT --model YOUR-MODEL \
  --context 100000 --workers 1024
```

开发机已将依赖隔离放在本工具目录 `deps/`，无需系统安装：

```bash
PYTHONPATH=/mnt/nas2/home/wangqiyuan/book_structure_classification_runs/20260928_fullbook_llm_v2/deps \
python3 fullbook_llm_v2.py --manifest books.json --out /mnt/nas2/path/new_v2_run \
  --api-url http://YOUR-ENDPOINT --model YOUR-MODEL
```

清单与 v1 相同：

```json
[{"identifier":"book001","title":"书名","md_path":"/mnt/nas2/path/book.md","pdf_evidence_path":"/mnt/nas2/path/book.json.gz"}]
```

PDF 路径字段指现有对齐缓存，不是 PDF 本身；可省略。本版不重新解析 PDF。每个结构单元汇入其覆盖行的字体、粗斜体、字号比例等注释，SUMMARY 记录实际格式覆盖，不能将部分缓存称为全部正文均有 PDF 辅助。

`--workers` 默认 1024，为所有书籍共享的分块任务/模型请求并发上限。`--book-workers` 默认 2，控制同时展开的书籍数。同书不同块并行，动态拆分后的子块重新入队，最后统一排序和冲突检查。实际请求数受可用分块数限制，不为吃满并发额外拆小块。`CONCURRENCY.json` 记录当前请求数及本进程峰值。`--overlap` 默认 4 个结构单元，预算不足时只缩减辅助邻文，不删除核心原文。

仅调度变更可经审查后使用 `--resume-scheduler-sha256 OLD_CODE_SHA256` 迁移断点：除指定入口代码哈希外，其余原文、提示词、模型、预算、格式缓存等身份必须完全一致。迁移证据保存于 `SCHEDULER_MIGRATION.json`，所有成功缓存仍重新校验。不要用此选项绕过抽取逻辑变更。

`--tokenizer` 可指向匹配模型的本地 tokenizer（需 transformers），否则 UTF-8 字节数保守估算，通常不能吃满真实 10 万 token。服务声明上限低于预算会拒绝启动，未知上限需先核实，再提供 `--server-context`。

## 输出和失败

- 每书目录名是 identifier 哈希；`INPUT.json` 可反查书名、路径和运行身份。
- `units.json` 保存结构单元、原始字符位置、标题路径和格式注释。
- `entries.json` 包含 knowledge_point/name/head/raw_content、来源及独立前后邻文；不拆定义解释。
- `chunks/` 保存全部叶子区间状态；覆盖校验包含失败区间，不能用成功片段伪装全书完成。
- 单个结构超过预算，不强拆表格或公式，记 `oversized_structure`，整书 partial。
- 技术或结构响应错误最多一次重试；输出截断/未扫描完可拆为子块。单个结构仍装不下时留作失败，不无限重试。
- 最终失败断点不自动重新调用。调整后新建运行目录，不能删除活跃锁。崩溃遗留 `.running` 必须先核对 PID/主机后手动移除。
- `executions/<id>/events/`、`SUMMARY.json` 保存本次事件和统计。异常中止可能只有事件没有终态汇总，应视为未完成；响应文件已落盘不代表语义正确。

`completed` 仅表示全部源单元完成技术处理；`body_complete` 是模型声明，不是独立质量结论。长篇正文仍可能超过相邻上下文。原名与原正文都能逐字回查，也不等于两者对应正确。仍需实书抽检，不承诺召回率或准确率。

## 文件

入口 `fullbook_llm_v2.py`；复用 v1 的源文锚点验证、模型通信及模型能力检查，需一并保留 `fullbook_llm_extract.py`、`model_range_crop.py`。测试依赖 `test_fullbook_llm_extract.py`。`probe_fullbook_v2.py` 为本开发机验证辅助脚本，含固定旧试跑清单路径，不是通用入口。

本版不改变旧版脚本及在跑任务。验证后再决定是否扩大到全量。
