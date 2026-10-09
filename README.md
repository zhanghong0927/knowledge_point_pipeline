# 知识点全流程处理管线

版本：2026-10-08。面向多个学科，将书目筛选、书籍质量与结构分类、知识点抽取、知识点清洗、知识树挂载、去重与合并组织成六个阶段。

## 文档导航

- 本README：当前能力、配置、运行方法和恢复约束。
- [技术说明：模块与数据流转](docs/技术说明_模块与数据流转.md)：各模块职责、输入输出、字段与ID契约、状态和统计口径。
- [机械MD全流程测试记录](docs/机械MD全流程测试记录_20261008.md)：本次真实测试逐模块的输出文件、数量、修复和验收。
- [本次最终70条知识点](runs/mechanical_e2e_20261008/important/06_dedup_after_review_retry/retained.jsonl)：复测后的最终结果，不是首轮68条基线。

## 当前状态

本版已解压现有工具包、接入当前两阶段清洗代码，并增加统一计划/调度入口、审核书单转换、辞海分类放行、字段转换、首次挂载及标准导出。**六阶段代码接口已衔接，重要书籍LLM分支已完成机械MD真实小样本联调。** 必须填写真实输入、依赖环境和模型配置后再执行，不能将小样本测试视为全量运行或语义验收。

当前约定：PDF默认符合质量要求，不执行独立PDF质量筛选；保留可选PDF版式辅助。重要书籍仅处理N1/N3，N2/N4/N5暂不纳入开发和抽取范围。

初次整合以离线验证为主；2026-10-08已使用用户部署的Qwen3.8-27B完成机械MD小样本实测，详见 [真实端到端报告](runs/mechanical_e2e_20261008/END_TO_END_REPORT.md)。一份重要书籍完成全流程并得到70条最终记录，另一份辞海资料被审核为Review，未人为放行。离线验证结果另见 `VALIDATION.json`。

| 路线/能力 | 当前验证状态 |
| --- | --- |
| 重要书籍N1，LLM抽取→清洗→挂载→去重 | 已完成1份完整现有MD的真实联调 |
| 重要书籍N1/N3规则抽取 | 接口及本地样例通过；本次真实全流程选择LLM路线 |
| 辞海分类、全书抽取和清洗交接 | 代码已接通、离线验证通过；本次辞海样本停在MD审核REVIEW，未进入后续抽取 |
| PDF质量筛选、N2/N4/N5、低产补抽 | 不在当前处理范围 |

当前接收已解析MD。PDF只作可选版式辅助，不承担PDF/EPUB转MD。源数据范围、筛选口径和模型服务均需要配置；小样本成功不等于全库召回率或人工准确率合格。

**最新串联评估（不调用模型接口）：三个分支的内部输入输出依赖已完整，第5步挂载及标准导出已接通。** 详细记录见 [离线串联报告](validation_logs/offline_flow/report.md)；测试中的模型响应为模拟值，不代表真实服务验证。

## 1. 六阶段流程

```text
原始全量书目 CSV / 已有学科书单
  01 书目筛选：学科召回、元数据初筛、重要书籍精筛、两轨拆分
  02 MD质量审核 → 审核书单转books.json → MD主体结构分类
       辞海轨：entry_prose / fixed_fields / text_commentary 等
       重要书籍轨：N1 / N3 / OTHER / needs_review / technical_failed
  03 知识点抽取
       辞海轨：完整MD分块模型抽取 + 原文范围核验
       重要书籍轨：只放行N1/N3，选择rule本地规则或llm逐书模型抽取
  04 当前清洗流程：字段规范化 → 通用规则 → 模型清洗 → 恢复来源与tag
  05 知识树挂载：整理已有边界 → 首次路由 → 路径复核 → 标准导出
  06 去重与合并：同学科多个输入 → 同完整主路径同名去重 → 验收
```

两条抽取轨道分别运行。合并发生在第6步，按学科汇合，不把所有学科混在一起做去重。一个知识点是否符合学科范围由第4步审核；具体挂载路径由第5步判断，二者不是同一任务。

| 阶段 | 现有模块 | 当前接入情况 | 模型/GPU需求 |
| --- | --- | --- | --- |
| 01 书目筛选 | `modules/book_screening` | 已接原包1–4子阶段 | 学科召回及硬规则本地执行；重要书籍精筛调用模型 |
| 02 质量与分类 | `book_screening` 的5–7子阶段、书单转换适配器、两轨分类模块 | 审核结果自动转书单；不做PDF质量筛选 | 模型远端推理；本地CPU整理书单和读取版式 |
| 03 抽取 | `dictionary`、`important_books`、`important_llm` | 辞海模型抽取；重要书籍N1/N3支持rule/llm二选一 | rule使用CPU；llm使用远端服务和本地tokenizer |
| 04 清洗 | `modules/cleaning` | 当前规则＋模型流程已接入 | 规则本地CPU；模型审核调用接口 |
| 05 挂载 | `adapters/mounting_bridge.py`、`modules/mounting` | 标准输入、知识树适配、首次挂载、复核与标准导出已接入 | 准备阶段本地；挂载和复核调用模型 |
| 06 去重合并 | `modules/dedup` | 已接默认长度优先模式，支持同学科多个输入 | 长度模式本地CPU；原包另有模型质量模式 |

