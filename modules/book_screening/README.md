# 通用书目筛选工具包

版本：20261008。用于从0611全库或现成学科书单筛出适合知识点抽取的书籍，不包含知识点抽取、清洗、挂载和交付数据处理。

开发机登录、NAS2路径及文件传输见同目录[开发机访问与文件传输说明](开发机访问与文件传输说明.md)。

## 管线组成

| 阶段 | 方法 | 主要产物 |
| --- | --- | --- |
| 学科宽召回 | 元数据标签、题名与配置中的关键词/共现规则；流式扫描，可分片 | 宽召回全集、满足后续处理条件、硬规则排除清单 |
| 元数据初筛与分轨 | 年份、电子版、语言、解析路径等硬规则；题名识别与重复书目处理 | 辞海类候选、重要书籍严格保留、待复核、重复项 |
| 重要书籍元数据精筛 | LLM结合学科边界判断内容与知识提取价值 | KEEP / REVIEW / DROP及检查点 |
| MD质量审核 | 辞海：V3知识可用性审核＋可疑PASS反证复核；重要书籍：通用V7正文审核 | PASS / REVIEW / DROP、证据、原始模型响应 |
| 汇总验收 | 回填原书目identifier，检查行数、重复ID与阶段衔接 | 两轨分级书单、技术失败清单、统计 |

两条轨道共用召回和元数据初筛，但正文标准不同：辞海侧重词头及释义；重要书籍侧重学科核心内容和可复用知识，不要求辞典式结构。配置是可替换件，不按书名逐本写规则。

## 环境与配置

- Python 3.10及以上，核心流程和离线测试使用标准库。推荐Linux开发机运行全量。
- 已挂载MD直接使用路径映射，不需要OSS依赖。若需下载，安装`requirements-oss.txt`中的可选SDK，并由运行环境提供`SOURCE_OSS_ACCESS_ID`、`SOURCE_OSS_ACCESS_KEY`，不写入工具包。
- `configs/subject_recall/`含14份原有学科配置及1份模板；推荐使用对应的`configs/audit_compatible/`版本。旧配置仅列L1名称而非完整节点时，兼容版将原名称列表并入学科范围，未补造节点定义或边界；召回关键词和排除条件不变。新增学科应替换学科名称、召回条件、纳入/排除边界，可在配置中附符合原脚本字段要求的完整L1边界。现有配置不等于覆盖全部学科。
- 必须明确传入模型服务URL、模型实际名称和学科配置，避免沿用原脚本中的历史默认值。此工具包不主动选择或切换服务。

## 推荐运行

先解压，进入工具包目录。以下示例不会因解压自动运行：

```bash
export CORPUS=/mnt/nas3/anna_book/book_meta/corpus_es_book_dedup_1451w_deduped_version0611.csv
export SUBJECT_CONFIG="$PWD/configs/audit_compatible/mechanical_engineering.json"
export RUN_DIR=/mnt/nas2/home/wangqiyuan/book_screening_runs/自己的新运行目录
export API_URL='http://自己的模型服务/v1/chat/completions'
export MODEL='服务返回的实际模型名称'
# parsed_path为OSS URI而MD已在NAS时配置前缀映射；可以不设置。
export MD_PREFIX='oss://自己的bucket/解析文件前缀/'
export MD_ROOT='/mnt/nas2/对应MD根目录/'
bash examples/run_stages.sh all
```

`run_stages.sh`按顺序执行1至7阶段；也可指定`1`至`7`单独执行。已有学科CSV可设`RECALLED_CSV`，从第2阶段开始。第1阶段支持多个`--config`的一次扫描方式，见原说明；全库不要使用`--limit`。默认元数据精筛20并发、重要书籍MD50并发、辞海MD16并发，可通过`META_WORKERS`、`MD_WORKERS`、`DICT_WORKERS`调整，未继承知识点管线的1024并发。

推荐流程仅将重要书籍“严格保留”送元数据精筛，再将KEEP送MD审核。初筛待复核及精筛REVIEW/DROP均保留在前段目录；**最终MD汇总的覆盖范围是进入MD审核的书目，不是整个原始全库**。需要扩大重要书籍范围时，单独核查待复核清单，不静默放行。

