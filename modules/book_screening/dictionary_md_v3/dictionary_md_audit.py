import argparse
import concurrent.futures
import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_ROOT = Path(r"D:\七月工作\多学科辞海可用性分析")
DEFAULT_OUTPUT = DEFAULT_ROOT / "审核结果_20260818"
DEFAULT_API_URL = (
    "http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job."
    "v5000-prod.nhss.zhejianglab.com/v1/chat/completions"
)
DEFAULT_MODEL = "/mnt/si002991n0no/default/model/Qwen/Qwen3.8-27B"
PRIORITY_WORKBOOK_EXPORTER = Path(__file__).with_name("priority_workbook_export.cjs")

SUBJECTS = [
    ("教育学", "EDU", [Path("辞海类书籍") / "教育学(7)"]),
    ("经济学", "ECO", [Path("经济学辞海类")]),
    ("军事学", "MIL", [Path("军事学辞典")]),
    ("艺术学", "ART", [Path("艺术学辞海类书籍Markdown原文")]),
    ("哲学", "PHI", [Path("哲学llm审核(1)")]),
    ("建筑学", "ARC", [Path("朱毅航-建筑学")]),
    ("管理学", "MGT", [Path("management_bookmeta_v0611_md")]),
    ("土木工程", "CIV", [Path("_土木解压输入") / "土木学科辞海字典类"]),
    ("文学", "LIT", [Path("文学第一批抽取书籍"), Path("文学第二批抽取书籍")]),
    ("社会学", "SOC", [Path("_社会学解压输入") / "词典和辞书"]),
]

SYSTEM_PROMPT = r"""
你是辞海类学科工具书 Markdown OCR 与知识抽取可用性审计员。输入书籍经过初步筛选，但不保证确实属于辞海类，也不保证学科相关。任务是同时判断：它是否具有可重复抽取的“独立词条/词头 + 明确释义”结构、是否与指定学科相关，以及 OCR 后文本能否可靠用于知识点抽取。不要评价原书名气，也不能只做字符率或 Markdown 结构检查。

你必须认真阅读给出的全部均匀分布片段，并进行语义判断：句子或词条是否合理、术语和定义是否对应、是否发生串栏错行、漏行粘连、重复污染、乱码、相似字错识以及上下文突变。针对不同文档类型采用不同标准：
1. 词典、术语表允许短语、释义、省略句和中英对照，不要求像散文一样完整；双语词条的对应翻译可视为直接释义，只有双语词条对照而没有扩展解释，也不因此降级；重点检查词头与翻译或释义是否错配、左右栏是否串行、条目是否粘连。
2. 教材、专著、百科正文重点检查论述、定义、因果关系和章节上下文是否完整连贯；不要把正常的跨段抽样误判为原文跳跃。
3. 标准、规范、法规和手册允许编号密集、句式固定、参数表较多；重点检查条款层级、限定条件、数值和单位是否保存。
4. 论文集、文集、年鉴允许作者与主题频繁切换；重点检查单篇内部是否可读，不要因篇章切换本身降级。
5. 图谱、图册、图表或公式占主导的书籍，应判断关键知识是否仍能从 Markdown 恢复；若核心含义主要留在图片、公式或表格中且大量丢失，应降低可用性。
6. 繁体字本身不等于 OCR 错误，但本批次只接收简体中文和英文；繁体正文主体必须判为 DROP。旧式用词、专业罕见字、合法公式符号不等于 OCR 错误；没有语义证据时不要武断判错。
7. 正文主语言只允许简体中文、英文或两者混合。若 dominant_languages 中出现 zh/en 以外的语言，正文大量使用日文、德文、法文、俄文等其他语言，或中英混合文本中的中文部分以繁体为主，即使其他部分尚可，也必须判为 DROP；少量书名、引文、专有名词或外文术语不算正文主语言。
8. 出版年代风险单独记录。仅因书旧不要直接判 DROP，但明显过时且可能造成知识错误时至少进入 REVIEW。
9. 区分可规则清洗的问题与不可恢复的问题。页眉页脚、页码、HTML 标签、固定水印等通常可规则清洗；大面积语义错识、串栏、漏文和错误脚本混排通常不可仅靠规则恢复。
10. 评级依据是“能否抽取可靠的学科知识点”，不是文体偏好。知识密度低但文本正确的材料可进入 REVIEW；只有当绝大部分内容不形成可用知识，或 OCR/版面损坏使知识无法恢复时才 DROP。

本批次还有三个硬门槛：
11. “辞海类”必须在多数有效抽样片段中反复出现相对独立的词头、术语或主题条目，并紧随对应翻译、定义、释义、解释、属性说明或知识描述。双语词条对照可以成立；百科条目的解释可以很长。目录、索引或偶尔出现定义，不足以证明整本书是辞海类。
12. 必须先对每个抽样片段单独标注结构：compact_entries 表示能够识别稳定的词条边界，且翻译、释义或长篇解释与相应词条明确对应；long_article 表示章节、论文或叙事正文连续展开，缺少可重复识别的词条边界；frontmatter_index 表示封面、前言、目录、索引或纯参考文献；unclear 表示仅凭该片段无法判断。每个输入片段必须且只能标注一次。
13. “有独立标题”不自动等于“辞海词条”，但也不能只因篇幅长而判为普通文章。若词头边界清楚，释义篇幅较长或包含多个自然段，且内容持续解释该词条，仍应标为 compact_entries。只有人物传记、历史叙事、案例分析、学术论文或章节正文缺少稳定、反复出现的词条边界时，才标为 long_article。书名含 Dictionary、Encyclopedia、辞典或百科，本身不得作为结构合格证据。
14. 排除 frontmatter_index 后，若真正的 long_article 占有效片段一半以上或明显多于 compact_entries，为避免边界争议必须判 DROP；若 compact_entries 不足有效片段的三分之二，或 long_article 超过有效片段的五分之一，不允许 PASS，最多 REVIEW。结构标签必须依据词条边界及内容归属，而不是依据单条释义的字数、段落数或页数。
15. 学科相关性必须根据实际片段判断。标题看似相关但正文主要属于其他学科时，subject_relevance 为 low 并判 DROP；跨学科但仍能稳定抽取本学科词条时可为 medium 并进入 REVIEW。
16. 只有 dictionary_structure=clear、subject_relevance=high、entry_definition_alignment=high、continuous_prose_dominant=false，且逐段结构计数同时通过第14条时才允许 PASS。结构混合、相关性中等或词头释义对应中等时最多 REVIEW；结构缺失、相关性低、词头释义对应低或长篇正文占主导时必须 DROP。

只允许给出三级结论：
- PASS：全部抽样片段总体语义可靠，未见明确的系统性 OCR/版面解析问题；最多只有少量可规则清洗的噪声，可直接进入后续流程。
- REVIEW：总体可能可用，但存在局部明确错误、图表或公式依赖、年代风险、判断不确定，必须人工抽查 PDF/MD 后决定。
- DROP：大面积语义不可读、乱码重复、严重串栏漏行、核心内容主要丢失、正文大量为非中英文，或错误系统性且无法通过规则清洗。

不要声称看过 PDF。问题证据必须引用输入片段中真实存在的短锚点。没有充分证据时优先 REVIEW，不要直接 DROP。

只输出一个 JSON 对象，不要输出 Markdown 代码块。字段必须完整：
{
  "title_guess": "书名，无法判断则用输入标题",
  "document_type": "dictionary|encyclopedia|textbook|monograph|handbook|standard|proceedings|yearbook|image_heavy|mixed|other",
  "dominant_languages": ["zh", "en"],
  "substantial_non_zh_en": false,
  "publication_year": null,
  "age_risk": "low|medium|high|unknown",
  "semantic_quality": "high|medium|low",
  "ocr_quality": "high|medium|low",
  "knowledge_density": "high|medium|low",
  "knowledge_extraction_suitability": "high|medium|low",
  "dictionary_structure": "clear|mixed|absent",
  "subject_relevance": "high|medium|low",
  "entry_definition_alignment": "high|medium|low",
  "continuous_prose_dominant": false,
  "sample_structure_labels": [
    {"sample_no": 1, "structure": "compact_entries|long_article|frontmatter_index|unclear"}
  ],
  "primary_extraction_obstacles": ["最多4项简短障碍"],
  "rule_cleanable": true,
  "decision": "PASS|REVIEW|DROP",
  "summary": "中文简要结论，说明为什么属于该级",
  "problems": [
    {
      "sample_no": 1,
      "problem_type": "非辞海结构|学科不相关|词头释义不对应|语义不通|术语错识|串栏错行|漏行粘连|重复污染|非中英文|图表公式缺失|格式噪声|年代风险|其他",
      "anchor": "不超过20字的真实原文锚点",
      "reason": "问题说明",
      "severity": "minor|major|critical",
      "rule_cleanable": false
    }
  ],
  "manual_review_targets": [
    {"sample_no": 1, "anchor": "回查锚点", "reason": "人工需要确认什么"}
  ],
  "confidence": 0.0
}

problems 最多 6 条，manual_review_targets 最多 4 条。锚点严禁超过20字；遇到长重复串，只引用开头10字并加省略号，绝对不要抄写整段重复内容。PASS 的 problems 可以为空；REVIEW 和 DROP 必须给出至少一条可核查证据。若输入提供了书型提示或抽取重点，将其作为辅助信息，但最终结论必须以实际片段为证据。
""".strip()


STRICT_TEXT_QUALITY_APPENDIX = r"""

本次启用“严格正文质量模式”，在上述标准上追加以下硬要求：
17. 必须逐段区分 body（正文）、non_body（封面、目录、索引、参考文献、页眉页脚等明确非正文）和 unclear。只有明确的 non_body 可以忽略文本不通顺；不得把疑似坏 OCR 的正文标成 non_body 来规避审核。
18. 任何一个正文片段只要存在一个可确认的明确错字、OCR 错识、语句不通、串栏错行、漏行粘连或语义断裂，整本书必须判为 DROP。专业罕见词、合法旧式用词、抽样从句中间截断均不算明确错误；无法确认时标为 unclear，并判 REVIEW。
19. 对每个正文片段进行语言分析。正文实质内容只允许简体中文和英文；只要正文混入成段的其他语言，必须判为 DROP。少量外文术语、专名或引文不算其他语言正文。
20. “候选词头”只统计看起来要作为知识点名称抽取的独立词头、术语或主题条目，不统计释义正文里的普通名词。泛化或非知识点词头包括“问题、情况、方法、概述、介绍、其他、第一章”等缺少稳定学科含义的泛称、章节标签、索引导航词、纯人名地名及无法独立形成知识点的条目。
21. 对全部正文片段汇总候选词头数量以及泛化或非知识点词头数量。后者占候选词头比例超过 {generic_limit_percent} 时必须判为 DROP；候选词头证据少于 {min_candidate_headwords} 个时不允许 PASS，最多 REVIEW。比例等于阈值仍可通过。
22. 严格模式下，PASS 必须同时满足：所有正文片段均 fluent、明确文本错误数为0、正文语言仅为 zh/en、候选词头证据充分、泛化或非知识点词头比例不超过阈值，并且此前辞海结构硬门槛也通过。

除原有字段外，必须额外输出 sample_quality_labels，且覆盖每个输入片段一次：
"sample_quality_labels": [
  {
    "sample_no": 1,
    "content_role": "body|non_body|unclear",
    "languages": ["zh", "en", "other"],
    "fluency": "fluent|error|unclear",
    "definite_text_error_count": 0,
    "candidate_headword_count": 0,
    "generic_or_nonknowledge_headword_count": 0
  }
]

计数必须是非负整数，generic_or_nonknowledge_headword_count 不得大于 candidate_headword_count。正文出现明确错误、其他语言或泛化词头超限时，problems 必须给出相应片段和真实短锚点。
""".strip()


KNOWLEDGE_HEADWORD_TYPES = (
    "discipline_concept",
    "method_technology_system",
    "object_material_device",
    "person_work_event",
    "general_language",
    "trivia_anecdote",
    "navigation",
)
DEFAULT_ACCEPTED_HEADWORD_TYPES = (
    "discipline_concept",
    "method_technology_system",
    "object_material_device",
)


KNOWLEDGE_VALUE_POLICY_VERSION = "qualitative_v2_20260907_r2"
PASS_CHALLENGE_POLICY_VERSION = "pass_challenge_v3_20260907"

PASS_CHALLENGE_STRONG_CLAIM_TYPES = {
    "concept_definition",
    "method_or_process",
    "feature_or_relation_analysis",
    "causal_explanation",
    "entity_with_analysis",
}
PASS_CHALLENGE_PAYLOADS = {
    "conceptual_explanation",
    "entity_analysis",
    "entity_factography",
    "quotation_source",
    "language_lookup",
    "trivia_rankings",
    "narrative_chapter",
    "list_or_catalog",
    "mixed",
    "unclear",
}
PASS_CHALLENGE_PASS_PAYLOADS = {
    "conceptual_explanation",
    "entity_analysis",
    "mixed",
}
PASS_CHALLENGE_RISKY_TITLE_RE = re.compile(
    r"(?:trivia|anecdote|quotation|quotes?|proverbs?|sayings?|"
    r"biographical\s+dictionary|icons?\s+of|"
    r"趣闻|轶事|毒舌|名言名句|谚语|俗语|歇后语|实词|虚词|词汇|"
    r"小学生|中学生|高中|考试|考点|掌中宝)",
    re.IGNORECASE,
)