执行端不要求本地GPU，模型可部署在其他机器。模型服务器的显存、窗口与并发需要单独核实。本版不运行BM25或Embedding；这些可作为后续挂载候选召回扩展，不是当前清洗的强制关卡。

## 2. 文件组织和原模块

```text
knowledge_point_pipeline/
├─ README.md
├─ pipeline.py                   # plan / check / run；按阶段调度
├─ validate_offline.py           # 不调用模型的接口图、局部实跑和阻塞检查
├─ requirements.txt
├─ MODULE_MANIFEST.json          # ZIP哈希、解压根目录、清洗代码快照来源
├─ VALIDATION.json               # 本次离线验证记录
├─ docs/                         # 模块技术说明、逐模块真实测试记录
├─ configs/
│  ├─ pipeline.example.json      # 单学科、单轨道配置
│  ├─ pipeline.important_llm.example.json # 重要书籍LLM分支
│  ├─ important_llm_services.example.json # 原生LLM服务/tokenizer配置
│  ├─ taxonomy_registry.json     # 学科别名与树文件映射
│  ├─ model.example.json         # 结构分类模型配置
│  └─ books.example.json         # 已通过前置审核的书单格式
├─ adapters/
│  ├─ normalize_records.py       # 标准化字段/ID、来源侧文件、清洗后恢复
│  ├─ screened_books_to_manifest.py # PASS审核书单转books.json，保存未匹配清单
│  ├─ approve_dictionary_books.py # 分类完成后生成approved_books.json，复核证据与MD哈希
│  ├─ important_llm_io.py        # N1/N3清单转LLM协议，原生交接快照转统一清洗输入
│  ├─ mounting_assets.py         # 知识树格式统一、原节点路径映射
│  ├─ mounting_bridge.py         # prepare / route / review / export
│  └─ important_route.py         # 包装原N1/N3路由函数为CLI
├─ tests/test_integration.py
├─ modules/
│  ├─ book_screening/
│  ├─ dictionary/
│  ├─ important_books/
│  ├─ important_llm/            # r5源码、原说明、锁定依赖、测试及分词器原样复制
│  ├─ cleaning/
│  ├─ mounting/
│  └─ dedup/
├─ inputs/                       # 测试配置、书单及小样本；原始MD通过路径引用
├─ runs/                         # 按学科/轨道/版本独立输出
└─ origin_zip/                   # 原始ZIP归档，保留供来源校验
```

用户清单中重复出现的两个ZIP各解压一次。目录内另外已有挂载和去重ZIP，也纳入本版。

| 原压缩包或本地目录 | 解压/复制位置 | 原说明 |
| --- | --- | --- |
| `book_screening_toolkit_20261008.zip` | `modules/book_screening` | [书目筛选README](modules/book_screening/README.md) |
| `generic_book_knowledge_toolkit_20261008.zip` | `modules/dictionary` | [辞海管线README](modules/dictionary/README.md) |
| `重要书籍交付模块.zip` | `modules/important_books` | [重要书籍README](modules/important_books/README.md) |
| `book-extractor-handoff-20261008-r5` | `modules/important_llm` | [逐书LLM抽取README](modules/important_llm/README.md) |
| `挂载管线_20260929.zip` | `modules/mounting` | [挂载README](modules/mounting/README.md) |
| `final_dedup_toolkit_20261008.zip` | `modules/dedup` | [最终去重README](modules/dedup/README.md) |
| 当前 `Books_textbooks_cleaning_pipeline` | `modules/cleaning` | [当前清洗README](modules/cleaning/README.md) |

ZIP模块解压时保留原README与代码；集成修复另记入MODULE_MANIFEST.json，目前包括重要书籍MD审核书单准备包装及清洗副本修正。原ZIP归档未改动。清洗复制代码、测试、示例、提示词和schema，不复制原模块的历史runs、全量数据或模型权重。包内历史说明可能带旧绝对路径，执行以本说明、显式运行参数和实际入口为准。

重要书籍LLM模块按原 `PACKAGE.json` 白名单复制81个文件，包含约13MB本地tokenizer；不复制源目录的虚拟环境、editable安装元数据、运行数据和凭据。原目录及副本的抽取核心均未改动，适配仅位于外层pipeline/adapters。来源路径、版本和逐文件SHA256见MODULE_MANIFEST.json。

## 3. 环境、配置和安全试跑

推荐Linux、Python 3.10+。挂载和部分分类模块使用 `fcntl`，不是原生Windows全流程。先激活自己的Python环境，再从本目录安装依赖：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

OSS下载依赖、tokenizer依赖分别按需求安装；OCR/EPUB转MD和模型服务不在包内。不要为了运行初版去安装模型权重或启动全量推理。

复制 `configs/pipeline.example.json` 为本地配置并修改：

