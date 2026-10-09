# 挂载管线包（2026-09-29）

基于12学科辞海实际运行的脚本打包。主链路：分类树语义边界生成 → 原挂载合理性判断 → 不合理/不确定词条带边界重挂 → 重挂结果模型复核。

## 运行环境
推荐 Linux / WSL，Python 3.10+。主入口使用 Linux 文件锁，不能直接在原生 Windows 执行。先运行 `python3 pipeline/runner.py --help` 检查导入。
模型接口需兼容 `/v1/chat/completions`。本包不含凭据、全量知识点、历史断点或正式交付上传功能。

## 输入目录
将知识点及分类树放在同一数据根目录：
```
data/dictionaries/{subject}/任意包含knowledge的名称.json
data/taxonomy/{subject}_taxonomy.json
```
知识点支持 JSON 数组和 JSONL。每个学科目录只能有一个匹配知识点文件。经济学目录名 economy，分类树文件为 economics_taxonomy.json。
默认12学科清单见 `pipeline/runner.py` 中 SUBJECTS，可按任务修改。先用一个学科的小样本和独立输出目录试跑。

## 主入口（串行学科，阶段内部并发）
```bash
export KNOWLEDGE_DATA_ROOT=/absolute/path/data
export KNOWLEDGE_ENDPOINT_OVERRIDE=http://your-model-host:8000
export KNOWLEDGE_MODEL=your-served-model-name
export KNOWLEDGE_REVIEW_WORKERS=1024
export KNOWLEDGE_MOUNT_WORKERS=1024
python3 pipeline/runner.py init --out /absolute/path/new_run
python3 pipeline/runner.py run --out /absolute/path/new_run
```
语义边界阶段默认 workers=1024，见 runner.py 的 cards_for。实际并发受同层兄弟分组、父子依赖及接口能力约束。模型名称与接口必须按实际服务修改，原默认值只是历史配置。

## 单独运行生成器或挂载器
```bash
python3 pipeline/generate_semantic_boundaries.py --help
python3 mapping_runtime/scripts/hierarchical-knowledge-labeling-beam-v3.py --help
```
挂载器详细参数和 profile 结构见 mapping_runtime/README.md，通用模板在 profiles/template。生成的语义卡片以父节点边界为约束，联合区分同级节点。复核按完整路径是否合理判断，不要求唯一最优路径，也不额外依据传统学科范围排除词条。

## 输出与续跑
每科保存 boundaries*、audit、remount、review、details.jsonl、summary.json、done.json。reasonable 为模型判定合理；uncertain、unreasonable、not_mounted、technical_failure 应分别统计。done 仅表示阶段结束，不能直接视为最终交付通过。
同一输入可用相同 run 命令续跑；输入、提示词或卡片变化需使用新输出目录。run 不自动重新处理已有 done.json 的学科。

## 历史辅助脚本
pipeline 内保留原并行调度、共享限流、边界补跑及失败复核脚本，供追溯和按需使用。它们仍可能引用原开发机路径或历史学科清单，不能直接照搬执行；新环境建议从上述主入口开始。
本包仅对 runner 主入口作相对依赖路径、模型/数据目录配置以及关闭历史复用的移植，不改变提示词与模型判断规则。没有把人工审核标记、名称清洗 DROP、去重或 S3 交付混入挂载结论。

## 验证范围
打包时检查 Python 语法和核心命令 --help；没有调用模型重跑全量，也没有验证新服务端的准确率和速度。SOURCE_MANIFEST.json 记录来源及原始哈希，FILES_SHA256.json 记录包内文件哈希。