PASS_CHALLENGE_SYSTEM_PROMPT = r"""
你是知识型参考书 PASS 结论的独立反证审核员。输入是一组重新挑选的 Markdown 原文片段。不要信任此前模型结论，也不要根据书名、出版社、名气或“看起来像词典/百科”放行；只根据输入原文判断这本书的主体是否能稳定抽取可靠的学科知识。

允许的有效知识不限于短定义。以下内容可作为强证据：概念定义；方法、技术或过程说明；特征、关系、机制或因果分析；人物、作品、机构、事件等实体与本学科之间有实质分析的条目。实体只列出生卒年、职位、作品名、榜单、轶事或其他元数据，不是强证据。

以下内容不能单独支撑 PASS：人名/作品/地名名单，传记或情节叙述，排名与趣闻，名言出处，普通语言翻译或查词，目录索引，书目清单，以及只有标题没有实质解释的条目。长条目可以合格，但必须持续解释同一个词头并提供学科知识；不能因为篇幅长而直接否定，也不能因为标题独立而直接通过。

逐条列出证据。headword 和 anchor 必须逐字来自对应输入片段或其上下文，不能改写；knowledge_claim 才允许概括。只有 recommendation=PASS、主体内容属于 conceptual_explanation/entity_analysis/mixed，且至少从 3 个不同样本取得 3 条可核验的强证据，程序才会维持 PASS。证据不足、主体混杂或无法确定时用 REVIEW。你可以建议 DROP，但程序只会将原 PASS 降为 REVIEW，交给人工处理。

只输出一个 JSON 对象，不要输出 Markdown 代码块：
{
  "recommendation": "PASS|REVIEW|DROP",
  "dominant_payload": "conceptual_explanation|entity_analysis|entity_factography|quotation_source|language_lookup|trivia_rankings|narrative_chapter|list_or_catalog|mixed|unclear",
  "summary": "中文简短结论",
  "evidence": [
    {
      "sample_no": 1,
      "headword": "原文真实词头",
      "anchor": "原文真实解释锚点",
      "claim_type": "concept_definition|method_or_process|feature_or_relation_analysis|causal_explanation|entity_with_analysis|metadata_fact|plot_or_biography|ranking_or_trivia|quotation_or_source|language_gloss|list_or_catalog|nonentry_section",
      "knowledge_claim": "该原文能或不能提供什么学科知识"
    }
  ]
}

evidence 最多 8 条。不要声称看过 PDF。若找不到 3 个独立强证据，不得输出 PASS。
""".strip()

KNOWLEDGE_VALUE_APPENDIX = r"""
本次采用“定性知识可用性模式”。逐项阅读所有正文样本及提供的原文上下文，以主体内容能否抽出可靠学科知识为依据。没有词头类别白名单，也没有词头比例、最少词头个数等评级门槛。不要输出 sample_headword_type_counts，不得自行按人名、作品、普通词语的占比降级。

17. 词头类型与学科价值分开判断。人物、作品、机构、事件、文化现象若有明确学科解释、特征、关系、方法、背景分析或事实，可以有效。纯姓名目录和作品目录不自动有效；普通语言学习、考试记忆练习、趣闻轶事若缺少学科分析，其主体不适用。成语典故、作品鉴赏与民俗条目须看实际解释，不能只按名称或类型排除。
语言是内容的载体，不等于目标学科。对任何学科，若主体是英语单词记忆、考试考点、词义辨析练习，应标 general_language_practice、usability=low、DROP。attend=参加、charge=收费、create=创造等普通词义不能作为文学或其他专业学科的知识证据；“这是词典、翻译通顺”不能说明学科适用。文学有效证据包括文学体裁、叙事方法、作品特征、文学流派等，也可以是确有文学分析的作家作品。对于其他学科按同样原则判断，不能把普通词汇释义包装成专业知识。
18. 先排除明确非正文：封面、目录、序言、全书引言、索引、参考文献、致谢、作者/插画者简介、出版广告。条目中用来解释概念的例子、诗歌引文、小标题和分析模型是条目的一部分；例如 Model 1 不自动成为新词条。跨学科对象只要能稳定提供本学科知识即可相关，不应仅因同时涉及历史、文化、政治等就判相关性低。
19. compact_entries=短词条；long_entry=具有真实词头并持续解释该词头的长百科条目，包括有学科分析的人物作品条目；long_article=确实缺少反复独立条目边界的连续章节或文章。uniform 片段可能在词条中间开始或结束，须结合 context_before、context_after、preceding_entry 判断；无足够上下文时标 unclear，不得由片段切断推断原书漏字、缺词头或不可配对。Markdown 井号也可能只是内部小标题。
20. 对主体适用性作定性判断：high=主体是可抽取学科知识的词条，允许少量普通词或导航；mixed=实质混合、仍需进一步确认；low=主体为纯名单、泛泛内容、普通词汇练习、无学科价值的材料；unclear=样本不足以判断。不得将所有人物、作品或长词条直接划为 low。对确实以普通考试词汇为主的书，不要把通顺翻译当成该学科知识。
21. 知识价值判断必须列出原文证据。high 应给出来自不同正文片段的 2 至 4 个真实词头和对应释义短锚点；只引用原文，不生成原文没有的解释。mixed/low/unclear 在 problems 中说明真实原因，缺少证据时优先 REVIEW，不能靠比例估计直接 DROP。
22. 词头、同义词和多语种对应词也属于正文语言证据。成组的法、德、西班牙等词头需记录实际语言代码，即使释义为英文也不能忽略。简中、英文可单独或混合；繁体主体和实质非中英文继续 DROP。少量书名、引用、出处、专名以及术语词源说明不等于多语种正文。
23. 每段给出 content_role 与 issue_basis。source_damage 仅指上下文已证实的错位、漏文或语义损坏；sample_truncation 表示仅由于取样切断，不证明源文件坏；non_entry_content 表示真的没有词条结构的正文；uncertain 表示待确认。明确非正文不参与正文质量判断。多个独立正文片段已证实系统性不可恢复损坏才 DROP；局部真实损坏 REVIEW，可确定性清洗的噪声可 PASS。不要把猜测纠错称为规则清洗。
24. 最终 PASS 需主体知识可用、学科相关、词条释义可靠、文本可用和语言合格；REVIEW 用于实质混合、局部损坏或证据不足；DROP 需主体不适用、真实非辞海结构、低相关性、系统性不可恢复损坏或不合格语言。词头比例不会触发 PASS、REVIEW 或 DROP。

除了基础 JSON 字段，必须输出：
"knowledge_value_assessment": {
  "usability": "high|mixed|low|unclear",
  "dominant_content": "disciplinary_entries|general_language_practice|name_list|nonreference_prose|mixed|unclear",
  "examples": [
    {"sample_no": 1, "headword": "原文真实词头", "anchor": "对应释义的原文短锚点"}
  ]
},
"sample_knowledge_value_flags": [
  {
    "sample_no": 1,
    "content_role": "body|non_body|unclear",
    "languages": ["zh", "en"],
    "entry_extraction_status": "usable|rule_cleanable|unusable|unclear",
    "issue_basis": "none|source_damage|sample_truncation|non_entry_content|uncertain"
  }
]
两个逐段数组必须覆盖全部输入 sample_no，各出现一次，结构与角色保持一致。knowledge_value_assessment.examples 最多4个，headword 和 anchor 不超过60字符。problems 的 anchor 必须在对应片段或附带上下文中真实存在；严禁使用“词头统计”等人工占位锚点。不得声称看过 PDF。
""".strip()

def build_system_prompt(
    strict_text_quality=False,
    max_generic_headword_ratio=0.15,
    min_candidate_headwords=20,
    knowledge_value_mode=False,
    max_nonknowledge_headword_ratio=0.15,
    drop_nonknowledge_headword_ratio=0.50,
    min_headword_evidence=10,
):
    appendices = []
    if strict_text_quality:
        appendix = STRICT_TEXT_QUALITY_APPENDIX.replace(
            "{generic_limit_percent}", f"{max_generic_headword_ratio:.0%}"
        ).replace(
            "{min_candidate_headwords}", str(min_candidate_headwords)
        )
        appendices.append(appendix)
    if knowledge_value_mode:
        appendix = KNOWLEDGE_VALUE_APPENDIX.replace(
            "{review_ratio}", f"{max_nonknowledge_headword_ratio:.0%}"
        ).replace(
            "{drop_ratio}", f"{drop_nonknowledge_headword_ratio:.0%}"
        ).replace(
            "{min_evidence}", str(min_headword_evidence)
        )
        appendices.append(appendix)
    base_prompt = SYSTEM_PROMPT
    if knowledge_value_mode:
        if strict_text_quality:
            raise ValueError("定性知识模式与旧严格正文模式不能同时启用")
        lines = base_prompt.splitlines()
        lines = [
            "14. 逐段结构只作为原文证据；百科长词条与短词条同等有效。取样截断、内部示例、参考文献和非正文不等于缺失词条结构。"
            if line.startswith("14. ") else
            "16. 根据主体实际知识价值、学科相关性、词条对应、语言和真实解析质量分级。补充模式中的定性口径优先，不按逐段结构比例机械降级。"
            if line.startswith("16. ") else line
            for line in lines
        ]
        base_prompt = "\n".join(lines)
    return "\n\n".join([base_prompt, *appendices])


def read_text(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


_TRADITIONAL_CONVERTER = None


def traditional_chinese_stats(text):
    cjk = "".join(char for char in text if "\u4e00" <= char <= "\u9fff")
    if len(cjk) < 500:
        return {
            "traditional_chinese_body": False,
            "cjk_chars": len(cjk),
            "traditional_variant_chars": 0,
            "traditional_ratio": 0.0,
        }
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise RuntimeError(
            "繁体硬门槛需要 opencc，请安装 opencc-python-reimplemented"
        ) from exc
    global _TRADITIONAL_CONVERTER
    if _TRADITIONAL_CONVERTER is None:
        _TRADITIONAL_CONVERTER = OpenCC("t2s")
    simplified = _TRADITIONAL_CONVERTER.convert(cjk)
    changed = sum(left != right for left, right in zip(cjk, simplified))
    changed += abs(len(cjk) - len(simplified))
    ratio = changed / max(1, len(cjk))
    return {
        "traditional_chinese_body": changed >= 500 and ratio >= 0.05,
        "cjk_chars": len(cjk),
        "traditional_variant_chars": changed,
        "traditional_ratio": round(ratio, 6),
    }


FOREIGN_LATIN_DIACRITICS = frozenset(
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØŒÙÚÛÜÝŸß"
    "àáâãäåæçèéêëìíîïñòóôõöøœùúûüýÿ"
)
NON_ZH_EN_SCRIPTS = re.compile(
    r"[\u0370-\u052f\u0590-\u08ff\u0900-\u0dff\u0e00-\u0e7f"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]"
)


def non_zh_en_heading_stats(text):
    headings = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", text)
    ]
    marked_aliases = set()
    script_headings = set()
    signal_chars = 0
    for index, heading in enumerate(headings):
        letters = [char for char in heading if char.isalpha()]
        uppercase_ratio = (
            sum(char.isupper() for char in letters) / len(letters)
            if letters else 0.0
        )
        segments = [segment.strip() for segment in heading.split(",")]
        alias_candidate = (
            len(segments) >= 3
            and len(heading) <= 140
            and not any(char.isdigit() for char in heading)
            and uppercase_ratio >= 0.85
            and all(1 <= len(segment.split()) <= 5 for segment in segments)
        )
        diacritics = [char for char in heading if char in FOREIGN_LATIN_DIACRITICS]
        scripts = NON_ZH_EN_SCRIPTS.findall(heading)
        signal_chars += len(diacritics) + len(scripts)
        if diacritics and alias_candidate:
            marked_aliases.add(index)
        if scripts:
            script_headings.add(index)

    signaled = set()
    if len(marked_aliases) >= 5:
        signaled.update(marked_aliases)
    if len(script_headings) >= 5 and signal_chars >= 20:
        signaled.update(script_headings)
    return {
        "non_zh_en_heading_signal": len(signaled) >= 5,
        "non_zh_en_heading_count": len(signaled),
        "non_zh_en_heading_chars": signal_chars,
    }


def distributed_samples(text, sample_count=12, chunk_chars=1200):
    if not text:
        return [{"sample_no": 1, "line_start": 1, "text": "[EMPTY DOCUMENT]"}]
    count = min(sample_count, max(1, math.ceil(len(text) / chunk_chars)))
    max_start = max(0, len(text) - chunk_chars)
    starts = [0] if count == 1 else [round(i * max_start / (count - 1)) for i in range(count)]
    samples = []
    seen = set()
    for start in starts:
        if start > 0:
            newline = text.find("\n", start)
            if 0 <= newline < start + 160:
                start = newline + 1
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + chunk_chars // 2:
                end = newline
        chunk = text[start:end].strip()
        if not chunk or chunk in seen:
            continue
        seen.add(chunk)
        samples.append(
            {
                "sample_no": len(samples) + 1,
                "line_start": text.count("\n", 0, start) + 1,
                "text": chunk,
            }
        )
    return samples