- `subject`：学科代码、中文名及范围说明。一份配置只处理一个学科。
- `track`：`dictionary` 或 `important`。同学科两轨使用不同运行目录。
- `important_extraction`：重要书籍选择 `rule`（默认）或 `llm`，两种方式都只接收N1/N3；使用不同运行目录进行对比，不自动叠加或按低产触发补抽。
- `paths.run`：新的运行目录，两个轨道不能共用。
- `paths.catalog`：原始书目CSV；筛选包按0611字段工作，不是任意CSV自动兼容。
- `paths.screening_scope`：筛选用学科配置。与taxonomy、辞海v9范围配置是不同协议。
- `book_manifest.enabled`：示例默认为true，阶段02自动转换审核CSV为书单。
- `paths.screening_result`：可选，外部审核结果；为空时读取本次 `screening/07_final/最终审核结果.csv`。
- `paths.md_root/pdf_root`：可选，按identifier或原路径文件名精确查找本地文件的根目录。
- `paths.dictionary_scope`：辞海分支的v9学科范围配置，可按书单行逐本提供scope_config。
- `paths.books`：已有标准书单时，将 `book_manifest.enabled=false` 后直接读取此文件。
- `dictionary_approval.enabled`：默认为true，第2步分类完成后自动生成approved_books并接入第3步。
- `paths.approved_books`：仅在 `dictionary_approval.enabled=false` 时使用的外部放行书单。
- `paths.taxonomy`：当前学科的真实知识树。
- `paths.classification_model_config`：结构分类专用JSON模型配置。
- `model.api_url/model.name`：清洗与抽取使用的真实服务和模型ID。分类JSON中的模型设置须同步填写。
- `paths.knowledge_input`：可选，直接从已有抽取数据启动第4步；配合 `knowledge_input_format=standard/dictionary/important`。

`paths` 中相对路径以配置文件所在目录为基准；可以使用 `{root}` 指向整合目录。`books.json` 里面的MD/PDF/scope路径按照原模块要求填写绝对路径。只复制根目录至其他环境即可携带逻辑代码，但必须重配数据、模型、知识树及依赖。

```bash
# 仅打印计划：不创建运行目录，不调用API
python pipeline.py plan --config configs/pipeline.example.json

# 文件与交接检查：示例缺真实输入/环境时返回非零；内置挂载不要求配置hook
python pipeline.py check --config configs/pipeline.example.json

# 配置真实输入后，显式执行选定阶段
python pipeline.py run --config configs/my_subject.json --stages 01
python pipeline.py run --config configs/my_subject.json --stages 02
python pipeline.py run --config configs/my_subject.json --stages 03,04

# 已有知识点时直接清洗；需设置knowledge_input和对应格式
python pipeline.py run --config configs/my_subject.json --stages 04

# 配置模型并完成清洗后，执行内置挂载和去重
python pipeline.py run --config configs/my_subject.json --stages 05,06

# 完整运行：配置全部真实输入与服务后才执行
python pipeline.py run --config configs/my_subject.json --stages 01,02,03,04,05,06
```

`--resume` 只跳过调度器记录为completed且命令/产物检查通过的任务。修复失败原因后可显式加 `--retry-failed` 重试failed任务，保存上一轮失败记录；它不会删除已有输出，是否能原地恢复仍由各模块决定。running任务仍拒绝重入。调度器完成状态只代表进程结束且约定文件存在，不等于语义准确，也不代替各包验收报告。

整合层目前尚未对所有任务的全部输入、输出内容与代码统一计算续跑指纹；命令相同且文件存在不足以证明文件内容未改动。不要改动原输入或手动混拼不同批次；原生模块有自己的更严格检查点约束。外部手动修复失败任务后，调度状态不会自动接纳该结果，需要按模块恢复流程处理或使用新运行目录。

各任务日志在 `runs/<批次>/logs/`；`pipeline_state.json` 保存命令、状态与退出码。配置变更必须新建运行目录。初版不在失败时自动降级模型、换规则或将失败项计为删除。

## 4. 各阶段的具体边界

### 01 书目筛选

复用筛选包 `examples/run_stages.sh` 的1–4子阶段：学科宽召回 → 元数据初筛与拆轨 → 重要书籍模型精筛 → MD审核输入与两轨CSV。

输入主要包括identifier、书名、语言、年份、parsed_path等元数据。宽召回依赖配置关键词、共现及排除规则，**不是BM25模型**。已有学科书单时，可以按原包从子阶段2执行；统一入口初版从子阶段1开始，不会隐式跳过全库召回。

### 02 质量审核及版型分类

先运行筛选包5–7子阶段完成重要书籍MD审核、辞海MD审核及两轨汇总。`run_md_quality_audit=false` 仅适用于已审核的外部书单，并应保留对应审核报告。

重要书籍audit/materialize原生入口读取固定的 `MD审核试验样本.csv`。当前包装会先将选定important.csv写到此文件，再调用审核，避免传入CSV与实际读取书单脱节。

