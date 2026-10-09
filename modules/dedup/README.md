# 通用最终去重管线

版本：20261008.2。适用于已完成词条清洗及挂载的知识点，辞海类、重要书籍或二者合并结果均可输入。不依赖特定学科，不自动发布或覆盖正式交付。

## 两种模式

| 项目 | `length`：长定义优先 | `llm`：质量优先 |
| --- | --- | --- |
| 重复候选 | 同一学科、同一完整主挂载路径，非空中文name或英文knowledge_point同名 | 与规则模式相同，只将重复候选交给模型 |
| 选择依据 | 中文definition字符长度优先，其次英文en_definition字符长度；再按原输入顺序 | 先确认同一知识点，再比较名称完整性、定义准确相关、正文完整干净、无串条截断等 |
| 长度与质量 | 完全复用之前全量交付的长度优先算法，快但不等于质量更高 | 可以保留较短但更准确的原记录，不按长度直接裁决 |
| 不确定或失败 | 不调用模型 | 同名不同义或不能确认重复时分别保留；技术失败、超大组保留并进入待复核 |
| 字段处理 | 原ID、原字段、值、类型和字段顺序不变 | 同左；模型仅输出分组与选中ID，不生成新定义或解释 |

候选匹配仅去掉名称首尾空白并忽略大小写，不删除内部空格、不补译、不做同义词扩展或模糊匹配。不因为同一分支就认为所有知识点重复，也不对不同完整路径做跨分支去重。父节点和子节点不是同一分支位置。

中英文同名可能组成候选链；被删项必须与最终保留项**直接**同名，不能通过已删除记录的别名继续扩散去重。不合并来源、定义、解释、related_tags，不重新生成ID。

## 输入格式

- UTF-8 JSON数组或JSONL，一条原始知识点记录一个对象。
- `id`必须是非空字符串，在同一学科本次输入中唯一。多个输入中ID冲突直接报错，不擅自重新编号。
- `main_tags`必须是完整主路径字符串；路径为空的记录不参与去重。
- 名称和定义/解释字段沿用标准字段：`knowledge_point`、`name`、`definition`、`en_definition`、`description`、`en_description`。文本可缺失或为null，但不会被本工具写成其他值。
- 其他字段及额外元数据全部保留。`source`支持原有字符串、数组或对象。
- 一次`run`只处理一个学科；多学科用`batch`分别运行。允许不同学科拥有相同ID，不将它们混成一个文件。
- 若有明确的路径别名映射，可提供`--path-aliases aliases.json`，格式为`{"旧完整路径":"标准完整路径"}`。只影响匹配，不改写输出路径；不猜测映射，不接受循环映射。

## 环境与快速运行

Python 3.10及以上，仅用标准库，支持Windows和Linux。开发机建议在NAS2上运行。

```bash
cd /mnt/nas2/home/wangqiyuan/toolkits/final_dedup_toolkit_20261008
python3 dedup.py --help
```

### 模式一：长定义

```bash
python3 dedup.py run --mode length \
  --subject education \
  --input /mnt/nas2/自己的目录/education_knowledge_point.json \
  --out /mnt/nas2/home/wangqiyuan/final_dedup_runs/education_length_新的运行目录
```

合并后的全量可直接输入。也可重复传入`--input`，将同一学科多个文件一起去重；输入顺序用于长度相同情况下的稳定选择。重复ID必须先按原始全量确定正确输入，不能用名字去重修复ID冲突。

### 模式二：模型比较质量

```bash
export FINAL_DEDUP_API_URL='http://自己的模型服务:8000'
export FINAL_DEDUP_MODEL='服务实际模型名称'
python3 dedup.py run --mode llm \
  --subject education \
  --input /mnt/nas2/自己的目录/education_knowledge_point.json \
  --out /mnt/nas2/home/wangqiyuan/final_dedup_runs/education_llm_新的运行目录 \
  --workers 64 --retries 2
```

模型URL支持服务根地址、`/v1`或完整`/v1/chat/completions`。必须显式配置实际模型名，不沿用旧模型默认值。默认无鉴权；需要Bearer鉴权时，通过`--api-key-env 环境变量名`从环境读取，不把密钥写进文件。URL不接受内嵌账号、密码或查询参数。

