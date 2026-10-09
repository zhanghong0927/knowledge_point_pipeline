# 通用辞海知识点管线工具包

版本：2026-10-08。主要用于辞海、词典、百科等工具书，从原书生成新知识点；不是已交付数据的人工质检收尾工具。

## 流程与版本

| 阶段 | 包内入口 | 输出 |
|---|---|---|
| 书目与MD质量筛选 | screening/dictionary_md_audit.py | 书目PASS / REVIEW / DROP及技术失败 |
| 主体结构分类 | classification/scripts/classify_books.py | 主体类别、词头位置、格式特征及证据 |
| 全书MD与PDF辅助格式准备 | portable_pipeline.py prepare | 全MD行缓存、PDF格式注释、books.json |
| 全书模型抽取 | src/run_fullbook_v5.py | 原名、原始正文、前后独立上下文、原文位置 |
| 名称格式筛选 | name_filters/01_name_title_quality.py，v22 | 名称格式处理与知识点名称判定 |
| 学科范围筛选 | name_filters/02_subject_scope_filter.py，v9 | 按各书原属学科判断 |
| 正文清洗、定义分离、对应验收 | src/clean_boundary_v46.py，由portable_pipeline.py clean调用 | 标准字段、内容溯源、排除和待定清单 |

分类为抽样判定，不是模型逐页读整书；抽取则处理每本书的完整MD，通过结构分块和预算拆分运行，不只读分类样本。

主体类型：entry_prose（词条＋连续正文）、fixed_fields（固定字段）、text_commentary（原文＋评注），另有other、review、technical_failed。组织方式O1-O4和格式特征是独立标签，不等于四套抽取脚本。当前全书模型抽取入口不依赖旧规则候选，也不按书名定制规则。

**不含：分类树挂载、同分支去重、人工质检结果合并、NAS/S3正式发布。**重要书籍的一般章节概念抽取也不等同于本包的独立词条抽取，不能直接宣称已全面适配。

## 原文与字段约束

- MD是唯一正文文本依据；PDF只辅助粗体、斜体、相对字号等语义格式。支持内容确实为PDF的.bin。PDF截图、普通换行坐标不会输入模型。
- PDF全书文字扫描不等于每行都成功对齐。PREPARED.json报告实际格式覆盖，未匹配部分不编造格式。非PDF原始文件可记录为MD-only；本包不代替原始文件转MD的OCR/EPUB解析器。
- 抽取只产生原文名称与raw_content，**不在抽取阶段分定义、解释**。leading_context、trailing_context不拼入正文。
- knowledge_point是原书英文名，name是原书中文名；原书没有对应语言就为空。不补译、不改含义、不根据外部知识修复。
- 名称双层通过后，正文模型只选择可回查的原文片段并做格式处理，再分离定义、解释并验收对应关系。格式清理不是改写。
- 当前v46实际输出的description/en_description是选中的解释正文，可能包含definition/en_definition；本包不另外做语义合并或强行删除重叠。
- 原文可靠词头可以没有定义或解释；name_only单独统计。若业务只接收有正文项，应按该统计另外筛选，不把空正文等同于技术失败。
- source保存书目identifier、title/book_title、MD路径与哈希、词头与正文字符范围、清洗内容回溯。ID沿用抽取结果，不在打包适配中重编。
- 技术校验成功不等于语义合格，也不等于人工审核。结果必须抽检，错误和待定不自动放行。

## 环境与输入

推荐Linux，Python 3.10+。先在自己的虚拟环境安装依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

如果使用本地模型tokenizer，另安装requirements-tokenizer.txt，并用--tokenizer提供实际目录。未提供时抽取调用服务根地址的/tokenize，按与推理一致的聊天模板计算实际token，并预留输出和安全余量；计数服务失败按技术失败处理，不回退为字节估算或内容DROP。相同请求的计数在进程内缓存。

复制examples/books.json填写新书单。每本必须有identifier、title、md_path；提供pdf_path可准备全书辅助格式。后续清洗还必须明确subject_slug与scope_config，不能从路径或书名臆测学科。所有文件路径是运行机器上的绝对路径。

scope_configs/包含20份通过v9格式校验的学科配置，是可替换组件，不保证覆盖任意学科。另保留1份心理学旧书目筛选配置于reference_only/，它缺少v9要求的学科定义与L1边界，不能直接用于词条筛选。缺失或校验不通过必须补配置，不能变成内容DROP。使用前检查L1纳入排除标准是否符合目标学科，不能因书籍属于学科就放行所有词条。

模型地址与model ID填写examples/model_config.json；不含密钥。分类模块还依赖服务/tokenize、/v1/models、/v1/chat/completions以及vLLM式chat_template_kwargs。现有分类和正文清洗HTTP层面向无鉴权内网服务；不是任意OpenAI兼容服务都能直接运行。需要鉴权时先适配HTTP层，不能将密钥填进书单。

100000是提示词、输入、输出预留一起计算的上下文总预算；不是输入10万token再加输出。必须核实部署真实窗口，并填写服务实际公布的模型ID。

## 分阶段运行

在解压根目录执行。以下/path仅是示意；运行目录由你指定，原书与旧结果不会被覆盖。

### 1. 书目与MD质量筛选