**PDF默认质量合格，不执行独立质量筛选，也不作为待接入任务。** PDF存在时可为分类提供粗体、字号等辅助；书单转换只关联路径，不检测扫描件、OCR质量、缺页等。未提供或未找到PDF时记录情况并使用MD，不伪造PDF路径。

辞海分类读取分布式样本及PDF辅助格式，输出主体结构、词头位置、证据、review与技术失败。重要书籍分类输出N1、N3、OTHER等；N2/N4/N5只是OTHER子类型，没有抽取器。

筛选最终CSV已通过 `adapters/screened_books_to_manifest.py` 接入，自动生成 `02_structure/book_manifest/books.json` 供结构分类使用。类别可靠不代表每页都适合抽取，模型分类仍需抽检。

#### 审核书单转换脚本

此脚本只做本地数据衔接，不调用API、不下载文件，也不重新判断书籍内容。

1. 默认读取 `final_decision`，兼容仅有 `decision` 的结果，只选 `PASS`。
2. 按 `--track dictionary/important` 选择辞海类/其他重要书籍；REVIEW、DROP及其他轨道写入excluded。
3. 使用identifier原值；空ID、非法文件名字符、重复ID、技术失败与学科冲突单独列为unresolved。
4. MD依次读取 `md_path/resolved_md_path/v3_source_path/source_path/parsed_path`。支持OSS前缀映射成本地目录，以及在MD根目录按identifier或路径文件名精确查找；不按相似书名猜测，多个同名候选记为未匹配。
5. PDF读取 `pdf_path/documentpath`，也可按identifier查找；只附加找到的本地PDF或.bin路径，不检查文件内容质量。
6. 输出绝对文件路径，保留原审核行、匹配方法与未匹配理由。相对字段路径以输入书单所在目录为基准。

从整合目录直接运行重要书籍转换：

```bash
python adapters/screened_books_to_manifest.py \
  --input /path/最终审核结果.csv \
  --track important \
  --subject-slug mechanical_engineering \
  --md-root /path/md_directory \
  --pdf-root /path/pdf_directory \
  --out /path/new_book_manifest
```

MD/PDF已在审核结果中有可用路径时可以省略两个root参数。只有MD时省略pdf-root。辞海轨使用 `--track dictionary`，并提供 `--scope-config /path/validated_v9_scope.json`，或者各输入行有scope_config；范围配置语义/协议校验仍由辞海下游负责。

OSS已映射到本地时使用：

```bash
python adapters/screened_books_to_manifest.py \
  --input /path/最终审核结果.csv --track important \
  --subject-slug mechanical_engineering \
  --path-map 'oss://bucket/parsed/=/local/md/' \
  --out /path/new_book_manifest
```

脚本默认不接收缺少book_track的书单；确认输入已拆轨后可加 `--assume-track`。其他表头使用 `--id-column/--title-column/--decision-column/--track-column/--md-column/--pdf-column` 显式指定。决策值需是PASS/REVIEW/DROP，未知值不作为合格数据放行。

输出：`books.json`、`mapping_audit.jsonl`、`excluded.jsonl`、`unresolved.jsonl`、`report.json`。有未解决记录时仍输出可匹配子集及报告，但退出码为2，防止流水线无声跳过；明确允许仅处理可匹配子集时设置 `--allow-partial`。没有可用书籍始终返回2。输出目录必须是新目录。

整合配置中 `book_manifest.path_maps` 可填写前缀映射列表；也会复用 `screening_environment` 中的MD_PREFIX/MD_ROOT。自定义列名、allow_partial和assume_track均可放在book_manifest对象中。

#### 辞海分类结果自动放行

第二份转换脚本为 [approve_dictionary_books.py](adapters/approve_dictionary_books.py)。它在分类及verify完成后运行，输入是分类时使用的books.json和对应classification目录，输出：

```text
02_structure/dictionary_approval/
├─ approved_books.json       # 第3步实际读取的书单
├─ other.jsonl               # 当前结构不适配，不代表书籍质量差
├─ review.jsonl              # 结构、词头、MD或适配性仍有疑点
├─ technical_failed.jsonl    # 分类失败、文件变化、记录缺失、校验失败
├─ approval_audit.jsonl      # 全量逐书决定及分类证据
└─ report.json               # 输入/放行/分流数量与校验口径
```

默认仅放行同时满足以下条件的书籍：

- 原分类 `status` 和 `routing_status` 均为 `sample_supported`。
- family属于entry_prose、fixed_fields或text_commentary；head_position为inline、standalone或both。
- md_quality_status为no_problem_reported_in_samples，applicability_status为model_considered_compatible。
- 分类书单与输入书单一致，MD哈希未变，保存的分类结果可由原响应和原MD证据重新验证。

全部核验为本地处理，不调用API、不新增PDF质量筛选。organization细分标签不确定时，只要同属已确认结构族且满足上述条件，不因细分标签单独阻断。原分类中的ready_for_extraction=false等字段不被改写；这里的approved只是允许尝试全书抽取，不是整本语义质量或人工验收通过。

也可单独运行：

```bash
python adapters/approve_dictionary_books.py \
  --books /path/02_structure/book_manifest/books.json \
  --classification /path/02_structure/classification \
  --out /path/new_dictionary_approval
```

