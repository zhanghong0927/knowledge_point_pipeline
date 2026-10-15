# 知识点全流程处理管线

当前说明：2026-10-15。将书目筛选、MD质量及结构分类、知识点抽取、清洗、分类树挂载和去重组织成六阶段管线。

## 现行约定

- 辞海与重要书籍独立抽取、独立清洗，使用同一发布版本的项目分类树和边界卡。正常任务只导入固定资产，不按任务重新生成。
- 批量分轮入口采用阶段分离：同阶段整批首轮结束，再进行两轮全局降并发重试，然后进入后续阶段。所有书籍和学科共享模型请求池，不保留固定单书配额。
- 知识点挂载复核由`mounting.review_enabled`控制，默认`false`。关闭时，分类匹配后直接导出、去重；不确定、未接受及技术失败分别保留，不自动放行。
- 分类边界的原审查状态与知识点挂载复核独立。当前资产含降级节点，不能因为导入成功就认为语义质量已验收。
- 两类正式清洗导出均接入保守名称空白修正，去重继续兜底；保留正常英文、单位、型号和公式空格，不推测补写定义、描述或原文。
- 新代码不热更新旧冻结任务。源码、输入、服务或分类资产身份改变后，不能直接重算检查点哈希继续旧任务。

## 六阶段与适用范围

| 阶段 | 主要处理 | 输出与边界 |
|---|---|---|
| 01 书目筛选 | 学科召回、元数据筛选、两轨拆分 | 候选与排除记录分别保存 |
| 02 MD质量及结构分类 | 核对MD质量、结构类型与准入证据 | 辞海默认只准入`entry_prose`；重要书籍处理N1/N3 |
| 03 抽取 | 辞海读取完整MD分块抽取；重要书籍支持规则或独立LLM分支 | 已验证子集与未解决项分开，partial不等于全部成功 |
| 04 清洗 | 名称、学科、正文及对应关系核验；通过项导出时修正安全空白 | 原文证据、ID和来源保持可追溯 |
| 05 挂载 | 导入固定项目资产，分类匹配，按配置决定是否复核，再导出 | 分类路径、层级和得分仍需满足条件 |
| 06 去重 | 同学科、同完整主路径内按名称及已核查空白变体比较 | 保留记录、移除记录和验证报告分别保存 |

当前接收已有MD；PDF可用于版式辅助。本项目不承担PDF转MD或自动修复OCR，不处理重要书籍N2/N4/N5。名称型抽取可能没有定义、描述，不能据此称原书正文缺失。

## 安装

建议Linux、Python 3.12，执行端不要求本地GPU；模型服务独立部署。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

调度依赖见`requirements-scheduling.txt`。重要书籍独立LLM分支使用`modules/important_llm/requirements.lock.txt`和单独环境，避免与主环境依赖冲突。实际输入、模型地址和运行目录需自行配置，仓库不包含生产书目、原始MD和旧检查点。

## 选择入口

### 单配置六阶段

复制`configs/pipeline.example.json`为忽略提交的`configs/my_run.local.json`，填写真实输入、模型和新运行目录。重要书籍规则分支设置`track: important`、`important_extraction: rule`；独立LLM分支参考其专用配置。

```bash
python -B pipeline.py plan --config configs/my_run.local.json
python -B pipeline.py check --config configs/my_run.local.json
python -B pipeline.py run --config configs/my_run.local.json
```

`plan`只输出计划；`check`检查本地文件和交接，不探测API；`run`执行任务。单配置入口不等同于批量入口的全局三轮调度。

### 批量全局分轮（0611受控入口）

```bash
python -B staged_benchmark.py plan --gpu-cards 16 --limit 512 \
  --front-limit 512 --extraction-limit 512 --model-limit 512 \
  --tracks dictionary important --target-per-track 30
```

该参考参数对应512 → 128 → 32；前置、抽取及后续模型阶段的额度可分别配置。它不是最优并发或实际卡数的证明；未显式传参时CLI默认前置64、抽取128、后续模型128，应以`plan`输出为准。CPU准备池与模型请求额度分开。