## 文件衔接与结果

| 目录 | 内容 |
| --- | --- |
| `01_recall/{subject_slug}/` | 学科召回及硬规则排除表 |
| `02_metadata/` | 全部初筛、重复项、两轨候选和重要书籍待复核 |
| `03_important_metadata/` | 重要书籍模型精筛结果、提示词、批次检查点 |
| `04_input/`、`04_tracks/` | 精筛后的MD审核输入及两轨拆分 |
| `05_important_md/` | 通用重要书籍正文审核和最终CSV |
| `05_dictionary_input/` | MD映射、冻结候选、MD哈希、V3配置、预处理失败状态 |
| `05_dictionary_md/` | 辞海审核、反证复核及三级结果 |
| `06_dictionary_final/` | 辞海结果回填原ID后的最终CSV |
| `07_final/` | 两轨合并书单、分级CSV、技术失败清单、SUMMARY.json |

`screening_io.py`只处理衔接，不修改原脚本的内容判断。Linux以符号链接关联源MD，Windows复制；回填时检查源MD与候选是否变化。缺失MD、API失败或缺失审核结果归REVIEW并列出原因，不伪装成内容DROP；非法/重复/错位审核记录直接报错。保留原书目ID，V3临时文件名和short_id不作为交付ID。

正文审核读取MD，并结合分布抽样及脚本已有全书信号；不是把每本全部文字交给模型逐字审查，也不包含PDF版式分类。PASS是程序和模型筛选结论，不等于人工审核或知识点抽取质量合格。

## 断点与验收

- 原精筛及MD审核使用JSONL检查点。同一输入、配置和模型下可续跑原审核阶段；改输入、边界或模型必须新建运行目录，不复用旧检查点。
- 第3、5阶段可按原输入续跑。第4阶段拆轨及第6阶段MD准备只执行一次；辞海模型审核续跑时使用第6阶段里的V3命令，随后执行`dictionary-finalize`，不要重新覆盖准备目录。
- 准备和拆轨目录不得覆盖。文件丢失需先定位原因，不能通过改判DROP把失败消掉。MD下载实现不等于经过源端Content-Length逐字节校验，生产输入宜采用已验证的文件副本。
- 召回阶段核对总扫描行数、分片完整性及“召回＝满足条件＋硬规则排除”；精筛核对每行ID与结果对应；MD候选核对两轨拆分；最终核对每轨PASS＋REVIEW＋DROP等于候选数，技术失败单列。再抽样对照MD判断语义质量。
- 技术覆盖率与语义准确率分开，行数守恒不能证明书籍筛选准确。

离线自检不会发起模型或OSS请求：

```bash
python3 validate_offline.py
python3 verify_package.py
```

当前打包验证详情见`VALIDATION.json`。实际服务可用性及全量筛选效果没有因本次打包重新验证。

## 包内文件

- `scripts/`：原有召回、合并分片、初筛、元数据精筛、MD审核及输入整理脚本，原样保留。
- `dictionary_md_v3/`：原V3工具包，保留其说明与测试。推荐`--knowledge-value-mode --pass-challenge`，不要误用旧词头比例阈值作为默认标准。
- `screening_io.py`：安全拆轨、辞海MD准备、审核回填与两轨合并。
- `examples/run_stages.sh`：本包推荐入口。`scripts/run_post_metadata_md_audit.sh`仅为原测试兼容保留的历史脚本，含旧部署路径，不作为本包入口。
- `docs/`：历史详细说明，仅作背景；冲突时以本README与显式运行参数为准。
- `SOURCE_PROVENANCE.json`、`MANIFEST_SHA256.json`：脚本来源与包内哈希。无原始大书库、审核数据、模型权重或密钥。
- Python 3.10的CSV写入器无法直接生成带NUL的测试数据，包内该测试改为写完CSV后插入NUL字节，仍验证相同读取行为；业务脚本未因此修改。