输出目录必须是新目录。分类目录需保留manifest、DONE、results、prepared、raw，以及manifest指向的准备目录；不能只拷贝results后声称完成原文复核。无可放行书籍时输出分流报告并返回2，避免空书单继续抽取；有放行书籍时只继续处理该子集，其他书籍仍留在分流清单。

整合入口已自动调度此步骤，重要书籍N1/N3分支不使用这份书单。

### 03 知识点抽取

辞海轨：全MD和PDF辅助准备 → 全书分块模型抽取 → `clean-prepare` 核对源文件哈希、词头与正文字符范围。输出 `verified/INPUT.json`，包含原名称、`raw_content`、原文位置和来源证据。`scope_config` 必须符合辞海v9协议。partial书的已完成词条可能被接收，但原书状态仍保留，不等同整本已完成。

重要书籍rule轨：调用原 `route_extractors()`，仅将成功分类的N1/N3交给相应规则。N1抽标题层级，N3抽条列中的名词性知识单元；其他状态记录到路由结果。输出 `knowledge_points.csv` 和逐书JSON。CSV的 `knowledge_point` **是原语言名称，不保证是英文**；CSV没有全局知识点ID，N1合并CSV通常也不含完整正文。

初版不会根据N1名称伪造定义，定义可为空；逐书JSON、MD与行号保留为后续取回正文的接入依据。当前仅要求N1/N3；N2/N4/N5只记录OTHER分类结果，不安排补充抽取。

#### 重要书籍LLM分支（r5）

已移植 [逐书MD抽取模块](modules/important_llm/README.md)，外层使用 `adapters/important_llm_io.py` 衔接。流程为：

```text
第2步N1/N3分类及证据复验
  → llm_work/inputs/manifest.json：identifier→book_id，md_path→local_name，附MD哈希
  → 原生book_extractor.cli：逐书MD分块、名称发现、证据支持的释义综合、定向复核
  → 原生validate_runs.py：来源回拼、引用、候选覆盖和状态核验
  → 原生export_cleaning_delivery.py：冻结可进入清洗的记录与证据
  → llm_clean_input/records.jsonl：添加全局ID、tag、结构化source
  → 第4步现有两阶段清洗
```

原模块可以处理通用MD，但当前整合只把已分类为N1/N3的书籍送入，OTHER和待复核/技术失败另存。抽取不依赖N1/N3规则先产出候选，也不是低产补抽任务。原模块内部本来具有目录/索引规则增强和必要调用重试，这些原生行为没有改动。

名称与别名要求原文依据；释义可以依据同书证据总结改写，**不是逐字摘录**。原 `record_id/book_id/run_id/evidence_ids/candidate_ids/issues/confidence/definition_supported` 等字段保留在原始记录和清洗trace里；不把category直接当知识树挂载路径。统一id按book_id、run_id、record_id确定性生成，原record_id不覆盖，运行间不按相同record_id盲目合并。

原生交接导出要求非空释义，并排除关联流程失败、未复核、未完成上下文、超预算、跨批分组延后等记录。排除明细保存在llm_delivery/excluded.jsonl，不代表已做独立语义清洗；low置信度等其余标记按原模块契约保留给后续处理。

LLM依赖必须隔离：它要求 **Python 3.12**，锁定markdown-it-py 4.2.0等版本，与辞海模块的4.0.0不同。不要将两个requirements直接合并安装到同一个环境。

```bash
# 在knowledge_point_pipeline目录内准备独立LLM环境
python3.12 -m venv .venv_important_llm
.venv_important_llm/bin/python -m pip install -r modules/important_llm/requirements.lock.txt

# 不要求editable安装；统一调度会设置PYTHONPATH到副本src
PYTHONPATH="$PWD/modules/important_llm/src" \
  .venv_important_llm/bin/python modules/important_llm/examples/check_installation.py
```

用 `configs/pipeline.important_llm.example.json` 创建本地配置，用 `configs/important_llm_services.example.json` 创建服务配置，修改模型ID与服务地址；服务配置中的tokenizer_path相对配置文件解析。tokenizer必须与实际模型一致，不会自动下载或估算替代。

- `important_llm.python`：独立解释器，示例为 `{root}/.venv_important_llm/bin/python`。
- `paths.important_llm_services`：原生services配置路径，与结构分类model配置分开。
- `important_llm.workers/book_workers/max_connections`：每书逻辑线程、驻留书数、全局HTTP并发，默认4/1/4。
- `important_llm.chunk_tokens/max_output_tokens`：分块预算及输出上限，默认4000/8192。
- 原生CLI客户端上下文默认为32768；顶层context_limit不覆盖它。改变原生窗口需按原包Python API接入，不能仅修改无效JSON字段。
- 服务密钥从 `LLM_API_KEY` 等配置指定的环境变量读取。无鉴权服务也需设置其接受的非空占位值；真实密钥不写入示例文件。