运行前需建立独立冻结根目录，包含`code/`、`inputs/`、`SERVICE_PREFLIGHT.json`及`CODE_SNAPSHOT.json`。候选文件必须带可核验的原0611书目行证据，普通`books.example.json`不能替代。入口保留`benchmark_support.SOURCE_0611`的原书目路径约束；更换部署时需核对该约束，不是任意JSON批量接口。服务配置需包含`api_url`、`model`、`configured_context`。

```bash
python -B /absolute/new_run/code/staged_benchmark.py snapshot-code --root /absolute/new_run
python -B /absolute/new_run/code/staged_benchmark.py run --root /absolute/new_run \
  --dictionary /absolute/new_run/inputs/dictionary_candidates_verified.json \
  --important /absolute/new_run/inputs/important_candidates_verified.json \
  --gpu-cards 16 --limit 512 --front-limit 512 --extraction-limit 512 --model-limit 512 \
  --target-per-track 30
```

重要书籍独立目标可设100，但必须仅选`--tracks important`。辞海及混合任务仍受30本目标限制。并发标定是显式可选输入；内容证据校验与性能档位选择分离，未降低原有内容要求。

## 固定分类资产

唯一默认入口为`project_assets/LOCK.json`，当前版本`taxonomy-boundaries-v1-20261015`，发布土木、电气、机械、医学、交通五科，共8,704节点，保留40个空边界标记。

新任务记录版本、LOCK及树/卡片指纹。未知学科、缺文件或哈希变化会拒绝执行，不自动生成或改用其他学科。边界维护须单独生成、验证并发布新版本；低层维护入口需要显式`--asset-maintenance`。

项目文件按字节参与校验；保留`.gitattributes`，避免Git换行转换改变源码、资产及模块指纹。

## 重试、接续与结果状态

- 抽取识别408、429、服务端故障及超时等技术失败；`/tokenize`有有限重试。挂载JSON解析和已识别结构异常纳入原第二、三轮，不增加第四轮。
- 有效的未接受、分类不确定等语义结果不按技术失败反复请求；持续服务故障或多轮格式失败仍可能留下未解决项。
- 已确认服务注销的404仅通过有证据的名单专项恢复，不将所有404统一改成可重试。
- `pipeline.py --resume`用于同一身份下核验并复用已完成任务；`--retry-failed`显式重试失败任务，实际可恢复范围仍取决于原生模块。
- `staged_benchmark.py run`要求新任务目录，没有任意跨版本、跨目录的一键resume能力。旧任务应保留冻结代码，或使用明确适配并留有迁移证明的恢复入口。
- 技术结束、partial、技术失败、语义验收分别报告；`finished_with_unresolved`不等于全部通过。

详见[运行与断点恢复](docs/运行与断点恢复.md)和[辞海独立抽取恢复](docs/dictionary_recovery.md)。

## 验证

```bash
python -B validate_offline.py
```

此命令运行离线接口、计划及本地处理检查，不能替代全部原生套件或真实模型验收。版本验证范围见[验证说明](docs/验证说明.md)，本轮改动见[变更记录](CHANGELOG.md)。

## 文档与目录

| 位置 | 内容 |
|---|---|
| `pipeline.py`、`staged_benchmark.py` | 单配置入口、批量分轮入口 |
| `adapters/`、`modules/` | 交接适配器与原生处理模块 |
| `project_assets/` | 固定版本分类树和边界卡 |
| `configs/`、`examples/` | 配置模板和显式运行示例 |
| [模块与数据流转](docs/技术说明_模块与数据流转.md) | 字段、模块职责和状态口径 |
| [分轨清洗与挂载合流](docs/辞海线路分离与挂载合流.md) | 两类数据的清洗、来源及同学科合流 |
| [历史文档](docs/history/) | 旧版说明，仅作历史证据 |

原生模块随包README和带日期的历史测试保留原样；涉及总管线当前行为时，以本README、当前配置与固定资产LOCK为准。`inputs/`、`runs/`、`validation_logs/`及本机配置不提交Git。
