# 逐书 Markdown 知识点提取模块

将 UTF-8 Markdown 逐书转换为带原文引用的知识点。名称必须有原文依据，释义允许依据同书证据总结改写；语言跟随原文。模块不负责 PDF/EPUB 转换、下游清洗、分类挂载或跨书去重。

交付包包含源码、锁定依赖、当前文档、从120本运行中选出的中英文真实样例、离线回归测试及 Qwen3.8-27B 分词文件；不含 API 密钥、模型权重、整批历史运行结果或历史迁移工具。

## 从这里开始

1. [安装、配置与运行](docs/handoff/安装与运行.md)：环境、服务配置、CLI、Python 调用及离线检查。
2. [管线接入契约](docs/handoff/管线接入.md)：输入、输出字段、状态、回源、清洗交接与幂等恢复。
3. [流程与运维](docs/handoff/流程与运维.md)：各阶段职责、重试、并发、统计、故障处理及当前限制。
4. [示例说明](examples/README.md)：最新zj实测的中英文原文节选、包含来源支持与置信度的原始记录及证据、批量清单、Python 接入和离线自检。

完整的真实提取需要调用方提供可访问的 OpenAI 兼容推理服务及凭据。附带分词文件只能用于对应模型和模板；换模型必须同步更换，不允许估算 token。

## 最短运行路径

以下命令在交付包根目录运行。Windows 用 `.venv/Scripts/python.exe`，Linux 用 `.venv/bin/python`，或激活环境后统一使用 `python`。

```sh
python3.12 -m venv .venv
# 激活环境后：
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
python examples/check_installation.py
```

复制 `llm_services.example.json` 为 `.llm_services.json`，填写服务地址、实际模型 ID，设置配置指定的 `LLM_API_KEY` 环境变量，然后运行：

```sh
python -m book_extractor.cli --manifest examples/books.json --services .llm_services.json --output work/runs --workers 4 --max-connections 4 --transport-failure-limit 2
python scripts/validate_runs.py work/runs
python scripts/summarize_md_runs.py work/runs
```

`complete` 表示流程执行完整，不表示语义质检通过；`partial` 含未完成工作，不能视作全书成功。详细接入规则以以上三份交付文档和 `src/book_extractor/models.py` 为准。

## 维护与打包

```sh
python -m unittest discover -s tests -v
python scripts/build_handoff.py --output dist/book-extractor-handoff-20261008
```

运行测试前设置 `BOOK_EXTRACTOR_TOKENIZER_PATH` 指向包内 `data/tokenizers/qwen3.8-27b` 的绝对路径。打包采用文件白名单；`PACKAGE.json` 列出逐文件 SHA-256。交付包不会复制本地虚拟环境、私密配置或工作目录。未修改的抽取核心保持原有行为；本次整理不承诺已解决所有质量与重试问题。