```bash
# 仅查看命令，不调用API
python pipeline.py plan --config configs/pipeline.important_llm.example.json --stages 03,04

# 填写真实输入/配置并完成阶段02后，再显式执行
python pipeline.py run --config configs/my_important_llm.json --stages 03,04
```

原生CLI退出码1（例如partial）时整合调度会暂停，默认不会将部分完成批次当成完整成功。显式恢复可用原生命令追加 `--resume-existing`，必须使用原冻结清单、服务配置和运行目录；修复后按调度器恢复约束重新衔接。`complete`也只是执行完成，不等于语义合格。原生导出器支持已停止的complete/partial书中的可交接记录，只有人工明确处理部分批次时才单独使用该接口，不能绕过失败清单。

新分支主要产物：`03_extraction/llm_work`（完整原生结果）、`llm_delivery`（带checksums的冻结交接）、`llm_clean_input`（统一输入和ID映射）。保留这三个目录以便回溯，不只拷贝最终records后删除证据。

### 04 当前清洗流程

1. `normalize_records.py`：JSON/JSONL/CSV转标准JSONL。纯英文name在英文名称槽为空时移入knowledge_point，明确为英文的正文移到英文字段；有冲突保留供模型判断。原文无中文不补译中文。混合中英名称不强行拆分。
2. 当前规则清洗：确定性格式错误删除，安全清理标题标记、序号、布局引用、简繁与字符格式；模糊名称保留给模型。
3. 当前模型清洗：按当前学科知识树审核名称与学科相关性，允许直接交叉；规范双语字段，处理正文，四路输出keep/drop/review/error。
4. 根据ID恢复原source对象及学科tag，输出标准字段；原记录、原ID类型、输入行、迁移操作保存在trace侧文件。

使用标准文本字段和explanation/raw_content作为证据，不将所有额外字段送模型。来源对象的标题用于模型输入，原对象在清洗导出时恢复。已存在ID统一转字符串，以满足去重包；没有ID的行按学科、轨道、书目、记录内容及位置生成稳定ID，并记录映射。相同ID冲突直接报错，不擅自覆盖。

**清洗包与辞海包内的清洗不能默认叠加。** 辞海包本来还有v22名称、v9学科、v46正文溯源清洗。本初版默认在辞海 `clean-prepare` 后转入当前两阶段清洗，避免两套名称/学科审核都自动跑一次。若要求严格原文片段输出，选择辞海原生clean路径进行单独实验，随后通过标准化接口接挂载，不默认再跑两阶段模型。

当前模型清洗允许截取和压缩，证据重合校验不是逐字精确原文校验。恢复source也不意味着新定义的全部字符仍对应原span。需要原文级交付时，应补清洗后精确证据检查或采用v46原生分支。

Review文件独立保存；沿用当前统计口径时计入未保留/删除，但不能清空复核证据。技术错误始终单列。历史“语言误删恢复”脚本作为审计工具保留，本版预先规范语言字段，不默认对新批次执行历史恢复规则。

### 05 挂载

已通过 [mounting_bridge.py](adapters/mounting_bridge.py) 接入。默认复用原包通用挂载器和证据门控复核函数，不调用固定12学科的历史总runner。

四个步骤：

1. **prepare（不调用API）**：冻结清洗输入；按tag/学科别名匹配知识树；统一嵌套树、扁平nodes表、root包装、hierarchy及电子信息L1–L4列表；生成运行profile、节点索引与内部ID到原记录的映射。
2. **route**：对本批知识点重新执行首次路由，不把原main_tags当正确答案。原挂载器采用定义优先、描述辅助的证据；沿树保留候选，允许证据支持的父节点。
3. **review**：对主路径及候选次相关路径复用原包合理性审核。判不合理时执行独立反证复核；不确定和技术失败单列，不自动放行。
4. **export（不调用API）**：重新校验完整节点编码链、深度、分数和审核证据，回填原ID；只更新main_tags/related_tags，缺失tag时补学科名。通过主路径复核的记录进入标准挂载文件，其余分流。

默认阈值0.85、深度L2–L5（根节点算L0）、最多2条次相关路径、16并发。路由阶段L1–L3采用0.6宽松候选阈值，最终接纳仍要求原生path_score≥0.85、深度合规且主路径复核reasonable。path_score为原挂载器的路径聚合分数，不是经过校准的正确率。次相关路径也必须单独复核合理，不仅凭beam排名写入。

配置由 `mounting` 对象控制，支持threshold/min_depth/max_depth/max_related/workers/max_tokens/timeout/limit，以及可选api_url/model/api_key_env。模型连接默认沿用顶层配置；首次测试建议limit=20、workers=4。原包/API故障重试保留，但不自动反复重挂未通过项。

知识树目录默认是 `Books_textbooks_cleaning_pipeline/taxonomy`，示例通过paths.taxonomy_dir及paths.taxonomy引用。映射见 [taxonomy_registry.json](configs/taxonomy_registry.json)，23份树已通过本地转换。水利工程无对应树，标记unconfigured，**不自动使用土木树**。新增学科需补注册表与树文件。