def hybrid_entry_samples(text, sample_count=12, chunk_chars=1200):
    if not text:
        return [{"sample_no": 1, "line_start": 1, "sampling_kind": "uniform",
                 "text": "[EMPTY DOCUMENT]"}]
    headings = list(re.finditer(r"(?m)^#{1,6}\s+\S[^\n]*", text))
    nonbody = re.compile(
        r"^(?:about (?:the )?(?:author|editor|illustrator)|acknowledg|"
        r"foreword|preface|contents|table of contents|index$|"
        r"(?:general )?bibliograph|references$|further readings?$|"
        r"作者简介|编者简介|插画者简介|致谢|目录|参考文献|索引$|序言|前言)", re.I
    )
    excluded = []
    for index, match in enumerate(headings):
        title = re.sub(r"^#+\s*", "", match.group()).strip()
        is_intro = title.lower() == "introduction" and match.start() < len(text) * 0.2
        if nonbody.search(title) or is_intro:
            following = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            end = min(following, match.start() + 4000)
            if re.match(r"about .*?(?:author|editor|illustrator)", title, re.I) and match.start() > len(text) * 0.65:
                end = len(text)
            excluded.append((match.start(), end))

    def excluded_at(position):
        return any(start <= position < end for start, end in excluded)

    candidates = [match.start() for match in headings if not excluded_at(match.start())]
    for match in re.finditer(r"(?m)(?:\A|(?<=\n\n))([^\n]{2,180})", text):
        first = match.group(1).strip()
        if (
            len(first.split()) <= 16
            and (re.match(r"[【\[][^】\]]{1,60}[】\]]", first)
                 or re.match(r"[^#\n]{2,70}?(?:\s+\([^)]+\)|[.:：])\s+\S", first))
            and not excluded_at(match.start())
        ):
            candidates.append(match.start())
    candidates = sorted(set(candidates))
    selected = []
    used_entries = set()

    def add(position, kind):
        position = max(0, min(position, max(0, len(text) - 1)))
        if excluded_at(position):
            return
        if kind == "uniform" and position:
            line = text.find("\n", position, min(len(text), position + 160))
            if line >= 0:
                position = line + 1
        if excluded_at(position):
            return
        end = min(len(text), position + chunk_chars)
        for excluded_start, _ in excluded:
            if position < excluded_start < end:
                end = excluded_start
                break
        if end - position < min(200, chunk_chars):
            return
        line = text.rfind("\n", position, end)
        if line > position + chunk_chars // 2:
            end = line
        chunk = text[position:end].strip()
        if not chunk:
            return
        for existing in selected:
            overlap = max(0, min(end, existing["char_end"]) - max(position, existing["char_start"]))
            if overlap > 0.25 * min(end - position, existing["char_end"] - existing["char_start"]):
                return
        preceding = next((p for p in reversed(candidates) if p <= position), None)
        if kind == "entry_anchor" and preceding in used_entries:
            return
        if preceding is not None:
            used_entries.add(preceding)
        context_start = max(0, position - 1200)
        selected.append({
            "sample_no": 0, "line_start": text.count("\n", 0, position) + 1,
            "char_start": position, "char_end": end, "sampling_kind": kind, "text": chunk,
            "context_before": text[context_start:position],
            "context_after": text[end:min(len(text), end + 350)],
            "preceding_entry": text[preceding:min(position, preceding + 600)] if preceding is not None else "",
        })

    anchor_target = sample_count // 2
    if candidates and anchor_target:
        for index in range(anchor_target):
            add(candidates[round(index * (len(candidates) - 1) / max(1, anchor_target - 1))], "entry_anchor")
    for count in (sample_count, sample_count * 4, sample_count * 12):
        for index in range(count):
            if len(selected) >= sample_count:
                break
            add(round(index * max(0, len(text) - chunk_chars) / max(1, count - 1)), "uniform")
    if not selected:
        return distributed_samples(text, sample_count, chunk_chars)
    selected.sort(key=lambda sample: sample["char_start"])
    for number, sample in enumerate(selected, 1):
        sample["sample_no"] = number
    return selected


def compact_repetition_for_prompt(text):
    def replace(match):
        unit = match.group(1)
        count = len(match.group(0)) // len(unit)
        return f"{unit[:12]}【该单元连续重复{count}次，已压缩】"

    text = re.sub(
        r"[A-Za-z0-9+/=]{120,}",
        lambda match: f"{match.group(0)[:16]}【疑似Base64连续{len(match.group(0))}字符，已压缩】",
        text,
    )
    text = re.sub(r"(.{17,120}?)\1{3,}", replace, text, flags=re.S)
    text = re.sub(r"(.{1,16}?)\1{5,}", replace, text, flags=re.S)
    return text


def guess_title(path, text):
    for line in text.splitlines()[:80]:
        value = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if 2 <= len(value) <= 100 and not value.lower().startswith(("http://", "https://")):
            return value
    return path.stem


def load_subject_definitions(config_path=None):
    if config_path is None:
        return [
            {
                "name": name,
                "code": code,
                "sources": [str(relative) for relative in relatives],
                "book_type_hint": "",
                "extraction_focus": "学科概念、定义、属性、关系与事实",
                "accepted_headword_types": list(DEFAULT_ACCEPTED_HEADWORD_TYPES),
            }
            for name, code, relatives in SUBJECTS
        ]

    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    definitions = config.get("subjects") if isinstance(config, dict) else config
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("配置文件必须是非空数组，或包含非空 subjects 数组")
    required = {"name", "code", "sources"}
    for index, definition in enumerate(definitions, 1):
        missing = required - set(definition)
        if missing:
            raise ValueError(f"第 {index} 个学科配置缺少字段: {sorted(missing)}")
        if not isinstance(definition["sources"], list) or not definition["sources"]:
            raise ValueError(f"{definition['name']} 的 sources 必须是非空数组")
    return definitions


def subject_specs(root, config_path=None):
    specs = []
    seen_names = set()
    seen_codes = set()
    for definition in load_subject_definitions(config_path):
        name = str(definition["name"]).strip()
        code = str(definition["code"]).strip().upper()
        if not name or not code:
            raise ValueError("学科 name 和 code 不能为空")
        if name in seen_names or code in seen_codes:
            raise ValueError(f"学科名称或代码重复: {name} / {code}")
        seen_names.add(name)
        seen_codes.add(code)
        sources = []
        for value in definition["sources"]:
            source = Path(value)
            sources.append(source if source.is_absolute() else root / source)
        specs.append({
            "name": name,
            "code": code,
            "sources": sources,
            "book_type_hint": str(definition.get("book_type_hint", "")).strip(),
            "extraction_focus": str(definition.get(
                "extraction_focus", "学科概念、定义、属性、关系与事实"
            )).strip(),
            "sample_count": definition.get("sample_count"),
            "chunk_chars": definition.get("chunk_chars"),
            "accepted_headword_types": list(definition.get(
                "accepted_headword_types", DEFAULT_ACCEPTED_HEADWORD_TYPES
            )),
        })
    return specs


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare(
    root,
    output,
    config_path=None,
    default_sample_count=12,
    default_chunk_chars=1200,
    knowledge_value_mode=False,
):
    manifests = output / "输入清单"
    all_rows = []
    summary = []
    for spec in subject_specs(root, config_path):
        for source in spec["sources"]:
            if not source.exists():
                raise FileNotFoundError(f"学科输入目录不存在: {source}")
        source_paths = [
            (source, path)
            for source in spec["sources"]
            for path in source.rglob("*.md")
        ]
        source_paths.sort(key=lambda pair: str(pair[1]).lower())
        sample_count = int(spec["sample_count"] or default_sample_count)
        chunk_chars = int(spec["chunk_chars"] or default_chunk_chars)
        if sample_count < 1 or chunk_chars < 200:
            raise ValueError(f"{spec['name']} 的抽样参数不合理")
        records = []
        for index, (source, path) in enumerate(source_paths, 1):
            text = read_text(path)
            traditional_stats = traditional_chinese_stats(text)
            heading_language_stats = non_zh_en_heading_stats(text)
            relative = path.relative_to(source)
            if len(spec["sources"]) > 1:
                relative = Path(source.name) / relative
            samples = (
                hybrid_entry_samples(text, sample_count, chunk_chars)
                if knowledge_value_mode
                else distributed_samples(text, sample_count, chunk_chars)
            )
            record = {
                "short_id": f"{spec['code']}{index:04d}",
                "subject": spec["name"],
                "source_path": str(path.resolve()),
                "relative_path": str(relative),
                "file_name": path.name,
                "title": guess_title(path, text),
                "book_type_hint": spec["book_type_hint"],
                "extraction_focus": spec["extraction_focus"],
                "accepted_headword_types": spec["accepted_headword_types"],
                "chars": len(text),
                "lines": text.count("\n") + 1 if text else 0,
                "samples": samples,
                **traditional_stats,
                **heading_language_stats,
            }
            records.append(record)
            all_rows.append({key: record[key] for key in (
                "short_id", "subject", "source_path", "relative_path", "file_name",
                "title", "chars", "lines"
            )})
        manifest = {
            "subject": spec["name"],
            "subject_code": spec["code"],
            "source_dirs": [str(source.resolve()) for source in spec["sources"]],
            "book_type_hint": spec["book_type_hint"],
            "extraction_focus": spec["extraction_focus"],
            "sampling_rule": (
                f"最多{sample_count}个均匀加词头锚定片段，每片段最多{chunk_chars}字符"
                if knowledge_value_mode
                else f"最多{sample_count}个均匀分布片段，每片段最多{chunk_chars}字符"
            ),
            "records": records,
        }
        write_json(manifests / f"{spec['name']}.json", manifest)
        summary.append({
            "subject": spec["name"],
            "count": len(records),
            "sources": [str(source) for source in spec["sources"]],
        })
    write_csv(
        output / "全学科输入书目.csv",
        all_rows,
        ["short_id", "subject", "title", "file_name", "chars", "lines", "relative_path", "source_path"],
    )
    write_json(output / "输入整理摘要.json", {"total": len(all_rows), "subjects": summary})
    return summary


def normalize_qualitative_labels(result, samples=None):
    labels = result.get("sample_knowledge_value_flags")
    if not isinstance(labels, list) or not labels:
        raise ValueError("qualitative mode requires sample_knowledge_value_flags")
    structure = {label["sample_no"]: label for label in result["sample_structure_labels"]}
    by_number = {}
    for label in labels:
        if not isinstance(label, dict):
            raise ValueError("invalid qualitative sample flag")
        number = label.get("sample_no")
        if type(number) is not int or number not in structure or number in by_number:
            raise ValueError("qualitative flags must cover each sample exactly once")
        for field, allowed in (
            ("content_role", {"body", "non_body", "unclear"}),
            ("entry_extraction_status", {"usable", "rule_cleanable", "unusable", "unclear"}),
            ("issue_basis", {"none", "source_damage", "sample_truncation", "non_entry_content", "uncertain"}),
        ):
            if label.get(field) not in allowed:
                raise ValueError(f"invalid qualitative {field}")
        languages = label.get("languages")
        if not isinstance(languages, list) or not all(isinstance(x, str) and x.strip() for x in languages):
            raise ValueError("invalid qualitative languages")
        if label["content_role"] == "body" and not languages:
            raise ValueError("body sample needs a language label")
        label["languages"] = list(dict.fromkeys(x.strip().lower() for x in languages))
        if label["content_role"] == "non_body":
            structure[number]["structure"] = "frontmatter_index"
        elif label["issue_basis"] == "sample_truncation":
            if structure[number]["structure"] == "long_article":
                structure[number]["structure"] = "unclear"
            label["entry_extraction_status"] = "unclear"
        by_number[number] = label
    if set(by_number) != set(structure):
        raise ValueError("qualitative flags missing samples")
    assessment = result.get("knowledge_value_assessment")
    if not isinstance(assessment, dict) or assessment.get("usability") not in {"high", "mixed", "low", "unclear"}:
        raise ValueError("invalid knowledge_value_assessment usability")
    if assessment.get("dominant_content") not in {
        "disciplinary_entries", "general_language_practice", "name_list", "nonreference_prose", "mixed", "unclear"
    }:
        raise ValueError("invalid dominant_content")
    examples = assessment.get("examples")
    if not isinstance(examples, list) or len(examples) > 4:
        raise ValueError("knowledge examples must be a list of at most four items")
    source_samples = {s["sample_no"]: s for s in samples or []}
    verified_examples = []
    unverified_examples = []
    for example in examples:
        if not isinstance(example, dict) or example.get("sample_no") not in by_number:
            raise ValueError("invalid knowledge example sample_no")
        for field in ("headword", "anchor"):
            value = example.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid knowledge example {field}")
        if by_number[example["sample_no"]]["content_role"] != "body":
            unverified_examples.append({**example, "rejection_reason": "non_body_sample"})
            continue
        if samples:
            sample = source_samples[example["sample_no"]]
            source = "\n".join(str(sample.get(k, "")) for k in (
                "text", "context_before", "context_after", "preceding_entry"
            ))
            normalized_source = re.sub(r"\s+", "", source).casefold()
            missing_fields = []
            for field in ("headword", "anchor"):
                needle = re.sub(r"\s+", "", example[field]).casefold()
                if needle not in normalized_source:
                    missing_fields.append(field)
            if missing_fields:
                unverified_examples.append({**example, "missing_fields": missing_fields})
                continue
        verified_examples.append(example)
    assessment["examples"] = verified_examples
    result["unverified_knowledge_examples"] = unverified_examples
    return by_number


def apply_qualitative_gate(result, flags):
    body = [flag for flag in flags.values() if flag["content_role"] == "body"]
    other = [flag for flag in body if any(x not in {"zh", "en"} for x in flag["languages"])]
    damaged = [flag for flag in body if flag["entry_extraction_status"] == "unusable"
               and flag["issue_basis"] == "source_damage"]
    assessment = result["knowledge_value_assessment"]
    result.update(
        knowledge_value_policy=KNOWLEDGE_VALUE_POLICY_VERSION,
        knowledge_value_tier=assessment["usability"],
        knowledge_other_language_samples=len(other),
        unusable_entry_samples=len(damaged),
        unclear_entry_samples=sum(flag["issue_basis"] == "uncertain" for flag in body),
        rule_cleanable_entry_samples=sum(flag["entry_extraction_status"] == "rule_cleanable" for flag in body),
        nonknowledge_headword_ratio=None,
        knowledge_value_evidence_count=len(assessment["examples"]),
    )
    adjustment = None
    if other:
        adjustment, reason = "DROP", "正文包含不合格语言。"
    elif assessment["usability"] == "low" or assessment["dominant_content"] in {"general_language_practice", "name_list"}:
        adjustment, reason = "DROP", "原文主体无法提供可用的本学科知识。"
    elif len(damaged) >= max(2, math.ceil(len(body) / 4)):
        adjustment, reason = "DROP", "多个独立正文片段证实存在不可恢复的源文损坏。"
    elif result["decision"] == "PASS":
        independent_examples = {e["sample_no"] for e in assessment["examples"]}
        if assessment["usability"] != "high" or len(body) < 4 or len(independent_examples) < 2:
            adjustment, reason = "REVIEW", "主体适用性或独立正文证据仍不足，需补充样本确认。"
        elif damaged:
            adjustment, reason = "REVIEW", "存在局部已证实的源文损坏，需要复核。"
    if adjustment and result["decision"] != adjustment:
        result["decision"] = adjustment
        result["decision_adjusted_by_knowledge_value_gate"] = True
        result["summary"] = f"[定性知识门槛调整为{adjustment}] " + result["summary"]
        if not result["problems"]:
            flag = next(iter(other or damaged or body or flags.values()))
            result["problems"].append({
                "sample_no": flag["sample_no"], "problem_type": "非中英文" if other else "其他",
                "anchor": "", "reason": reason, "severity": "major", "rule_cleanable": False,
            })
    return result