`--workers`默认64，仅作用于重复候选组，可按服务能力调整（包括设为1024）；不是每条知识点都调用模型。首次调用失败后最多再重试两次，`--retries`可设0、1、2，包括HTTP失败、非正常结束、截断或返回ID集合不合法。只接受正常结束的模型响应。失败不会自动改用长度结果，也不会删掉整组。

每组传完整名称、定义、解释和原记录证据，不静默截断文本。默认`--max-members 100`、`--max-input-chars 60000`是完整系统提示词与候选内容的字符预算，不是token数，也不等于模型上下文窗口。超过限制整组保留待复核；可确认服务容量后提高预算重试。

### 多学科批量

填写`examples/batch.example.json`中的真实输入路径（相对路径以配置文件所在目录为基准），然后：

```bash
python3 dedup.py batch --mode length \
  --manifest /mnt/nas2/自己的目录/batch.json \
  --out /mnt/nas2/home/wangqiyuan/final_dedup_runs/all_subjects_length_新的运行目录
# 同一清单可单独使用 --mode llm，输出至另外一个目录。
```

学科顺序执行，每个学科内部模型组可并发；不同时加载所有学科全量数据。每个学科单独输出，ID不跨学科去重、不重新编号。本工具单学科数据在内存中处理，大数据建议分学科运行，不是无限规模流式去重。

## 输出与验收

| 文件 | 内容 |
| --- | --- |
| `retained.json` / `retained.jsonl` | 按原输入顺序排列的保留原记录；含保守保留的待复核项 |
| `removed.jsonl` | 因重复剔除的完整原记录，不改写原数据 |
| `duplicate_audit.jsonl` | 被删ID、最终保留ID、同名字段、分支、选择原因 |
| `review.jsonl` | 模型未确认可合并、技术失败或超过预算的原记录；是retained的子集，不重复计入总数 |
| `group_decisions.jsonl` | 每组模型状态、分组选择、技术错误类型和请求次数 |
| `INPUT.jsonl` / `RUN.json` | 冻结输入快照、原文件哈希、运行口径、模型及提示词版本 |
| `checkpoints/` | 按组原子保存的模型断点，无API密钥 |
| `PROGRESS.json` / `SUMMARY.json` | 组完成进度、保留/删除/复核数、耗时和本次模型请求数 |
| `VERIFICATION.json` / `DONE.json` | 独立程序验证及阶段完成状态，不等于人工或模型语义准确率验收 |

```bash
python3 dedup.py verify --out /mnt/nas2/自己的运行目录
```

验证包含：输入=保留+删除、原ID与全部字段未变、原顺序保留、每个删除有直接同分支保留对象、长度模式符合旧优先级、模型模式与已校验断点决定一致。模型模式允许残留同名候选，不能为了把重复数清零而放行不确定合并。

本工具不自动过滤输入质量；不会重新做书目筛选、知识点清洗、学科判断、挂载或来源补齐。模型质量比较仅基于提供的原记录，不重新查找PDF/MD原文。需要核实错误定义时仍应对照来源抽检。

## 断点续跑

原命令原输出目录加`--resume`。已通过校验的模型组复用；技术失败及超预算组再试。调整并发、重试次数或完整文本预算允许续跑；改输入、路径映射、学科、模式、模型、URL、程序版本或提示词必须使用新目录。

输入文件在运行中发生变化会使最终验收失败。相同运行目录有排他锁，不允许多个进程同时写。默认不覆盖旧运行目录，也不更新正式NAS/S3交付。

## 离线测试及打包来源

```bash
python3 validate_offline.py
python3 verify_package.py
```

测试使用合成数据与模拟模型响应，不调用真实模型，不代表真实语义质量达标。运行逻辑复用旧全量去重`closeout_core.merge_records`及旧去重包的URL、JSON解析与分组校验辅助函数；只使用其中的选择逻辑，不使用内容融合、名称改写或ID重分配流程。来源与哈希见`SOURCE_PROVENANCE.json`。

本包附使用样例、离线测试及打包验证，不包含真实全量知识点、私钥、账号密码、模型密钥或正式交付发布脚本。`build_package.py`仅是Windows源代码打包工具，不是开发机运行入口。