运行时完整路径由原始祖先节点名称逐级连接得到，不按名称中自带的“/”重新拆层，原路径保留在node_index。电子信息列表没有显式学科根，添加仅用于路由的“电子信息工程”根并在报告标明；根不可作为挂载目标。其余树保留实际根名称。L2–L5依据结构深度判断，不依赖斜杠数量。

初版语义卡片只整理树中已有的定义/边界、路径及子节点名，**不会预先调用模型生成全树边界**。原始树文件不被改写。其他层级的叶节点不会为了提高挂载数自动放宽到L1或L6以上。

主要产物：

| 文件 | 用途 |
| --- | --- |
| `05_mounting/mounted_standard.jsonl` | 主路径复核合理的标准输出，第6步直接读取 |
| `review.jsonl` / `not_mounted.jsonl` | 不确定/不合适路径，保存原记录和依据 |
| `technical_failure.jsonl` / `unconfigured.jsonl` | 接口/结果错误、知识树缺失，不能当内容删除 |
| `mount_audit.jsonl` / `SUMMARY.json` | 原记录、内部ID、评分、审核及层级统计 |
| `groups/<slug>/profile/` / `node_index.json` | 冻结运行知识树、原始路径和编码映射 |
| `groups/<slug>/mounted_standard.jsonl` | 按学科输出，供独立多学科任务分别去重 |

统一pipeline仍按单学科执行，混入别的已知tag会报错；独立使用bridge时省略subject-slug，可按tag拆多个学科顺序处理。输入字段和原ID不改写，不能把内部KP_或复核request_id用作交付ID。缺树、接口错误与未挂载都参与数量守恒统计。

已准备20条机械知识点供部署后测试（只运行过prepare）：

```bash
# 在knowledge_point_pipeline目录；填写新部署接口和实际模型ID
export MOUNT_API_URL='http://MODEL_HOST:8000/v1/chat/completions'
export MOUNT_MODEL='EXACT_MODEL_ID'

python adapters/mounting_bridge.py route \
  --out runs/mounting_smoke_20_20261008 \
  --api-url "$MOUNT_API_URL" --model "$MOUNT_MODEL" --workers 4
python adapters/mounting_bridge.py review \
  --out runs/mounting_smoke_20_20261008 \
  --api-url "$MOUNT_API_URL" --model "$MOUNT_MODEL" --workers 4
python adapters/mounting_bridge.py export --out runs/mounting_smoke_20_20261008
```

样本为 `inputs/mounting_smoke_20.jsonl`，来自现有清洗结果中的前20条机械记录，不是随机质量评测集。也可用任意标准清洗JSONL重新prepare到新目录。prepare快照/配置不得中途改写；route发现已存在输出会拒绝覆盖。review默认也拒绝覆盖，但可显式加 `--retry-failed-reviews`，在原输入完全一致时只重试technical_failure，其他复核结果复用，旧响应留备份。模型接收短请求ID，由本地映射回完整审计ID。原生全量挂载仍不声称具备通用断点续跑。

鉴权时在环境中设置密钥，再给route和review同时传 `--api-key-env 环境变量名`；不把密钥写入命令文件。`hooks.mounting`仍允许高级用户用自定义argv任务覆盖默认实现，但不再是必填接口。

### 06 去重与合并

默认执行原 `dedup.py run --mode length`。同一学科可配置多个 `dedup_inputs`，一次合并辞海轨、重要书籍轨等已挂载文件；路径写绝对路径或使用 `{run}`。不同学科分别运行。

候选依据为：同学科、同完整main_tags路径，非空中文name或英文knowledge_point同名。名称只做首尾空白/大小写规范，不是语义同义词去重。长度优先保留较长中文定义，其次较长英文定义，再按输入顺序。重复ID直接报错，不自动重新编号；空main_tags记录不参与去重，需要前置挂载导出把它们分流。

`retained.jsonl` 是保留结果，`removed.jsonl`和`duplicate_audit.jsonl`记录去重删除。原包另有 `--mode llm`，仅比较重复候选组质量；不确定、超限或失败保留并复核，review是retained子集。**这与清洗阶段Review计入删除的统计口径不同，不得直接把各模块review相加。**

## 5. 统一数据和追溯约定

```json
{
  "id": "subject:track:stable_record_id",
  "knowledge_point": "gear",
  "name": "齿轮",
  "definition": "原文支持的中文定义",
  "en_definition": "",
  "description": "",
  "en_description": "",
  "main_tags": "学科/一级节点/二级节点/具体节点",
  "related_tags": [],
  "tag": "机械工程",
  "source": "原书名或原始来源对象"
}
```

至少一项名称非空的有效性由清洗判定。抽取阶段还没有挂载时main_tags可为空；tag是来源任务的目标学科，不等于模型在全学科中择一分类的结果。挂载不得通过猜测tag补出完整路径。

全流程建议保留三类数据：正式输出、待复核/技术失败、审计与原文证据。不能为了统一schema删除source中的原文范围、哈希及书目ID；本版通过trace侧文件保留这些内容。

## 6. 预留接口清单

