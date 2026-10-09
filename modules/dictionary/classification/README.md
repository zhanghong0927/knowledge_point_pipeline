# 书籍主体结构分类工具包

## 用途和版本

这是当前书籍结构分类工具，不包含知识点抽取、词条清洗、翻译、挂载或去重。

分类核心与提示词来自开发机实际运行的：
`/mnt/nas2/home/wangqiyuan/book_structure_classification_runs/20260921_common_revision_full113/code/`

这 7 个核心文件原样保留。新增 `scripts/classify_books.py` 仅提供可接收新书单、独立模型配置和输出目录的入口，不依赖历史 113 本书目路径。文件校验值见 MANIFEST_SHA256.json。

不要直接用旧 `run_unseen20_classification.py` 的命令行入口处理新书。该文件保留当前提示词与验证接口，但原来的主函数依赖历史目录和固定书目；推荐入口统一为 classify_books.py。

## 分类逻辑

1. 扫描 MD 全文的结构信号，在不同区域定向选择词头附近片段，并加入对照片段。
2. 尝试对齐原 PDF 的文字，附加匹配到的粗体、相对字号、斜体等格式证据。PDF 不替代 MD 正文，不传每页截图。
3. 为样本补上少量前文和标题线索，交给 LLM 判断主体区域、主词头角色、组织方式、可用性及边界适配。
4. 规则校验引用是否真实来自给定 MD 行，检查跨区域证据，再汇总成主体类别和格式特征。

**全文信号扫描不等于模型逐页读完全文。这是抽样分类，不是全书抽取，也不保证每种少数结构都会被抽到。**

主体规则族为 entry_prose（词条＋连续正文）、fixed_fields（重复固定字段）、text_commentary（原文＋评注）。另外用 other 表示有证据不适合当前策略，review 表示证据不足，technical_failed 表示技术失败。不要把 review 当特殊格式或坏书。实际字段以 JSON 为准，family、routing_status/status 等字段分开保存。

O1/O2/O3/O4 是组织特征，不等于四套已完成的抽取器。词头位置 inline/standalone/both/unknown、局部格式标签和 MD 风险独立保存，不能把大类已确认等同于边界已确认。

## 环境

- 推荐开发机 Linux，Python 3.10 或以上；运行阶段使用 fcntl 防止同一目录并发写入。
- PyMuPDF 用于 PDF 解析；安装：`python3 -m pip install -r requirements.txt`。
- 标准库负责其余处理。MD 必须为 UTF-8 或 UTF-8 BOM，所有输入路径应在运行机器上真实存在。
- `pdf_path` 可省略；提供后按 PDF 文件头判断，也支持实际内容为 PDF 的 .bin。其他格式不会假装解析成功。

## 输入

按 examples/books.json 填写 JSON 数组，每本一条：
- identifier：本批唯一，只能含 ASCII 字母、数字、下划线和短横线；用于输出文件名。
- title：书名，仅作来源记录，不作为模型猜测书类的依据。
- md_path：MD 绝对路径。
- pdf_path：可选原 PDF 绝对路径。

模型配置参考 examples/config.json。填写部署实例的真实 model ID；api_url 为服务根地址，不带 /v1 后缀。100000 表示上下文预算上限，不是要求把每次输入塞满。

## 运行

在解压后的工具包根目录运行。准备和结果各使用独立目录。

```bash
python3 scripts/classify_books.py prepare --books /path/books.json --out /path/prepared_run
python3 scripts/classify_books.py run --base /path/prepared_run --config /path/config.json --out /path/classification_run --workers 4
python3 scripts/classify_books.py verify --out /path/classification_run
```

prepare 只读原书并保存样本，不调用模型。检查 PREPARED.json 和 preparation_failed/ 后再运行模型阶段。

模型阶段会检查 /v1/models，先完成一条请求再并发。workers 可以按实例容量调至 16；这不是推荐所有部署都开 16。每本至多尝试两次，技术失败不能当作其他类或不合格书。

当前传输逻辑要求服务提供 POST /tokenize，以及 /v1/chat/completions。请求包含 response_format、chat_template_kwargs.enable_thinking=false，最多输出 16000 tokens，并预留 16500 tokens 上下文余量。**不是任意 OpenAI 兼容服务都满足这些接口要求。**当前接口不实现鉴权头、tokenize 替代方案或自动降级，需要鉴权的服务应先适配传输层。包内不含 API 密钥或实际部署地址。

## 输出与断点

- prepared/：MD 样本、PDF 格式证据、原文哈希和行号。
- sent/、raw/：模型请求和响应，可能含书籍内容，不要上传公共仓库。
- results/：逐本分类结果及排除证据。
- manifest.json：本次书目、配置、输入和代码哈希。
- PROGRESS.json：模型阶段进度。
- DONE.json：ID 集合齐全及原始响应再验证后的汇总。存在此文件不等于语义审核通过。
- FAILED.json：预检阻塞；以错误内容为准，不以输出目录存在判断成功。

同一输出目录可续跑尚未生成结果的书，但配置、输入或代码改变时会拒绝续跑。已有 technical_failed 结果也会保留，不自动覆盖；要重试这些书，请用相同结构的新书单和新输出目录。前置预检失败后先排除接口问题，不能直接把失败结果当成可接受分类。

## 验收和局限

至少检查输入/输出 identifier 是否一致、technical_failed 的数量、review 和 other 的证据是否来自主体区域，以及 PDF 是否真的对齐。结构标签正确不保证后续抽取成功。固定字段等样本稀少的类别仍需独立验证，不能宣称所有类型都已经成熟。

包内 tests 包含核心分类回归测试及新入口离线测试。运行：
```bash
PYTHONPATH=scripts python3 -m unittest discover -s tests
```

本次打包验证仅包括离线测试、历史运行代码一致性、ZIP 完整性和文件哈希核验。没有启动新的付费模型批次，不宣称新部署接口已经验证连通。

## 文件说明

- classify_books.py：通用入口。
- run_unseen20_classification.py：当前提示词及 validate 接口；历史批次启动器不是通用入口。
- classification_routing.py：证据校验和主体类别投影。
- structure_v6.py、md_primary_v5.py：MD 特征和抽样。
- structure_v6_1.py：上下文和角色校验工具。
- run_md_primary_v5.py：PDF 对齐及 HTTP 传输。
- run_structure_v6_1.py：按书请求、重试和落盘。

版本范围：当前分类核心的可复用工具包，不包含抽取管线，也不是此前 113 本分类已全部准确的承诺。