def pass_challenge_reasons(result, title=""):
    """Return auditable reasons for independently challenging a PASS result."""
    if result.get("decision") != "PASS":
        return []
    reasons = []
    title_text = " ".join((str(title or ""), str(result.get("title_guess") or "")))
    if PASS_CHALLENGE_RISKY_TITLE_RE.search(title_text):
        reasons.append("risky_title")
    counts = result.get("sample_structure_counts") or {}
    if int(counts.get("long_article", 0) or 0) >= 2:
        reasons.append("long_article_samples")
    return reasons


def _pass_challenge_sample_text(sample):
    return "\n".join(str(sample.get(field, "")) for field in (
        "text", "context_before", "context_after", "preceding_entry"
    ))


def _normalized_evidence_text(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _pass_challenge_anchor_matches(anchor, normalized_source):
    parts = [
        _normalized_evidence_text(part)
        for part in re.split(r"(?:\.\s*){3,}|…+", str(anchor or ""))
        if _normalized_evidence_text(part)
    ]
    if not parts:
        return False
    position = 0
    for part in parts:
        found = normalized_source.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def normalize_pass_challenge(response, samples):
    """Validate challenge output and verify every evidence anchor against source text."""
    if not isinstance(response, dict):
        raise ValueError("pass challenge response must be an object")
    recommendation = str(response.get("recommendation", "")).upper()
    if recommendation not in {"PASS", "REVIEW", "DROP"}:
        raise ValueError("invalid pass challenge recommendation")
    dominant_payload = response.get("dominant_payload")
    if dominant_payload not in PASS_CHALLENGE_PAYLOADS:
        raise ValueError("invalid pass challenge dominant_payload")
    evidence = response.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("pass challenge evidence must be a list")
    source_by_number = {sample.get("sample_no"): sample for sample in samples or []}
    verified = []
    unverified = []
    for raw in evidence:
        if not isinstance(raw, dict):
            raise ValueError("invalid pass challenge evidence item")
        item = {key: raw.get(key) for key in (
            "sample_no", "headword", "anchor", "claim_type", "knowledge_claim"
        )}
        if type(item["sample_no"]) is not int or item["sample_no"] not in source_by_number:
            raise ValueError("invalid pass challenge evidence sample_no")
        if not all(isinstance(item[field], str) and item[field].strip() for field in (
            "headword", "anchor", "claim_type", "knowledge_claim"
        )):
            raise ValueError("pass challenge evidence fields must be non-empty strings")
        source = _normalized_evidence_text(_pass_challenge_sample_text(
            source_by_number[item["sample_no"]]
        ))
        missing_fields = []
        if _normalized_evidence_text(item["headword"]) not in source:
            missing_fields.append("headword")
        if not _pass_challenge_anchor_matches(item["anchor"], source):
            missing_fields.append("anchor")
        if missing_fields:
            unverified.append({**item, "missing_fields": missing_fields})
        else:
            verified.append(item)
    strong = [item for item in verified if item["claim_type"] in PASS_CHALLENGE_STRONG_CLAIM_TYPES]
    normalized = {
        "policy_version": PASS_CHALLENGE_POLICY_VERSION,
        "recommendation": recommendation,
        "dominant_payload": dominant_payload,
        "summary": str(response.get("summary", "")).strip(),
        "verified_evidence": verified,
        "unverified_evidence": unverified,
        "strong_evidence_count": len(strong),
        "strong_evidence_sample_count": len({item["sample_no"] for item in strong}),
    }
    normalized["pass_supported"] = (
        recommendation == "PASS"
        and dominant_payload in PASS_CHALLENGE_PASS_PAYLOADS
        and normalized["strong_evidence_count"] >= 3
        and normalized["strong_evidence_sample_count"] >= 3
    )
    return normalized


def apply_pass_challenge(result, challenge, reasons):
    """Overlay a completed challenge without ever creating an automatic DROP."""
    result["pass_challenge_policy"] = PASS_CHALLENGE_POLICY_VERSION
    result["pass_challenge_triggered"] = bool(reasons)
    result["pass_challenge_reasons"] = list(reasons)
    result["pass_challenge_result"] = challenge
    result["pass_challenge_strong_evidence_count"] = challenge.get("strong_evidence_count", 0)
    result["pass_challenge_strong_evidence_sample_count"] = challenge.get(
        "strong_evidence_sample_count", 0
    )
    result["decision_before_pass_challenge"] = result.get("decision")
    result["decision_adjusted_by_pass_challenge"] = False
    if result.get("decision") == "PASS" and reasons and not challenge.get("pass_supported", False):
        result["decision"] = "REVIEW"
        result["decision_adjusted_by_pass_challenge"] = True
        summary = challenge.get("summary") or "独立反证复核未取得足够的学科知识证据。"
        result["summary"] = f"[PASS反证复核调整为REVIEW] {summary} " + result.get("summary", "")
    return result


def select_pass_challenge_samples(item, primary_result, max_samples=8):
    """Select a compact challenge set while retaining known risk evidence."""
    samples = list(item.get("samples") or [])
    if not samples or max_samples < 1:
        return []
    by_number = {sample.get("sample_no"): sample for sample in samples}
    priority_numbers = []
    for problem in primary_result.get("problems") or []:
        priority_numbers.append(problem.get("sample_no"))
    for label in primary_result.get("sample_structure_labels") or []:
        if label.get("structure") in {"long_article", "unclear"}:
            priority_numbers.append(label.get("sample_no"))
    assessment = primary_result.get("knowledge_value_assessment") or {}
    for example in assessment.get("examples") or []:
        priority_numbers.append(example.get("sample_no"))

    chosen_numbers = []
    for number in priority_numbers:
        if number in by_number and number not in chosen_numbers:
            chosen_numbers.append(number)
            if len(chosen_numbers) >= max_samples:
                return [copy.deepcopy(by_number[number]) for number in chosen_numbers]

    remaining_slots = min(max_samples, len(samples)) - len(chosen_numbers)
    if remaining_slots > 0:
        candidates = [sample for sample in samples if sample.get("sample_no") not in chosen_numbers]
        if remaining_slots >= len(candidates):
            fill = candidates
        elif remaining_slots == 1:
            fill = [candidates[len(candidates) // 2]]
        else:
            indexes = [round(index * (len(candidates) - 1) / (remaining_slots - 1))
                       for index in range(remaining_slots)]
            fill = [candidates[index] for index in indexes]
        chosen_numbers.extend(sample.get("sample_no") for sample in fill)
    return [copy.deepcopy(by_number[number]) for number in chosen_numbers]


def overlay_pass_challenges(primary_completed, challenge_completed, manifest_records):
    """Return copied primary records with complete challenge results overlaid."""
    merged = {}
    for source_path, primary in primary_completed.items():
        item = copy.deepcopy(primary)
        manifest = manifest_records.get(source_path)
        if manifest is None:
            raise ValueError(f"missing manifest record for pass challenge: {source_path}")
        title = item.get("input_title") or manifest.get("title", "")
        reasons = pass_challenge_reasons(item["result"], title)
        if reasons:
            challenge = challenge_completed.get(source_path)
            if not challenge or not challenge.get("ok") or not challenge.get("challenge"):
                raise ValueError(f"missing pass challenge result: {source_path}")
            item["result"] = apply_pass_challenge(item["result"], challenge["challenge"], reasons)
            item["pass_challenge_usage"] = challenge.get("usage")
            item["pass_challenge_latency_seconds"] = challenge.get("latency_seconds")
        else:
            item["result"].update(
                pass_challenge_policy=PASS_CHALLENGE_POLICY_VERSION,
                pass_challenge_triggered=False,
                pass_challenge_reasons=[],
                decision_before_pass_challenge=item["result"].get("decision"),
                decision_adjusted_by_pass_challenge=False,
                pass_challenge_strong_evidence_count=0,
                pass_challenge_strong_evidence_sample_count=0,
            )
        merged[source_path] = item
    return merged


def parse_pass_challenge_content(content, samples):
    content = re.sub(r"<think>.*?</think>", "", str(content), flags=re.S).strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("pass challenge response does not contain a JSON object")
    return normalize_pass_challenge(json.loads(content[start:end + 1]), samples)


def parse_json_content(
    content,
    expected_sample_count=None,
    strict_text_quality=False,
    max_generic_headword_ratio=0.15,
    min_candidate_headwords=20,
    knowledge_value_mode=False,
    max_nonknowledge_headword_ratio=0.15,
    drop_nonknowledge_headword_ratio=0.50,
    min_headword_evidence=10,
    accepted_headword_types=None,
    samples=None,
):
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response does not contain a JSON object")
    result = json.loads(content[start : end + 1])
    required = {
        "title_guess", "document_type", "dominant_languages", "substantial_non_zh_en",
        "publication_year", "age_risk", "semantic_quality", "ocr_quality",
        "knowledge_density", "knowledge_extraction_suitability",
        "dictionary_structure", "subject_relevance", "entry_definition_alignment",
        "continuous_prose_dominant", "sample_structure_labels",
        "primary_extraction_obstacles", "rule_cleanable", "decision", "summary", "problems",
        "manual_review_targets", "confidence",
    }
    missing = required - set(result)
    if missing:
        gate_fields = {
            "dictionary_structure", "subject_relevance", "entry_definition_alignment",
            "continuous_prose_dominant", "sample_structure_labels",
        }
        if missing & gate_fields:
            raise ValueError(f"missing dictionary gate fields: {sorted(missing & gate_fields)}")
        raise ValueError(f"missing fields: {sorted(missing)}")
    if result["decision"] not in {"PASS", "REVIEW", "DROP"}:
        raise ValueError("invalid decision")
    if not isinstance(result["dominant_languages"], list) or not result["dominant_languages"]:
        raise ValueError("dominant_languages must be a non-empty list")
    if not all(isinstance(language, str) and language.strip() for language in result["dominant_languages"]):
        raise ValueError("invalid dominant_languages")
    if not isinstance(result["substantial_non_zh_en"], bool):
        raise ValueError("substantial_non_zh_en must be boolean")
    if not isinstance(result["primary_extraction_obstacles"], list):
        raise ValueError("primary_extraction_obstacles must be a list")
    if not isinstance(result["problems"], list) or not isinstance(result["manual_review_targets"], list):
        raise ValueError("problem and review fields must be lists")
    for field, allowed in (
        ("dictionary_structure", {"clear", "mixed", "absent"}),
        ("subject_relevance", {"high", "medium", "low"}),
        ("entry_definition_alignment", {"high", "medium", "low"}),
    ):
        if result[field] not in allowed:
            raise ValueError(f"invalid {field}")
    if not isinstance(result["continuous_prose_dominant"], bool):
        raise ValueError("continuous_prose_dominant must be boolean")
    qualitative_flags = normalize_qualitative_labels(result, samples) if knowledge_value_mode else None
    labels = result["sample_structure_labels"]
    if not isinstance(labels, list) or not labels:
        raise ValueError("sample_structure_labels must be a non-empty list")
    allowed_structures = {"compact_entries", "long_article", "frontmatter_index", "unclear"}
    if knowledge_value_mode:
        allowed_structures.add("long_entry")
    sample_numbers = []
    structure_counts = Counter()
    for label in labels:
        if not isinstance(label, dict) or set(label) != {"sample_no", "structure"}:
            raise ValueError("invalid sample_structure_labels item")
        if not isinstance(label["sample_no"], int) or label["sample_no"] < 1:
            raise ValueError("invalid sample_structure_labels sample_no")
        if label["structure"] in {"mixed", "image_heavy"}:
            label["structure"] = "unclear"
        if label["structure"] not in allowed_structures:
            raise ValueError("invalid sample_structure_labels structure")
        sample_numbers.append(label["sample_no"])
        structure_counts[label["structure"]] += 1
    if len(sample_numbers) != len(set(sample_numbers)):
        raise ValueError("duplicate sample_structure_labels sample_no")
    if expected_sample_count is not None and set(sample_numbers) != set(
        range(1, expected_sample_count + 1)
    ):
        raise ValueError("sample_structure_labels must cover every input sample exactly once")
    substantive_count = len(labels) - structure_counts["frontmatter_index"]
    compact_count = structure_counts["compact_entries"]
    long_entry_count = structure_counts["long_entry"]
    entry_count = compact_count + long_entry_count
    long_count = structure_counts["long_article"]
    minimum_evidence = max(4, math.ceil(len(labels) / 2))
    long_article_dominant = (
        substantive_count > 0
        and long_count >= 3
        and long_count * 2 >= substantive_count
    )
    structure_pass_allowed = (
        substantive_count >= minimum_evidence
        and entry_count * 3 >= substantive_count * 2
        and long_count * 5 <= substantive_count
    )
    result["sample_structure_counts"] = dict(structure_counts)
    result["long_entry_samples"] = long_entry_count
    result["substantive_sample_count"] = substantive_count
    non_zh_en_language = (
        result["substantial_non_zh_en"]
        or any(
            language.strip().lower() not in {"zh", "en"}
            for language in result["dominant_languages"]
        )
    )
    hard_drop = (
        result["dictionary_structure"] == "absent"
        or result["subject_relevance"] == "low"
        or result["entry_definition_alignment"] == "low"
        or result["continuous_prose_dominant"]
        or (long_article_dominant and not knowledge_value_mode)
        or non_zh_en_language
    )
    pass_allowed = (
        result["dictionary_structure"] == "clear"
        and result["subject_relevance"] == "high"
        and result["entry_definition_alignment"] == "high"
        and not result["continuous_prose_dominant"]
        and (structure_pass_allowed or knowledge_value_mode)
        and not non_zh_en_language
    )
    result["decision_before_gate"] = result["decision"]
    result["decision_adjusted_by_gate"] = False
    if hard_drop and result["decision"] != "DROP":
        result["decision"] = "DROP"
        result["decision_adjusted_by_gate"] = True
        result["summary"] = "[辞海硬门槛调整为DROP] " + result["summary"]
    elif result["decision"] == "PASS" and not pass_allowed:
        result["decision"] = "REVIEW"
        result["decision_adjusted_by_gate"] = True
        result["summary"] = "[辞海硬门槛调整为REVIEW] " + result["summary"]

    result["strict_text_quality"] = bool(strict_text_quality)
    result["decision_before_strict_quality_gate"] = result["decision"]
    result["decision_adjusted_by_strict_quality_gate"] = False
    result["strict_body_error_samples"] = 0
    result["strict_unclear_body_samples"] = 0
    result["strict_body_has_other_language"] = False
    result["candidate_headword_count"] = 0
    result["generic_or_nonknowledge_headword_count"] = 0
    result["generic_headword_ratio"] = 0.0
    if strict_text_quality:
        if not 0 <= max_generic_headword_ratio <= 1:
            raise ValueError("max_generic_headword_ratio must be between 0 and 1")
        if min_candidate_headwords < 1:
            raise ValueError("min_candidate_headwords must be positive")
        quality_labels = result.get("sample_quality_labels")
        if not isinstance(quality_labels, list) or not quality_labels:
            raise ValueError("strict mode requires sample_quality_labels")
        quality_sample_numbers = []
        body_error_samples = []
        unclear_samples = []
        body_other_language_samples = []
        candidate_count = 0
        generic_count = 0
        required_quality_fields = {
            "sample_no", "content_role", "languages", "fluency",
            "definite_text_error_count", "candidate_headword_count",
            "generic_or_nonknowledge_headword_count",
        }
        for label in quality_labels:
            if not isinstance(label, dict) or set(label) != required_quality_fields:
                raise ValueError("invalid sample_quality_labels item")
            sample_no = label["sample_no"]
            if not isinstance(sample_no, int) or sample_no < 1:
                raise ValueError("invalid sample_quality_labels sample_no")
            quality_sample_numbers.append(sample_no)
            if label["content_role"] not in {"body", "non_body", "unclear"}:
                raise ValueError("invalid sample_quality_labels content_role")
            languages = label["languages"]
            if not isinstance(languages, list) or not languages or not all(
                isinstance(language, str) and language.strip() for language in languages
            ):
                raise ValueError("invalid sample_quality_labels languages")
            normalized_languages = []
            for language in languages:
                code = language.strip().lower()
                normalized = code if code in {"zh", "en", "other"} else "other"
                if normalized not in normalized_languages:
                    normalized_languages.append(normalized)
            label["languages"] = normalized_languages
            languages = normalized_languages
            if label["fluency"] not in {"fluent", "error", "unclear"}:
                raise ValueError("invalid sample_quality_labels fluency")
            for field in (
                "definite_text_error_count", "candidate_headword_count",
                "generic_or_nonknowledge_headword_count",
            ):
                if not isinstance(label[field], int) or isinstance(label[field], bool) or label[field] < 0:
                    raise ValueError(f"invalid sample_quality_labels {field}")
            if (
                label["generic_or_nonknowledge_headword_count"]
                > label["candidate_headword_count"]
            ):
                raise ValueError("generic headword count exceeds candidate headword count")

            if label["content_role"] == "body":
                candidate_count += label["candidate_headword_count"]
                generic_count += label["generic_or_nonknowledge_headword_count"]
                if label["fluency"] == "error" or label["definite_text_error_count"] > 0:
                    body_error_samples.append(sample_no)
                if label["fluency"] == "unclear":
                    unclear_samples.append(sample_no)
                if "other" in languages:
                    body_other_language_samples.append(sample_no)
            elif label["content_role"] == "unclear":
                unclear_samples.append(sample_no)

        if len(quality_sample_numbers) != len(set(quality_sample_numbers)):
            raise ValueError("duplicate sample_quality_labels sample_no")
        if expected_sample_count is not None and set(quality_sample_numbers) != set(
            range(1, expected_sample_count + 1)
        ):
            raise ValueError("sample_quality_labels must cover every input sample exactly once")

        generic_ratio = generic_count / candidate_count if candidate_count else 0.0
        result["strict_body_error_samples"] = len(body_error_samples)
        result["strict_unclear_body_samples"] = len(unclear_samples)
        result["strict_body_has_other_language"] = bool(body_other_language_samples)
        result["candidate_headword_count"] = candidate_count
        result["generic_or_nonknowledge_headword_count"] = generic_count
        result["generic_headword_ratio"] = round(generic_ratio, 6)

        strict_drop_reason = None
        strict_drop_sample = None
        strict_problem_type = "其他"
        if body_error_samples:
            strict_drop_reason = "正文片段存在明确错字、OCR错识或语义不通。"
            strict_drop_sample = body_error_samples[0]
            strict_problem_type = "语义不通"
        elif body_other_language_samples:
            strict_drop_reason = "正文片段包含实质性非中英文内容。"
            strict_drop_sample = body_other_language_samples[0]
            strict_problem_type = "非中英文"
        elif candidate_count and generic_ratio > max_generic_headword_ratio:
            strict_drop_reason = (
                f"泛化或非知识点词头占比 {generic_ratio:.1%}，"
                f"超过阈值 {max_generic_headword_ratio:.1%}。"
            )
            strict_drop_sample = next(
                (
                    label["sample_no"]
                    for label in quality_labels
                    if label["content_role"] == "body"
                    and label["generic_or_nonknowledge_headword_count"] > 0
                ),
                quality_labels[0]["sample_no"],
            )
            strict_problem_type = "非知识点词头"

        if strict_drop_reason:
            if result["decision"] != "DROP":
                result["decision"] = "DROP"
                result["decision_adjusted_by_strict_quality_gate"] = True
                result["summary"] = "[严格正文质量门槛调整为DROP] " + result["summary"]
            if not any(
                problem.get("sample_no") == strict_drop_sample
                and problem.get("problem_type") == strict_problem_type
                for problem in result["problems"]
            ):
                result["problems"].append({
                    "sample_no": strict_drop_sample,
                    "problem_type": strict_problem_type,
                    "anchor": "严格正文质量门槛",
                    "reason": strict_drop_reason,
                    "severity": "critical",
                    "rule_cleanable": False,
                })
        elif result["decision"] == "PASS" and (
            unclear_samples or candidate_count < min_candidate_headwords
        ):
            result["decision"] = "REVIEW"
            result["decision_adjusted_by_strict_quality_gate"] = True
            if unclear_samples:
                reason = "正文质量或正文角色存在无法确认的片段。"
                sample_no = unclear_samples[0]
            else:
                reason = (
                    f"正文候选词头仅 {candidate_count} 个，"
                    f"少于最低证据量 {min_candidate_headwords} 个。"
                )
                sample_no = quality_labels[0]["sample_no"]
            result["summary"] = "[严格正文质量门槛调整为REVIEW] " + result["summary"]
            result["problems"].append({
                "sample_no": sample_no,
                "problem_type": "其他",
                "anchor": "严格正文质量待确认",
                "reason": reason,
                "severity": "major",
                "rule_cleanable": False,
            })

    result["knowledge_value_mode"] = bool(knowledge_value_mode)
    result["decision_before_knowledge_value_gate"] = result["decision"]
    result["decision_adjusted_by_knowledge_value_gate"] = False
    result["discipline_knowledge_headword_count"] = 0
    result["nonknowledge_headword_count"] = 0
    result["knowledge_value_evidence_count"] = 0
    result["nonknowledge_headword_ratio"] = 0.0
    result["knowledge_value_tier"] = "disabled"
    result["knowledge_other_language_samples"] = 0
    result["unusable_entry_samples"] = 0
    result["unclear_entry_samples"] = 0
    result["rule_cleanable_entry_samples"] = 0
    result["accepted_headword_types"] = list(
        accepted_headword_types or DEFAULT_ACCEPTED_HEADWORD_TYPES
    )
    if knowledge_value_mode:
        result = apply_qualitative_gate(result, qualitative_flags)
    if result["decision"] != "PASS" and not result["problems"]:
        if non_zh_en_language:
            result["problems"] = [{
                "sample_no": labels[0]["sample_no"],
                "problem_type": "非中英文",
                "anchor": ",".join(result["dominant_languages"])[:20],
                "reason": "正文主语言不是中文或英文，按语言硬门槛判为DROP。",
                "severity": "critical",
                "rule_cleanable": False,
            }]
        else:
            evidence_structure = "long_article" if long_count else "unclear"
            evidence_sample = next(
                (
                    label["sample_no"]
                    for label in labels
                    if label["structure"] == evidence_structure
                ),
                labels[0]["sample_no"],
            )
            result["problems"] = [{
                "sample_no": evidence_sample,
                "problem_type": "非辞海结构",
                "anchor": evidence_structure,
                "reason": (
                    f"逐段结构计数为 compact_entries={compact_count}, "
                    f"long_entry={long_entry_count}, "
                    f"long_article={long_count}, 有效片段={substantive_count}，"
                    "未通过辞海结构硬门槛。"
                ),
                "severity": "major",
                "rule_cleanable": False,
            }]
    if result["decision"] != "PASS" and not result["problems"]:
        raise ValueError("REVIEW/DROP must include evidence")
    return result


def apply_document_hard_gates(result, item):
    result["decision_before_traditional_gate"] = result["decision"]
    result["decision_adjusted_by_traditional_gate"] = False
    if item.get("traditional_chinese_body"):
        if result["decision"] != "DROP":
            result["decision"] = "DROP"
            result["decision_adjusted_by_gate"] = True
            result["decision_adjusted_by_traditional_gate"] = True
            result["summary"] = "[繁体硬门槛调整为DROP] " + result["summary"]
        result["rule_cleanable"] = False
        if not any(problem.get("problem_type") == "繁体中文" for problem in result["problems"]):
            result["problems"].append({
                "sample_no": 1,
                "problem_type": "繁体中文",
                "anchor": "繁体正文主体",
                "reason": (
                    f"检测到繁体差异字符 {item.get('traditional_variant_chars', 0)} 个，"
                    f"占中文字符 {float(item.get('traditional_ratio', 0)):.1%}，"
                    "按语言硬门槛判为DROP。"
                ),
                "severity": "critical",
                "rule_cleanable": False,
            })

    result["decision_before_heading_language_gate"] = result["decision"]
    result["decision_adjusted_by_heading_language_gate"] = False
    if item.get("non_zh_en_heading_signal"):
        if result["decision"] != "DROP":
            result["decision"] = "DROP"
            result["decision_adjusted_by_gate"] = True
            result["decision_adjusted_by_heading_language_gate"] = True
            result["summary"] = "[多语词头硬门槛调整为DROP] " + result["summary"]
        result["rule_cleanable"] = False
        if not any(
            problem.get("anchor") == "全文多语词头信号"
            for problem in result["problems"]
        ):
            result["problems"].append({
                "sample_no": 1,
                "problem_type": "非中英文",
                "anchor": "全文多语词头信号",
                "reason": (
                    f"全文检测到 {item.get('non_zh_en_heading_count', 0)} 个反复出现的"
                    "非中英文或多语种词头标题，按语言硬门槛判为DROP。"
                ),
                "severity": "critical",
                "rule_cleanable": False,
            })
    return result


def fit_request_context(body, payload, api_url, headers):
    tokenize_url = api_url.rsplit("/v1/", 1)[0] + "/tokenize"
    model_limit = 32768
    for round_no in range(9):
        body["messages"][-1]["content"] = json.dumps(payload, ensure_ascii=False)
        token_request = {"model": body["model"], "messages": body["messages"],
                         "add_generation_prompt": True,
                         "chat_template_kwargs": {"enable_thinking": False}}
        verified = False
        try:
            req = urllib.request.Request(tokenize_url, data=json.dumps(token_request).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                token_info = json.load(response)
            count = int(token_info["count"])
            model_limit = int(token_info.get("max_model_len") or model_limit)
            verified = True
        except (OSError, ValueError, KeyError):
            text = "\n".join(message["content"] for message in body["messages"])
            cjk = len(re.findall(r"[\u3400-\u9fff]", text))
            count = math.ceil(cjk * 2 + (len(text) - cjk) / 3) + 256
        if count + body["max_tokens"] + 512 <= model_limit:
            return {"input_tokens": count, "tokenizer_verified": verified,
                    "model_limit": model_limit, "trim_rounds": round_no}
        for sample in payload["samples"]:
            for field in ("context_before", "context_after", "preceding_entry"):
                value = sample.get(field, "")
                size = int(len(value) * 0.45)
                sample[field] = value[-size:] if field == "context_before" and size else value[:size]
            if round_no >= 2 and len(sample["text"]) > 350:
                size = max(350, int(len(sample["text"]) * 0.8))
                sample["context_after"] = sample["text"][size:size + 150] + sample.get("context_after", "")[:100]
                sample["text"] = sample["text"][:size]
                sample["char_end"] = sample.get("char_start", 0) + size
    raise ValueError("input cannot fit model context without losing minimum sample content")


def request_one(
    api_url,
    model,
    item,
    timeout,
    max_tokens,
    retries,
    strict_text_quality=False,
    max_generic_headword_ratio=0.15,
    min_candidate_headwords=20,
    knowledge_value_mode=False,
    max_nonknowledge_headword_ratio=0.15,
    drop_nonknowledge_headword_ratio=0.50,
    min_headword_evidence=10,
):
    payload = {
        "short_id": item["short_id"],
        "subject": item["subject"],
        "input_title": item["title"],
        "file_name": item["file_name"],
        "book_type_hint": item.get("book_type_hint", ""),
        "extraction_focus": item.get("extraction_focus", ""),
        "sample_count": len(item["samples"]),
        "samples": [
            {**sample, "text": compact_repetition_for_prompt(sample["text"])}
            for sample in item["samples"]
        ],
    }
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": build_system_prompt(
                    strict_text_quality=strict_text_quality,
                    max_generic_headword_ratio=max_generic_headword_ratio,
                    min_candidate_headwords=min_candidate_headwords,
                    knowledge_value_mode=knowledge_value_mode,
                    max_nonknowledge_headword_ratio=max_nonknowledge_headword_ratio,
                    drop_nonknowledge_headword_ratio=drop_nonknowledge_headword_ratio,
                    min_headword_evidence=min_headword_evidence,
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    api_key = (
        os.environ.get("KNOWLEDGE_LABELING_API_KEY") or os.environ.get("ZJLAB_API_KEY") or ""
    ).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    context_budget = fit_request_context(body, payload, api_url, headers) if knowledge_value_mode else None
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error = None
    last_content = None
    for attempt in range(1, retries + 2):
        started = time.time()
        try:
            request = urllib.request.Request(
                api_url,
                data=encoded,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                obj = json.loads(response.read().decode("utf-8"))
            content = obj["choices"][0]["message"]["content"]
            last_content = content
            result = parse_json_content(
                content,
                expected_sample_count=len(item["samples"]),
                strict_text_quality=strict_text_quality,
                max_generic_headword_ratio=max_generic_headword_ratio,
                min_candidate_headwords=min_candidate_headwords,
                knowledge_value_mode=knowledge_value_mode,
                max_nonknowledge_headword_ratio=max_nonknowledge_headword_ratio,
                drop_nonknowledge_headword_ratio=drop_nonknowledge_headword_ratio,
                min_headword_evidence=min_headword_evidence,
                accepted_headword_types=item.get("accepted_headword_types"),
                samples=payload["samples"] if knowledge_value_mode else None,
            )
            result = apply_document_hard_gates(result, item)
            return {
                "ok": True,
                "short_id": item["short_id"],
                "subject": item["subject"],
                "source_path": item["source_path"],
                "relative_path": item["relative_path"],
                "file_name": item["file_name"],
                "input_title": item["title"],
                "result": result,
                "traditional_chinese_body": item.get("traditional_chinese_body", False),
                "cjk_chars": item.get("cjk_chars", 0),
                "traditional_variant_chars": item.get("traditional_variant_chars", 0),
                "traditional_ratio": item.get("traditional_ratio", 0.0),
                "non_zh_en_heading_signal": item.get("non_zh_en_heading_signal", False),
                "non_zh_en_heading_count": item.get("non_zh_en_heading_count", 0),
                "non_zh_en_heading_chars": item.get("non_zh_en_heading_chars", 0),
                "usage": obj.get("usage"),
                "latency_seconds": round(time.time() - started, 3),
                "attempt": attempt,
                "raw_content": content,
                "input_samples": payload["samples"],
                "context_budget": context_budget,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, urllib.error.HTTPError):
                last_error += ": " + exc.read().decode("utf-8", errors="replace")[:500]
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 5))
    return {
        "ok": False,
        "short_id": item["short_id"],
        "subject": item["subject"],
        "source_path": item["source_path"],
        "relative_path": item["relative_path"],
        "file_name": item["file_name"],
        "input_title": item["title"],
        "error": last_error,
        "raw_content": last_content,
        "attempt": retries + 1,
    }


def request_pass_challenge(api_url, model, item, timeout, max_tokens, retries):
    samples = [
        {**sample, "text": compact_repetition_for_prompt(sample.get("text", ""))}
        for sample in item["challenge_samples"]
    ]
    payload = {
        "short_id": item["short_id"],
        "subject": item["subject"],
        "input_title": item["title"],
        "file_name": item["file_name"],
        "sample_count": len(samples),
        "samples": samples,
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PASS_CHALLENGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    api_key = (
        os.environ.get("KNOWLEDGE_LABELING_API_KEY") or os.environ.get("ZJLAB_API_KEY") or ""
    ).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    context_budget = fit_request_context(body, payload, api_url, headers)
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error = None
    last_content = None
    for attempt in range(1, retries + 2):
        started = time.time()
        try:
            request = urllib.request.Request(
                api_url, data=encoded, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                obj = json.loads(response.read().decode("utf-8"))
            content = obj["choices"][0]["message"]["content"]
            last_content = content
            challenge = parse_pass_challenge_content(content, payload["samples"])
            return {
                "ok": True,
                "short_id": item["short_id"],
                "subject": item["subject"],
                "source_path": item["source_path"],
                "relative_path": item["relative_path"],
                "file_name": item["file_name"],
                "input_title": item["title"],
                "trigger_reasons": item["challenge_reasons"],
                "challenge": challenge,
                "usage": obj.get("usage"),
                "latency_seconds": round(time.time() - started, 3),
                "attempt": attempt,
                "raw_content": content,
                "input_samples": payload["samples"],
                "context_budget": context_budget,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, urllib.error.HTTPError):
                last_error += ": " + exc.read().decode("utf-8", errors="replace")[:500]
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 5))
    return {
        "ok": False,
        "short_id": item["short_id"],
        "subject": item["subject"],
        "source_path": item["source_path"],
        "relative_path": item["relative_path"],
        "file_name": item["file_name"],
        "input_title": item["title"],
        "trigger_reasons": item["challenge_reasons"],
        "error": last_error,
        "raw_content": last_content,
        "attempt": retries + 1,
    }


def load_progress(path):
    completed = {}
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("ok"):
                completed[item["source_path"]] = item
    return completed


def selected_specs(root, names, config_path=None):
    specs = subject_specs(root, config_path)
    if not names or names == ["all"]:
        return specs
    wanted = set(names)
    chosen = [spec for spec in specs if spec["name"] in wanted]
    missing = wanted - {spec["name"] for spec in chosen}
    if missing:
        raise ValueError(f"未知学科: {sorted(missing)}")
    return chosen


def run_subject(
    output, spec, api_url, model, workers, timeout, retries, max_tokens, limit,
    pending_limit=None, strict_text_quality=False,
    max_generic_headword_ratio=0.15, min_candidate_headwords=20,
    knowledge_value_mode=False, max_nonknowledge_headword_ratio=0.15,
    drop_nonknowledge_headword_ratio=0.50, min_headword_evidence=10,
):
    manifest_path = output / "输入清单" / f"{spec['name']}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest["records"]
    if limit is not None:
        items = items[:limit]
    raw_dir = output / spec["name"] / "模型原始结果"
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_config = {
        "api_url": api_url,
        "model": model,
        "workers": workers,
        "timeout": timeout,
        "retries": retries,
        "max_tokens": max_tokens,
        "strict_text_quality": bool(strict_text_quality),
        "max_generic_headword_ratio": max_generic_headword_ratio,
        "min_candidate_headwords": min_candidate_headwords,
        "knowledge_value_mode": bool(knowledge_value_mode),
        "knowledge_value_policy": KNOWLEDGE_VALUE_POLICY_VERSION if knowledge_value_mode else "disabled",
        "max_nonknowledge_headword_ratio": max_nonknowledge_headword_ratio,
        "drop_nonknowledge_headword_ratio": drop_nonknowledge_headword_ratio,
        "min_headword_evidence": min_headword_evidence,
        "system_prompt": build_system_prompt(
            strict_text_quality=strict_text_quality,
            max_generic_headword_ratio=max_generic_headword_ratio,
            min_candidate_headwords=min_candidate_headwords,
            knowledge_value_mode=knowledge_value_mode,
            max_nonknowledge_headword_ratio=max_nonknowledge_headword_ratio,
            drop_nonknowledge_headword_ratio=drop_nonknowledge_headword_ratio,
            min_headword_evidence=min_headword_evidence,
        ),
    }
    progress_path = raw_dir / "progress.jsonl"
    config_path = raw_dir / "run_config.json"
    if config_path.exists() and progress_path.exists() and progress_path.stat().st_size:
        previous = json.loads(config_path.read_text(encoding="utf-8"))
        for field in ("api_url", "model", "system_prompt", "knowledge_value_policy"):
            if previous.get(field) != run_config.get(field):
                raise ValueError(f"审核策略已变化，不能复用旧 progress.jsonl: {field}；请使用新输出目录")
    write_json(config_path, run_config)
    api_raw_path = raw_dir / "api_raw.jsonl"
    completed = load_progress(progress_path)
    pending = [item for item in items if item["source_path"] not in completed]
    if pending_limit is not None:
        pending = pending[:pending_limit]
    counters = Counter(ok=0, failed=0)
    lock = threading.Lock()

    def run(item):
        return request_one(
            api_url,
            model,
            item,
            timeout,
            max_tokens,
            retries,
            strict_text_quality=strict_text_quality,
            max_generic_headword_ratio=max_generic_headword_ratio,
            min_candidate_headwords=min_candidate_headwords,
            knowledge_value_mode=knowledge_value_mode,
            max_nonknowledge_headword_ratio=max_nonknowledge_headword_ratio,
            drop_nonknowledge_headword_ratio=drop_nonknowledge_headword_ratio,
            min_headword_evidence=min_headword_evidence,
        )

    with progress_path.open("a", encoding="utf-8") as progress, api_raw_path.open("a", encoding="utf-8") as api_raw:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run, item): item for item in pending}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                with lock:
                    key = "ok" if result.get("ok") else "failed"
                    counters[key] += 1
                    progress.write(json.dumps(result, ensure_ascii=False) + "\n")
                    progress.flush()
                    api_raw.write(json.dumps({
                        key: result.get(key)
                        for key in ("ok", "short_id", "source_path", "raw_content", "error", "usage", "latency_seconds", "attempt")
                        if key in result
                    }, ensure_ascii=False) + "\n")
                    api_raw.flush()
                    done = counters["ok"] + counters["failed"]
                    if done % 10 == 0 or done == len(pending):
                        print(json.dumps({
                            "subject": spec["name"], "done": done, "pending_total": len(pending),
                            "ok": counters["ok"], "failed": counters["failed"]
                        }, ensure_ascii=False), flush=True)
    merged = load_progress(progress_path)
    ordered = [merged[item["source_path"]] for item in manifest["records"] if item["source_path"] in merged]
    write_json(raw_dir / "results.json", ordered)
    return {"subject": spec["name"], "requested": len(items), "pending": len(pending), "completed_total": len(ordered)}


def run_pass_challenge_subject(
    output, spec, api_url, model, workers, timeout, retries, max_tokens,
    pending_limit=None, challenge_sample_count=8,
):
    manifest_path = output / "输入清单" / f"{spec['name']}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest["records"]
    primary_path = output / spec["name"] / "模型原始结果" / "progress.jsonl"
    primary = load_progress(primary_path)
    items = []
    for record in records:
        primary_item = primary.get(record["source_path"])
        if not primary_item:
            continue
        reasons = pass_challenge_reasons(primary_item["result"], record.get("title", ""))
        if not reasons:
            continue
        item = copy.deepcopy(record)
        item["challenge_reasons"] = reasons
        item["challenge_samples"] = select_pass_challenge_samples(
            item, primary_item["result"], max_samples=challenge_sample_count
        )
        items.append(item)

    challenge_dir = output / spec["name"] / "模型反证复核"
    challenge_dir.mkdir(parents=True, exist_ok=True)
    progress_path = challenge_dir / "challenge_progress.jsonl"
    api_raw_path = challenge_dir / "challenge_api_raw.jsonl"
    config_path = challenge_dir / "run_config.json"
    run_config = {
        "api_url": api_url,
        "model": model,
        "workers": workers,
        "timeout": timeout,
        "retries": retries,
        "max_tokens": max_tokens,
        "challenge_sample_count": challenge_sample_count,
        "policy_version": PASS_CHALLENGE_POLICY_VERSION,
        "system_prompt": PASS_CHALLENGE_SYSTEM_PROMPT,
    }
    if config_path.exists() and progress_path.exists() and progress_path.stat().st_size:
        previous = json.loads(config_path.read_text(encoding="utf-8"))
        for field in ("api_url", "model", "challenge_sample_count", "policy_version", "system_prompt"):
            if previous.get(field) != run_config.get(field):
                raise ValueError(
                    f"PASS反证策略已变化，不能复用旧 challenge_progress.jsonl: {field}；"
                    "请使用新输出目录"
                )
    write_json(config_path, run_config)
    completed = load_progress(progress_path)
    pending = [item for item in items if item["source_path"] not in completed]
    if pending_limit is not None:
        pending = pending[:pending_limit]
    counters = Counter(ok=0, failed=0)
    lock = threading.Lock()

    def run(item):
        return request_pass_challenge(
            api_url, model, item, timeout, max_tokens, retries
        )

    with progress_path.open("a", encoding="utf-8") as progress, api_raw_path.open(
        "a", encoding="utf-8"
    ) as api_raw:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run, item): item for item in pending}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                with lock:
                    key = "ok" if result.get("ok") else "failed"
                    counters[key] += 1
                    progress.write(json.dumps(result, ensure_ascii=False) + "\n")
                    progress.flush()
                    api_raw.write(json.dumps({
                        key: result.get(key)
                        for key in (
                            "ok", "short_id", "source_path", "raw_content", "error",
                            "usage", "latency_seconds", "attempt", "trigger_reasons"
                        )
                        if key in result
                    }, ensure_ascii=False) + "\n")
                    api_raw.flush()
                    done = counters["ok"] + counters["failed"]
                    if done % 10 == 0 or done == len(pending):
                        print(json.dumps({
                            "subject": spec["name"], "stage": "pass_challenge",
                            "done": done, "pending_total": len(pending),
                            "ok": counters["ok"], "failed": counters["failed"],
                        }, ensure_ascii=False), flush=True)
    merged = load_progress(progress_path)
    ordered = [merged[item["source_path"]] for item in items if item["source_path"] in merged]
    write_json(challenge_dir / "results.json", ordered)
    summary = {
        "subject": spec["name"],
        "primary_completed": len(primary),
        "triggered": len(items),
        "pending": len(pending),
        "completed_total": len(ordered),
        "failed_this_run": counters["failed"],
    }
    write_json(challenge_dir / "summary.json", summary)
    return summary