筛选规则、学科目录配置和具体参数见screening/README_脚本包.md及其--help。必须显式传root、output、config、api-url、model，避免使用旧脚本内置历史路径。

```bash
python3 screening/dictionary_md_audit.py all --root /path/books_root --output /path/screening_run --config /path/audit_config.json --api-url http://MODEL_HOST:8000/v1/chat/completions --model EXACT_MODEL_ID --pass-challenge
```

筛选模型的实际请求路径请按screening/README_脚本包.md核对；该模块配置与后续模型根地址不是同一字段约定。按PASS及技术失败清单整理正式books.json，不把失败当合格或不合格内容。

screening/priority_workbook_export.cjs是可选的Excel汇总工具，额外需要Node.js和xlsx包；Python筛选本身不依赖它。

### 2. 分类

```bash
python3 classification/scripts/classify_books.py prepare --books /path/books.json --out /path/classification_prepared
python3 classification/scripts/classify_books.py run --base /path/classification_prepared --config /path/model_config.json --out /path/classification_run --workers 16
python3 classification/scripts/classify_books.py verify --out /path/classification_run
```

other、review和technical_failed分别处理；分类标签不会自动删改抽取书单。确认需要处理的书目后再向后传入，避免无声跳过困难书目。

### 3. 全书格式准备与抽取

```bash
python3 portable_pipeline.py prepare --books /path/books.json --out /path/source_prepared
python3 src/run_fullbook_v5.py --manifest /path/source_prepared/books.json --out /path/extraction_run --api-url http://MODEL_HOST:8000 --model EXACT_MODEL_ID --context 100000 --server-context 100000 --book-workers 32
```

只有已核实部署长度后才能使用--server-context 100000。默认运输轮次并发为1024→256→64；后两轮只重试HTTP504。其余失败不会无限重跑。轮次切换依赖/metrics；不可访问或无法确认队列状态时会阻塞，不会擅自扩大服务权限。详情见docs/FULLBOOK_V5_GUIDE.md。

若历史504需要最后16并发补跑，包内保留src/fullbook_llm_v5_retry16.py独立入口；检查--help后在相同冻结书单和输出下手动运行，不能与仍在写入同一目录的进程并发。不得把partial称为completed。

### 4. 生成清洗输入

```bash
python3 portable_pipeline.py clean-prepare --books /path/source_prepared/books.json --extraction /path/extraction_run --out /path/clean_prepared
```

只读取已结束书籍的accepted_entries.json，并校验原文回取、哈希和ID；隔离候选写EXCLUDED.json。partial书的已完成词条允许进入，但来源状态保留并单独统计，不能据此声称该书完整抽取。

### 5. 名称双层、正文与验收

```bash
python3 portable_pipeline.py clean --books /path/source_prepared/books.json --input /path/clean_prepared/INPUT.json --config /path/model_config.json --out /path/clean_run
```

默认workers=1024。实际并发受记录数量、名称批大小8及队列状态限制，不保证1024请求一直跑满。

顺序固定：v22名称初判（无正文）→仅review补600字符一次→v9学科初判（无正文）→仅review补600字符一次→v46解释选择、定义分离和对应验收。技术失败单独保存，不以DROP冒充成功筛选。

## 结果与续跑

- FINAL_RECORDS.jsonl：当前所有阶段通过的记录。
- STANDARD_RECORDS.json：id、knowledge_point、name、definition、en_definition、description、en_description、source；不含tag字段。
- DISPOSITIONS.jsonl：每条经过了哪些步骤，以及keep/drop/review/技术状态。
- REVIEW.jsonl、TECHNICAL_FAILURES.jsonl：未决问题，不能混入正式通过统计。
- SUMMARY.json：各阶段数量、耗时、name_only及待定/失败数量。
- STATE.json：运行阶段和finished/finished_with_pending，不代表人工质检通过。
- calls/与原文trace：模型输入输出和原文选中范围。

同一输出目录续跑要求输入、代码、配置、原文和边界配置不变。配置改变要开新目录。先停旧进程，再续跑；不要删除活进程锁。已经冻结的失败结果保留供诊断，不会自动无限重试。

## 校验与限制

```bash
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
PYTHONPATH=classification/scripts python3 -m unittest discover -s classification/tests -v
python3 -m unittest discover -s screening -p 'test_*.py' -v
python3 verify_package.py
```

也可用python3 validate_offline.py --out /path/offline_validation一并运行离线测试、入口和学科配置校验；它不调用真实模型服务。

本次交付只做离线依赖、入口、原文衔接、核心回归和ZIP校验，不启动新模型批次，也不承诺所有书籍结构都能准确抽取。抽检需要对照原文，至少检查词头完整性、内部标题误入、串条、句中截断、双语名称字段及定义对象。

部分原测试需要历史抽检数据，缺少数据时会明确skip，不算通过。v42旧样本测试保留在docs/historical_tests/，不进入可移植离线测试集。docs/保留原版参考说明；其中历史批次路径和命令不作为通用入口，以本文件的命令为准。

MANIFEST_SHA256.json固定包内文件，SOURCE_PROVENANCE.json标记复用核心、适配入口及原始筛选包。VALIDATION.json记录本次实际验证结果。