| 接口 | 当前缺口 | 要求的后续实现 |
| --- | --- | --- |
| PDF/EPUB→MD | 本版使用已有MD | 接解析器，保存原文件ID/哈希和转换版本 |
| 重要书籍N1正文取回 | 合并CSV以名称/行号为主 | 从逐书JSON/MD恢复正文，避免编造定义 |
| 清洗后精确原文复核 | 当前证据重合度不等于逐字核验 | 检查名称、正文与原MD范围；必要时回到原生v46 |
| 全流程质量统计 | 各模块已有本地统计 | 区分书数/词条数、重复/内容删除/技术失败、Review是否子集 |

## 7. 并发与运行注意

初版整合层默认分类4、清洗32并发、清洗batch-size=8；不同学科和轨道顺序运行，由各模块内部并发。它们不是全流程统一限流器。

辞海全书抽取原包内部轮次固定为 **1024→256→64**，`book_workers`只是同时处理书籍数，不会把HTTP请求并发限制到4。后续重试轮依赖 `/metrics`；分类可能要求 `/tokenize` 和 `/v1/models`。内置新挂载接口默认16并发，不调用历史1024并发边界生成总runner。真实部署前检查接口、服务容量及模型上下文，不沿用历史10万token窗口。

未进行本批次吞吐基准测试，不能提供可靠全量耗时。可用2–5本代表性书籍记录各阶段耗时、token、API失败和实际并发，再按MD字符数/抽取条数分层估算；不同版型差异较大。

## 8. 验证与下一步

### 2026-10-08 离线串联评估

离线回归套件不调用真实模型接口。实测修复后执行了41项集成/衔接测试及57项清洗回归，共98项通过。测试进程禁用socket连接；模型清洗/挂载/复核响应使用模拟产物，实际运行本地适配、规则抽取/清洗、挂载ID与路径/证据校验及长度优先去重。重要书籍LLM核心81个文件保持移植时的哈希。真实模型测试另见前述端到端报告。

| 交接位置 | 离线结论 | 验证范围 |
| --- | --- | --- |
| 01→02 | 接口明确 | 筛选子阶段文件名、两轨审核结果、PASS书单转换已核对；未进行真实全库筛选 |
| 02→03 辞海 | 接口通过 | 分类原响应/MD证据可复核；approved_books自动接入抽取 |
| 02→03 重要书籍rule | 本地样例通过 | N1/N3实跑规则抽取，其他版型不进入抽取 |
| 02→03 重要书籍llm | 协议通过 | N1/N3清单、独立Python、原生验证/冻结导出与证据回填衔接；不调用模型 |
| 03→04 | 本地测试通过 | 名称语言迁移、原ID/来源保留、两轨数据转标准格式、规则清洗 |
| 04→05 | 接口通过 | 标准输入直接准备profile并路由、复核、导出；23份实际知识树格式兼容 |
| 05→06 | 本地样例通过 | bridge生成的mounted_standard直接进入真实去重；模型判断仍为模拟响应 |

本轮修正：

1. 补齐各阶段依赖与产物声明，包含筛选中间CSV、结构准备manifest、分类verify标记和LLM validation.json，避免计划检查误报缺失。
2. 修复清洗副本中过宽的图表引用正则：保留Stable、Configuration、table tennis等普通词，只有明确带数字编号的引用才匹配。规则版本更新为v3、模型本地清洗策略更新为v6；旧结果不自动重算。
3. 子进程无法启动时，将调度状态写为failed，避免留下误导性的running。
4. 增加内置挂载接口与学科注册表，完成原ID回填、L2–L5/0.85接纳、次路径独立复核以及标准结果导出。

重复评估命令（已安装本地依赖的环境）：

```bash
python validate_offline.py
```

报告输出 `validation_logs/offline_flow/report.json`，含本轮代码SHA256和三种计划的依赖检查；日志在同目录。`all_six_stage_interfaces_connected=true` 表示代码交接完整；该离线报告中的 `production_run_validated=false` 只限定离线套件的验证范围，不否认另行完成的真实MD测试。真实运行以测试记录和 `VALIDATION.json` 的real_model_smoke_test为准。

当前已验证该部署的模型列表、tokenize及重要书籍分支所需的真实结构化请求。仍未验证辞海抽取的metrics轮次门控、跨学科语义准确率、生产吞吐及全量数据质量。重要书籍LLM独立环境需按前述说明安装；切换服务后不能沿用旧服务的联调结论。

整合测试覆盖英文名称迁移、重要书籍中文标题映射、冲突字段保留、来源侧文件回填、ID冲突拒绝、规则串接以及未实现接口阻塞；不访问模型服务。

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s modules/cleaning/tests -v
python pipeline.py plan --config configs/pipeline.example.json
```

六阶段接口已衔接，已完成一份机械N1资料的真实LLM路线测试。下一步应选择通过审核的辞海资料、N3真实样本及其他学科进行扩展联调，随后再评估批量吞吐。验收需分别检查书目筛选误删、原文词头完整性、名称与正文匹配、学科交叉误删、挂载路径与去重理由；条数守恒只证明技术覆盖，不证明知识点质量。
