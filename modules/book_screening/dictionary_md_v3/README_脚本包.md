# 通用书籍 MD 质量筛选脚本包

## 文件

- `dictionary_md_audit.py`：主脚本，支持 `prepare`、`run`、`challenge`、`materialize`、`all`。
- `priority_workbook_export.cjs`：重点问题 Excel 导出器。
- `evaluate_pass_challenge.py`：PASS 反证复核回归统计。
- `audit_config.example.json`：多学科输入目录配置示例。
- `定性知识可用性模式_20260907.md`：判断口径和运行命令。
- `test_*.py`：单元测试。

主脚本只使用 Python 标准库。生成重点问题 Excel 需要 Node.js 及 `xlsx` 包；没有该环境时使用 `--skip-priority-workbook`。

## 推荐运行

完整运行第一轮筛选和 PASS 反证复核：

```bash
python3 dictionary_md_audit.py all \
  --root /path/to/input \
  --config audit_config.example.json \
  --output /path/to/new_output \
  --api-url http://your-service/v1/chat/completions \
  --model /path/to/model \
  --knowledge-value-mode --pass-challenge \
  --workers 16 --parallel-subjects 3 \
  --max-tokens 5500 --timeout 240 --retries 2 \
  --skip-priority-workbook --symlink-classified
```

复用已有第一轮结果时，只运行 `challenge`，随后执行带 `--pass-challenge` 的 `materialize`。详细命令见 `定性知识可用性模式_20260907.md`。

运行测试：

```bash
python3 -m unittest discover -p "test_*.py" -q
```