def flattened_result(item):
    result = item["result"]
    problems = result.get("problems", [])
    targets = result.get("manual_review_targets", [])
    structure_counts = result.get("sample_structure_counts", {})
    usage = item.get("usage") or {}
    return {
        "short_id": item["short_id"],
        "subject": item["subject"],
        "decision": result["decision"],
        "input_title": item.get("input_title", ""),
        "title_guess": result.get("title_guess", ""),
        "file_name": item["file_name"],
        "summary": result.get("summary", ""),
        "document_type": result.get("document_type", ""),
        "dominant_languages": ",".join(result.get("dominant_languages", [])),
        "substantial_non_zh_en": result.get("substantial_non_zh_en"),
        "publication_year": result.get("publication_year"),
        "age_risk": result.get("age_risk", ""),
        "semantic_quality": result.get("semantic_quality", ""),
        "ocr_quality": result.get("ocr_quality", ""),
        "knowledge_density": result.get("knowledge_density", ""),
        "knowledge_extraction_suitability": result.get("knowledge_extraction_suitability", ""),
        "dictionary_structure": result.get("dictionary_structure", ""),
        "subject_relevance": result.get("subject_relevance", ""),
        "entry_definition_alignment": result.get("entry_definition_alignment", ""),
        "continuous_prose_dominant": result.get("continuous_prose_dominant"),
        "sample_structure_labels": json.dumps(
            result.get("sample_structure_labels", []), ensure_ascii=False
        ),
        "compact_entry_samples": structure_counts.get("compact_entries", 0),
        "long_entry_samples": structure_counts.get("long_entry", 0),
        "long_article_samples": structure_counts.get("long_article", 0),
        "frontmatter_index_samples": structure_counts.get("frontmatter_index", 0),
        "unclear_samples": structure_counts.get("unclear", 0),
        "substantive_sample_count": result.get("substantive_sample_count", 0),
        "decision_before_gate": result.get("decision_before_gate", result.get("decision", "")),
        "decision_adjusted_by_gate": result.get("decision_adjusted_by_gate", False),
        "decision_before_traditional_gate": result.get(
            "decision_before_traditional_gate", result.get("decision", "")
        ),
        "decision_adjusted_by_traditional_gate": result.get(
            "decision_adjusted_by_traditional_gate", False
        ),
        "decision_before_heading_language_gate": result.get(
            "decision_before_heading_language_gate", result.get("decision", "")
        ),
        "decision_adjusted_by_heading_language_gate": result.get(
            "decision_adjusted_by_heading_language_gate", False
        ),
        "strict_text_quality": result.get("strict_text_quality", False),
        "decision_before_strict_quality_gate": result.get(
            "decision_before_strict_quality_gate", result.get("decision", "")
        ),
        "decision_adjusted_by_strict_quality_gate": result.get(
            "decision_adjusted_by_strict_quality_gate", False
        ),
        "strict_body_error_samples": result.get("strict_body_error_samples", 0),
        "strict_unclear_body_samples": result.get("strict_unclear_body_samples", 0),
        "strict_body_has_other_language": result.get(
            "strict_body_has_other_language", False
        ),
        "candidate_headword_count": result.get("candidate_headword_count", 0),
        "generic_or_nonknowledge_headword_count": result.get(
            "generic_or_nonknowledge_headword_count", 0
        ),
        "generic_headword_ratio": result.get("generic_headword_ratio", 0.0),
        "sample_quality_labels": json.dumps(
            result.get("sample_quality_labels", []), ensure_ascii=False
        ),
        "knowledge_value_mode": result.get("knowledge_value_mode", False),
        "decision_before_knowledge_value_gate": result.get(
            "decision_before_knowledge_value_gate", result.get("decision", "")
        ),
        "decision_adjusted_by_knowledge_value_gate": result.get(
            "decision_adjusted_by_knowledge_value_gate", False
        ),
        "knowledge_value_tier": result.get("knowledge_value_tier", "disabled"),
        "knowledge_value_policy": result.get("knowledge_value_policy", "legacy"),
        "knowledge_value_assessment": json.dumps(result.get("knowledge_value_assessment", {}), ensure_ascii=False),
        "unverified_knowledge_examples": json.dumps(result.get("unverified_knowledge_examples", []), ensure_ascii=False),
        "accepted_headword_types": json.dumps(
            result.get("accepted_headword_types", []), ensure_ascii=False
        ),
        "discipline_knowledge_headword_count": result.get(
            "discipline_knowledge_headword_count", 0
        ),
        "nonknowledge_headword_count": result.get("nonknowledge_headword_count", 0),
        "knowledge_value_evidence_count": result.get("knowledge_value_evidence_count", 0),
        "nonknowledge_headword_ratio": result.get("nonknowledge_headword_ratio", 0.0),
        "sample_headword_type_counts": json.dumps(
            result.get("sample_headword_type_counts", []), ensure_ascii=False
        ),
        "knowledge_other_language_samples": result.get(
            "knowledge_other_language_samples", 0
        ),
        "unusable_entry_samples": result.get("unusable_entry_samples", 0),
        "unclear_entry_samples": result.get("unclear_entry_samples", 0),
        "rule_cleanable_entry_samples": result.get("rule_cleanable_entry_samples", 0),
        "sample_knowledge_value_flags": json.dumps(
            result.get("sample_knowledge_value_flags", []), ensure_ascii=False
        ),
        "pass_challenge_policy": result.get("pass_challenge_policy", "disabled"),
        "pass_challenge_triggered": result.get("pass_challenge_triggered", False),
        "pass_challenge_reasons": ",".join(result.get("pass_challenge_reasons", [])),
        "decision_before_pass_challenge": result.get(
            "decision_before_pass_challenge", result.get("decision", "")
        ),
        "decision_adjusted_by_pass_challenge": result.get(
            "decision_adjusted_by_pass_challenge", False
        ),
        "pass_challenge_recommendation": (
            result.get("pass_challenge_result") or {}
        ).get("recommendation", ""),
        "pass_challenge_dominant_payload": (
            result.get("pass_challenge_result") or {}
        ).get("dominant_payload", ""),
        "pass_challenge_strong_evidence_count": result.get(
            "pass_challenge_strong_evidence_count", 0
        ),
        "pass_challenge_strong_evidence_sample_count": result.get(
            "pass_challenge_strong_evidence_sample_count", 0
        ),
        "pass_challenge_result": json.dumps(
            result.get("pass_challenge_result", {}), ensure_ascii=False
        ),
        "traditional_chinese_body": item.get("traditional_chinese_body", False),
        "cjk_chars": item.get("cjk_chars", 0),
        "traditional_variant_chars": item.get("traditional_variant_chars", 0),
        "traditional_ratio": item.get("traditional_ratio", 0.0),
        "non_zh_en_heading_signal": item.get("non_zh_en_heading_signal", False),
        "non_zh_en_heading_count": item.get("non_zh_en_heading_count", 0),
        "non_zh_en_heading_chars": item.get("non_zh_en_heading_chars", 0),
        "primary_extraction_obstacles": json.dumps(
            result.get("primary_extraction_obstacles", []), ensure_ascii=False
        ),
        "rule_cleanable": result.get("rule_cleanable"),
        "problem_count": len(problems),
        "problem_evidence": json.dumps(problems, ensure_ascii=False),
        "manual_review_targets": json.dumps(targets, ensure_ascii=False),
        "confidence": result.get("confidence"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "latency_seconds": item.get("latency_seconds"),
        "relative_path": item["relative_path"],
        "source_path": item["source_path"],
        "classified_copy": "",
    }


CSV_FIELDS = [
    "short_id", "subject", "decision", "input_title", "title_guess", "file_name", "summary",
    "document_type", "dominant_languages", "substantial_non_zh_en", "publication_year",
    "age_risk", "semantic_quality", "ocr_quality", "knowledge_density",
    "knowledge_extraction_suitability", "dictionary_structure", "subject_relevance",
    "entry_definition_alignment", "continuous_prose_dominant",
    "sample_structure_labels", "compact_entry_samples", "long_entry_samples", "long_article_samples",
    "frontmatter_index_samples", "unclear_samples", "substantive_sample_count",
    "decision_before_gate", "decision_adjusted_by_gate",
    "decision_before_traditional_gate", "decision_adjusted_by_traditional_gate",
    "decision_before_heading_language_gate", "decision_adjusted_by_heading_language_gate",
    "strict_text_quality", "decision_before_strict_quality_gate",
    "decision_adjusted_by_strict_quality_gate", "strict_body_error_samples",
    "strict_unclear_body_samples", "strict_body_has_other_language",
    "candidate_headword_count", "generic_or_nonknowledge_headword_count",
    "generic_headword_ratio", "sample_quality_labels",
    "knowledge_value_mode", "decision_before_knowledge_value_gate",
    "decision_adjusted_by_knowledge_value_gate", "knowledge_value_tier",
    "knowledge_value_policy", "knowledge_value_assessment",
    "unverified_knowledge_examples",
    "accepted_headword_types", "discipline_knowledge_headword_count",
    "nonknowledge_headword_count", "knowledge_value_evidence_count",
    "nonknowledge_headword_ratio", "sample_headword_type_counts",
    "knowledge_other_language_samples", "unusable_entry_samples",
    "unclear_entry_samples", "rule_cleanable_entry_samples",
    "sample_knowledge_value_flags",
    "pass_challenge_policy", "pass_challenge_triggered", "pass_challenge_reasons",
    "decision_before_pass_challenge", "decision_adjusted_by_pass_challenge",
    "pass_challenge_recommendation", "pass_challenge_dominant_payload",
    "pass_challenge_strong_evidence_count", "pass_challenge_strong_evidence_sample_count",
    "pass_challenge_result",
    "traditional_chinese_body", "cjk_chars", "traditional_variant_chars",
    "traditional_ratio", "non_zh_en_heading_signal", "non_zh_en_heading_count",
    "non_zh_en_heading_chars",
    "primary_extraction_obstacles",
    "rule_cleanable", "problem_count",
    "problem_evidence", "manual_review_targets", "confidence", "prompt_tokens",
    "completion_tokens", "latency_seconds", "relative_path", "source_path", "classified_copy",
]


def classified_name(row):
    stem = re.sub(r'[<>:"/\\|?*]+', "_", Path(row["file_name"]).stem).strip(" .")
    digest = hashlib.sha1(row["source_path"].encode("utf-8")).hexdigest()[:8]
    stem = stem[:80] or "unnamed"
    return f"{row['short_id']}__{stem}__{digest}.md"


def find_node_runtime():
    candidates = []
    path_node = shutil.which("node")
    if path_node:
        candidates.append(Path(path_node))
    candidates.append(
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" /
        "dependencies" / "node" / "bin" / "node.exe"
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise RuntimeError("未找到 Node.js，无法生成重点问题工作簿")


def export_priority_workbook(output, spec, render_dir=None, progress_path=None):
    manifest_path = output / "输入清单" / f"{spec['name']}.json"
    progress_path = progress_path or (
        output / spec["name"] / "模型原始结果" / "progress.jsonl"
    )
    if not manifest_path.exists() or not progress_path.exists():
        raise FileNotFoundError("生成重点问题工作簿所需的输入清单或模型结果不存在")
    if not PRIORITY_WORKBOOK_EXPORTER.exists():
        raise FileNotFoundError(f"工作簿导出器不存在: {PRIORITY_WORKBOOK_EXPORTER}")

    date_suffix = datetime.now().strftime("%Y%m%d")
    output_path = (
        output / spec["name"] /
        f"{spec['name']}_重点问题_MD段落与问题说明_{date_suffix}.xlsx"
    )
    node = find_node_runtime()
    env = dict(os.environ)
    bundled_node_modules = node.parent.parent / "node_modules"
    if bundled_node_modules.exists():
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = str(bundled_node_modules) + (os.pathsep + existing if existing else "")
    command = [
        str(node), str(PRIORITY_WORKBOOK_EXPORTER), str(manifest_path),
        str(progress_path), str(output_path), spec["name"],
    ]
    if render_dir is not None:
        command.append(str(render_dir))
    completed = subprocess.run(
        command,
        cwd=PRIORITY_WORKBOOK_EXPORTER.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"重点问题工作簿生成失败: {detail}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("重点问题工作簿导出器未返回结果")
    return json.loads(lines[-1])


def materialize_subject(
    output, spec, generate_priority_workbook=True, symlink_classified=False,
    pass_challenge=False,
):
    raw_path = output / spec["name"] / "模型原始结果" / "progress.jsonl"
    completed = load_progress(raw_path)
    workbook_progress_path = raw_path
    challenge_summary = None
    if pass_challenge:
        manifest_path = output / "输入清单" / f"{spec['name']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_records = {item["source_path"]: item for item in manifest["records"]}
        challenge_dir = output / spec["name"] / "模型反证复核"
        challenge_completed = load_progress(challenge_dir / "challenge_progress.jsonl")
        completed = overlay_pass_challenges(completed, challenge_completed, manifest_records)
        workbook_progress_path = challenge_dir / "merged_progress.jsonl"
        with workbook_progress_path.open("w", encoding="utf-8") as handle:
            for source_path in manifest_records:
                if source_path in completed:
                    handle.write(json.dumps(completed[source_path], ensure_ascii=False) + "\n")
        challenge_summary = {
            "enabled": True,
            "triggered": sum(
                item["result"].get("pass_challenge_triggered", False)
                for item in completed.values()
            ),
            "demoted_to_review": sum(
                item["result"].get("decision_adjusted_by_pass_challenge", False)
                for item in completed.values()
            ),
        }
    rows = [flattened_result(item) for item in completed.values()]
    rows.sort(key=lambda row: row["short_id"])
    subject_dir = output / spec["name"]
    tier_root = subject_dir / "三级分类"
    previous_results = subject_dir / "审核结果.csv"
    if previous_results.exists():
        with previous_results.open("r", encoding="utf-8-sig", newline="") as handle:
            for previous in csv.DictReader(handle):
                value = previous.get("classified_copy", "").strip()
                if not value:
                    continue
                previous_copy = Path(value)
                try:
                    resolved_copy = previous_copy.resolve()
                    resolved_root = tier_root.resolve()
                except OSError:
                    continue
                if resolved_copy.is_file() and resolved_root in resolved_copy.parents:
                    resolved_copy.unlink()
    for decision in ("PASS", "REVIEW", "DROP"):
        (tier_root / decision).mkdir(parents=True, exist_ok=True)
    copy_errors = []
    for row in rows:
        destination = tier_root / row["decision"] / classified_name(row)
        try:
            if symlink_classified:
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                destination.symlink_to(Path(row["source_path"]).resolve())
            else:
                shutil.copy2(row["source_path"], destination)
            row["classified_copy"] = str(destination.resolve())
        except Exception as exc:
            copy_errors.append({"short_id": row["short_id"], "error": str(exc)})
    write_csv(subject_dir / "审核结果.csv", rows, CSV_FIELDS)
    for decision in ("PASS", "REVIEW", "DROP"):
        write_csv(tier_root / decision / "_本级审核结果.csv", [row for row in rows if row["decision"] == decision], CSV_FIELDS)
    counts = Counter(row["decision"] for row in rows)
    summary = {
        "subject": spec["name"],
        "completed": len(rows),
        "counts": {decision: counts.get(decision, 0) for decision in ("PASS", "REVIEW", "DROP")},
        "copy_errors": copy_errors,
        "pass_challenge": challenge_summary or {"enabled": False},
    }
    if generate_priority_workbook:
        summary["priority_workbook"] = export_priority_workbook(
            output, spec, progress_path=workbook_progress_path
        )
    write_json(subject_dir / "审核摘要.json", summary)
    lines = [
        f"# {spec['name']}知识型书籍 LLM 审核摘要",
        "",
        f"- 已完成：{len(rows)} 本",
        f"- PASS：{counts.get('PASS', 0)} 本",
        f"- REVIEW：{counts.get('REVIEW', 0)} 本",
        f"- DROP：{counts.get('DROP', 0)} 本",
        f"- 复制失败：{len(copy_errors)} 本",
        "",
        "详细原因见 `审核结果.csv`；各级目录内另有 `_本级审核结果.csv`。",
    ]
    (subject_dir / "审核摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows, summary


def build_prompt_snapshot(run_entries):
    lines = ["# 本次 LLM 审核配置与提示词", ""]
    for entry in run_entries:
        strict_label = "启用" if entry.get("strict_text_quality") else "未启用"
        knowledge_value_label = "启用" if entry.get("knowledge_value_mode") else "未启用"
        lines.extend([
            f"## {entry.get('subject', '未命名学科')}",
            "",
            f"- API：`{entry.get('api_url', '')}`",
            f"- 模型：`{entry.get('model', '')}`",
            f"- 抽样：{entry.get('sampling_rule', '')}",
            f"- 严格正文质量：{strict_label}",
            f"- 泛化或非知识点词头上限：{float(entry.get('max_generic_headword_ratio', 0.15)):.1%}",
            f"- 最少候选词头证据量：{entry.get('min_candidate_headwords', 20)}",
            f"- 学科知识价值模式：{knowledge_value_label}",
            f"- 知识价值策略：{entry.get('knowledge_value_policy', 'legacy')}",
            "- 定性模式不按词头比例或词头个数分级。",
            "",
        ])
    prompts = []
    for entry in run_entries:
        prompt = entry.get("system_prompt", "")
        if prompt and prompt not in prompts:
            prompts.append(prompt)
    for index, prompt in enumerate(prompts, 1):
        heading = "## System Prompt" if len(prompts) == 1 else f"## System Prompt {index}"
        lines.extend([heading, "", "```text", prompt, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def materialize_all(
    root, output, names, config_path=None, generate_priority_workbook=True,
    symlink_classified=False, pass_challenge=False,
):
    all_rows, summaries = [], []
    for spec in selected_specs(root, names, config_path):
        rows, summary = materialize_subject(
            output, spec, generate_priority_workbook, symlink_classified,
            pass_challenge=pass_challenge,
        )
        all_rows.extend(rows)
        summaries.append(summary)
    all_rows.sort(key=lambda row: (row["subject"], row["short_id"]))
    write_csv(output / "全学科审核结果.csv", all_rows, CSV_FIELDS)
    totals = Counter(row["decision"] for row in all_rows)
    write_json(output / "全学科审核摘要.json", {
        "completed": len(all_rows),
        "counts": {decision: totals.get(decision, 0) for decision in ("PASS", "REVIEW", "DROP")},
        "subjects": summaries,
    })
    drop_rows = [row for row in all_rows if row["decision"] == "DROP"]
    report_lines = [
        "# 全学科知识型书籍 LLM 审核摘要",
        "",
        f"- 输入与完成：{len(all_rows)} 本",
        f"- PASS：{totals.get('PASS', 0)} 本",
        f"- REVIEW：{totals.get('REVIEW', 0)} 本",
        f"- DROP：{totals.get('DROP', 0)} 本",
        "",
        "## 分学科结果",
        "",
        "| 学科 | 总数 | PASS | REVIEW | DROP |",
        "|---|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        counts = summary["counts"]
        report_lines.append(
            f"| {summary['subject']} | {summary['completed']} | {counts['PASS']} | "
            f"{counts['REVIEW']} | {counts['DROP']} |"
        )
    report_lines.extend([
        "",
        "## DROP 模型标记",
        "",
        f"- 大量非中英文：{sum(row['substantial_non_zh_en'] is True for row in drop_rows)} 本",
        f"- OCR 质量低：{sum(row['ocr_quality'] == 'low' for row in drop_rows)} 本",
        f"- 语义质量低：{sum(row['semantic_quality'] == 'low' for row in drop_rows)} 本",
        f"- 年代风险高：{sum(row['age_risk'] == 'high' for row in drop_rows)} 本",
        "",
        "以上原因可能重叠。详细证据、问题锚点和人工复查目标见 `全学科审核结果.csv`。",
        "模型仅审查每本 MD 的均匀分布抽样片段，没有查看 PDF；实际抽样数量见输入清单，REVIEW 应作为后续人工审核的主要工作区。",
    ])
    (output / "全学科审核摘要.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    run_entries = []
    for spec in selected_specs(root, names, config_path):
        manifest_path = output / "输入清单" / f"{spec['name']}.json"
        run_config_path = output / spec["name"] / "模型原始结果" / "run_config.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_config = (
            json.loads(run_config_path.read_text(encoding="utf-8"))
            if run_config_path.exists()
            else {
                "api_url": DEFAULT_API_URL,
                "model": DEFAULT_MODEL,
                "strict_text_quality": False,
                "max_generic_headword_ratio": 0.15,
                "min_candidate_headwords": 20,
                "knowledge_value_mode": False,
                "max_nonknowledge_headword_ratio": 0.15,
                "drop_nonknowledge_headword_ratio": 0.50,
                "min_headword_evidence": 10,
                "system_prompt": SYSTEM_PROMPT,
            }
        )
        run_entries.append({
            "subject": spec["name"],
            "sampling_rule": manifest.get("sampling_rule", ""),
            **run_config,
        })
    prompt_snapshot = build_prompt_snapshot(run_entries)
    (output / "本次模型审核配置与提示词.md").write_text(prompt_snapshot, encoding="utf-8")
    return summaries


def main():
    parser = argparse.ArgumentParser(description="跨学科知识型书籍 Markdown 三级 LLM 审核")
    parser.add_argument("action", choices=("prepare", "run", "challenge", "materialize", "all"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, help="学科与书籍目录 JSON 配置；省略时使用内置十学科配置")
    parser.add_argument("--subject", action="append", dest="subjects", help="可重复；默认 all")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=100)
    parser.add_argument("--parallel-subjects", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2500)
    parser.add_argument("--sample-count", type=int, default=12)
    parser.add_argument("--chunk-chars", type=int, default=1200)
    parser.add_argument(
        "--strict-text-quality",
        action="store_true",
        help="启用正文零明确错误、正文语言和候选词头质量硬门槛",
    )
    parser.add_argument(
        "--max-generic-headword-ratio",
        type=float,
        default=0.15,
        help="严格模式允许的泛化或非知识点候选词头最大比例",
    )
    parser.add_argument(
        "--min-candidate-headwords",
        type=int,
        default=20,
        help="严格模式允许 PASS 所需的最少候选词头证据量",
    )
    parser.add_argument(
        "--knowledge-value-mode",
        action="store_true",
        help="启用主体知识可用性判断、带上下文抽样和原文证据核对，不使用词头比例门槛",
    )
    parser.add_argument(
        "--max-nonknowledge-headword-ratio",
        type=float,
        default=0.15,
        help="旧参数，仅兼容命令行；定性模式不使用比例门槛",
    )
    parser.add_argument(
        "--drop-nonknowledge-headword-ratio",
        type=float,
        default=0.50,
        help="旧参数，仅兼容命令行；定性模式不使用比例门槛",
    )
    parser.add_argument(
        "--min-headword-evidence",
        type=int,
        default=10,
        help="旧参数，仅兼容命令行；定性模式不使用词头个数门槛",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--pending-limit",
        type=int,
        help="每次只处理指定数量的未完成书目，便于按 API 限额分批续跑",
    )
    parser.add_argument(
        "--skip-priority-workbook",
        action="store_true",
        help="仅生成 CSV 与三级分类，不生成重点问题 MD 段落工作簿",
    )
    parser.add_argument(
        "--symlink-classified",
        action="store_true",
        help="三级目录使用指向原 MD 的符号链接，避免重复复制大文件",
    )
    parser.add_argument(
        "--pass-challenge",
        action="store_true",
        help="对可疑 PASS 启用独立反证复核；all 会运行该阶段，materialize 会覆盖其结果",
    )
    parser.add_argument(
        "--challenge-sample-count",
        type=int,
        default=8,
        help="PASS 反证复核的最大样本数，默认 8",
    )
    args = parser.parse_args()
    if not 0 <= args.max_generic_headword_ratio <= 1:
        parser.error("--max-generic-headword-ratio 必须在 0 到 1 之间")
    if args.min_candidate_headwords < 1:
        parser.error("--min-candidate-headwords 必须为正整数")
    if not 0 <= args.max_nonknowledge_headword_ratio <= args.drop_nonknowledge_headword_ratio <= 1:
        parser.error("知识价值比例必须满足 0 <= PASS上限 <= DROP阈值 <= 1")
    if args.min_headword_evidence < 1:
        parser.error("--min-headword-evidence 必须为正整数")
    if args.challenge_sample_count < 3:
        parser.error("--challenge-sample-count 至少为 3")
    names = args.subjects or ["all"]
    args.output.mkdir(parents=True, exist_ok=True)

    if args.action in {"prepare", "all"}:
        print(json.dumps(prepare(
            args.root, args.output, args.config, args.sample_count, args.chunk_chars,
            knowledge_value_mode=args.knowledge_value_mode,
        ), ensure_ascii=False, indent=2))
    if args.action in {"run", "all"}:
        specs = selected_specs(args.root, names, args.config)

        def run_spec(spec):
            return run_subject(
                args.output, spec, args.api_url, args.model, args.workers,
                args.timeout, args.retries, args.max_tokens, args.limit,
                args.pending_limit,
                args.strict_text_quality,
                args.max_generic_headword_ratio,
                args.min_candidate_headwords,
                args.knowledge_value_mode,
                args.max_nonknowledge_headword_ratio,
                args.drop_nonknowledge_headword_ratio,
                args.min_headword_evidence,
            )

        if args.parallel_subjects <= 1:
            for spec in specs:
                print(json.dumps(run_spec(spec), ensure_ascii=False), flush=True)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(args.parallel_subjects, len(specs))
            ) as executor:
                futures = {executor.submit(run_spec, spec): spec for spec in specs}
                for future in concurrent.futures.as_completed(futures):
                    print(json.dumps(future.result(), ensure_ascii=False), flush=True)
    if args.action == "challenge" or (args.action == "all" and args.pass_challenge):
        specs = selected_specs(args.root, names, args.config)

        def challenge_spec(spec):
            return run_pass_challenge_subject(
                args.output, spec, args.api_url, args.model, args.workers,
                args.timeout, args.retries, args.max_tokens,
                pending_limit=args.pending_limit,
                challenge_sample_count=args.challenge_sample_count,
            )

        if args.parallel_subjects <= 1:
            for spec in specs:
                print(json.dumps(challenge_spec(spec), ensure_ascii=False), flush=True)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(args.parallel_subjects, len(specs))
            ) as executor:
                futures = {executor.submit(challenge_spec, spec): spec for spec in specs}
                for future in concurrent.futures.as_completed(futures):
                    print(json.dumps(future.result(), ensure_ascii=False), flush=True)
    if args.action in {"materialize", "all"}:
        print(json.dumps(
            materialize_all(
                args.root,
                args.output,
                names,
                args.config,
                generate_priority_workbook=not args.skip_priority_workbook,
                symlink_classified=args.symlink_classified,
                pass_challenge=args.pass_challenge,
            ),
            ensure_ascii=False,
            indent=2,
        ))


if __name__ == "__main__":
    main()
