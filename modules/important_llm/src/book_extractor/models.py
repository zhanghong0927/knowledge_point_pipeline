"""定义抽取、复核、分组与交付数据，并验证来源引用、语言和候选覆盖；

不承担后置知识清洗。
"""

import re
from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

Name = Annotated[str, Field(min_length=1)]
DefinitionSupported = Annotated[
    bool,
    Field(
        description=(
            "Whether the source supports any substantive statement about the object. "
            "An explicitly stated class membership or relation IS sufficient: "
            "return true even if formulas, mechanisms and a full definition "
            "are absent. "
            "A mere name, page reference or acknowledgment without such a statement "
            "is insufficient. False requires definition=null and conditions=[]."
        )
    ),
]
Definition = Annotated[
    str | None,
    Field(
        description=(
            "Only source-supported knowledge about the object. Never include "
            "commentary such as what the source does not provide, cannot define, "
            "or merely mentions: put all such limitations in support_reason/issues. "
            "Use null when definition_supported is false."
        )
    ),
]
Confidence = Annotated[
    Literal["high", "medium", "low"],
    Field(
        description=(
            "Fidelity of the extraction and support decision, not certainty of "
            "the author's claims or completeness of the source. A faithfully "
            "stated hypothesis or limited classification can be high."
        )
    ),
]
PARTITION_FEEDBACK_LIMIT = 20
UNIT_ID_WIDTH = 6
ENGLISH_SOURCE_WORD_MINIMUM = 3
PARENTHESIZED_ENGLISH_PATTERN = re.compile(r"[A-Za-z]{3,}")


def partition_error(
    prefix: str, represented: list[str], expected: Iterable[str] | None
) -> str:
    """按预期 ID 集合返回有界的分区错误说明；

    只列可信缺失或重复 ID，未知模型值仅计数。
    """
    allowed = set(expected) if expected is not None else set()
    counts = Counter(represented)
    missing = sorted(allowed - counts.keys())
    duplicates = sorted(key for key in allowed if counts[key] > 1)
    unexpected = None
    if expected is not None:
        unexpected = sum(count for key, count in counts.items() if key not in allowed)
    return (
        f"{prefix}; missing_expected_ids={missing[:PARTITION_FEEDBACK_LIMIT]}; "
        f"duplicate_expected_ids={duplicates[:PARTITION_FEEDBACK_LIMIT]}; "
        f"missing_expected_count={len(missing)}; "
        f"duplicate_expected_count={len(duplicates)}; "
        f"unexpected_count={unexpected}"
    )


def member_evidence_error(
    location: tuple[str | int, ...],
    message: str,
    members: Iterable[str],
    allowed: Iterable[str],
    *,
    error_type: str = "evidence_id_not_allowed",
    unknown_count: int = 0,
) -> ValidationError:
    """为成员引用错误补精确位置和有界可信ID；不携带模型输入或源文。"""

    def safe_ids(values: Iterable[str]) -> list[str]:
        """只展示短标识符，限量反馈，不把任意上下文文字当作ID复述。"""
        return sorted(
            value
            for value in set(values)
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        )[:PARTITION_FEEDBACK_LIMIT]

    member_ids = set(members)
    allowed_ids = set(allowed)
    trusted = {
        "candidate_ids": safe_ids(member_ids),
        "allowed_evidence_ids": safe_ids(allowed_ids),
        "allowed_evidence_count": len(allowed_ids),
        "unknown_count": unknown_count,
    }
    feedback = (
        f"{message}; candidate_ids={trusted['candidate_ids']}; "
        f"allowed_evidence_ids={trusted['allowed_evidence_ids']}; "
        f"allowed_evidence_count={len(allowed_ids)}; unknown_count={unknown_count}"
    )
    return ValidationError.from_exception_data(
        "SynthesisReview",
        [
            {
                "type": PydanticCustomError(error_type, feedback, trusted),
                "loc": location,
                "input": None,
            }
        ],
    )


class StrictModel(BaseModel):
    """所有响应的严格基类；拒绝额外字段和损坏文本，不静默丢弃模型输出。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_control_characters(cls, value: Any) -> Any:
        """递归检查输入中的控制字符；

        返回原输入，不改写文本，发现损坏转义时抛结构化校验错误。
        """

        def inspect(item: Any) -> None:
            """遍历字典和列表中的字符串；

            发现不允许的控制字符时抛异常，不产生输出副作用。
            """
            if isinstance(item, str) and re.search(r"[\x00-\x09\x0b-\x1f]", item):
                raise PydanticCustomError(
                    "invalid_control_character",
                    (
                        "Invalid control character in text: escape LaTeX backslashes "
                        "correctly in JSON (e.g. \\\\beta), or preserve "
                        "the source Unicode "
                        "symbol"
                    ),
                )
            if isinstance(item, dict):
                for child in item.values():
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)

        inspect(value)
        return value


def is_english_source(text: str) -> bool:
    """复用已观察到的纯英文判据：常见英文词达到阈值且来源无汉字。

    这不是通用语言识别；仅供校验与同边界提示选择共享，不改写来源。
    """
    # This conservative guard catches observed English→Chinese translation;
    # it is not a general language detector or a Simplified/Traditional mapper.
    prose = re.sub(r"<[^>]*>", " ", text)
    english_words = re.findall(
        r"\b(?:the|is|are|of|and|with|that|for|from|this)\b", prose, re.I
    )
    return len(english_words) >= ENGLISH_SOURCE_WORD_MINIMUM and not re.search(
        r"[\u3400-\u9fff]", text
    )


def validate_source_language(
    name: str,
    definition: str | None,
    aliases: list[str],
    conditions: list[str],
    evidence_ids: list[str],
    info: ValidationInfo,
) -> None:
    """依据最终引用的来源拒绝英文原文中新增的中文解释；

    通过时不返回内容；发现擅自翻译时抛结构化校验错误，不修改传入字段。
    """
    sources = (info.context or {}).get("evidence_texts", {})
    text = "\n".join(sources[key] for key in evidence_ids if key in sources)
    output = "\n".join([name, definition or "", *aliases, *conditions])
    # 短列表/表格可能没有足够英文虚词，仍不能凭空译出中文。
    # 此处只确认拉丁文字证据，不判它一定是英语；不得据此选择英文输出提示。
    prose = re.sub(r"<[^>]*>|\$[^$]*\$|`[^`]*`", " ", text)
    latin_prose = len(re.findall(r"\b[A-Za-z]{2,}\b", prose)) >= 3 and not re.search(
        r"[\u3400-\u9fff]", text
    )
    if (is_english_source(text) or latin_prose) and re.search(
        r"[\u3400-\u9fff]", output
    ):
        raise PydanticCustomError(
            "source_language_changed",
            "Source evidence has no Chinese: preserve its language in name, "
            "definition, aliases and conditions; do not translate into Chinese",
        )
    return


def validate_prose_ids(
    name: str,
    definition: str | None,
    aliases: list[str],
    conditions: list[str],
    info: ValidationInfo,
) -> None:
    """检查正文类字段中的内部 ID；

    非原文固有内容只能出现在引用字段，否则抛结构化校验错误。
    """
    sources = (info.context or {}).get("evidence_texts", {})
    source_text = "\n".join(sources.values())
    prose = "\n".join([name, definition or "", *aliases, *conditions])
    for identifier in re.findall(r"\bu[0-9]{6}\b", prose):
        if identifier in sources and identifier not in source_text:
            raise PydanticCustomError(
                "internal_id_in_prose",
                (
                    "Internal source unit IDs belong only in evidence_ids, not "
                    "name/definition/conditions/aliases"
                ),
            )
    return


def validate_source_aliases(
    name: str, aliases: list[str], evidence_ids: list[str], info: ValidationInfo
) -> None:
    """校验别名及括号英文由最终引用支持；未引用的已给来源不能代为供证。"""
    sources = (info.context or {}).get("evidence_texts", {})
    if not sources:
        return

    def comparison_form(text: str) -> str:
        """仅归一单字母下标的行内数学排版用于比较，不改写交付文本。

        例如原文“$ T_{2} $ , relaxation”可支持“T2 relaxation”；
        不展开公式、补词、翻译或推导外部同义词。
        """
        text = re.sub(
            r"\$\s*([A-Za-z])_\s*(?:\{([A-Za-z0-9]+)\}|([A-Za-z0-9]))\s*\$\s*,?",
            lambda match: match[1] + (match[2] or match[3]) + " ",
            text,
        )
        return re.sub(r"\s+", "", text).casefold()

    source = comparison_form(
        "\n".join(sources[key] for key in evidence_ids if key in sources)
    )
    for alias in aliases:
        if not alias.strip() or comparison_form(alias) not in source:
            raise PydanticCustomError(
                "source_alias_not_found",
                (
                    "aliases must occur in supplied source text; remove invented "
                    "synonyms or translations"
                ),
            )
    if not re.search(r"[\u3400-\u9fff]", name):
        return
    for parenthesis in re.findall(r"[（(]([^（）()]*)[）)]", name):
        phrase = re.sub(r"\s+", "", parenthesis).casefold()
        if PARENTHESIZED_ENGLISH_PATTERN.search(parenthesis) and phrase not in source:
            raise PydanticCustomError(
                "source_parenthesized_translation_not_found",
                "Do not add an English translation in parentheses to a "
                "source-language name unless that phrase occurs in source",
            )
    return


class EvidenceModel(StrictModel):
    """要求至少一条有效来源引用的响应基类；引用所属范围由本次验证上下文提供。"""

    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """根据上下文校验引用并按顺序去重；

        只修正已知单元 ID 的零填充，未知引用抛结构化校验错误。
        """
        allowed = (info.context or {}).get("evidence_ids")
        if allowed is None:
            return list(dict.fromkeys(value))
        allowed = set(allowed)
        normalized = []
        for identifier in value:
            # 仅修复允许集合内单元 ID 的零填充，不猜测其他来源。
            numeric_id = re.fullmatch(rf"u[0-9]{{1,{UNIT_ID_WIDTH}}}", identifier)
            if identifier not in allowed and numeric_id:
                canonical = f"u{int(identifier[1:]):0{UNIT_ID_WIDTH}d}"
                if canonical in allowed:
                    identifier = canonical
            if identifier not in allowed:
                raise PydanticCustomError(
                    "evidence_id_not_allowed",
                    "evidence_ids must belong to the supplied source units",
                )
            normalized.append(identifier)
        return list(dict.fromkeys(normalized))


class Finding(EvidenceModel):
    """有局部来源支持的知识候选；允许无释义并标记问题，尚未经过独立后置清洗。"""

    name: Name
    aliases: list[str]
    category: Name
    support_reason: Name
    definition_supported: DefinitionSupported
    definition: Definition
    confidence: Confidence
    conditions: list[str]
    issues: list[str]

    @field_validator("name", "category")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """检查名称或分类含可见字符并返回去除两端空白的字符串；空值抛 ValueError。"""
        if not value.strip():
            raise ValueError("must contain visible text")
        return value.strip()

    @model_validator(mode="after")
    def preserve_expected_name(self, info: ValidationInfo) -> "Finding":
        """单条原文复核必须保持名称，在纠错及缓存校验边界一并检查。"""
        expected = (info.context or {}).get("expected_name")
        if expected is not None and self.name != expected:
            raise PydanticCustomError(
                "verification_name_mismatch",
                "Verification must preserve the supplied concept name",
            )
        return self

    @model_validator(mode="after")
    def mark_missing(self) -> "Finding":
        """将空释义规范为 None 并追加缺失标记；原位更新当前实例后返回，不补写内容。"""
        if self.definition is None or not self.definition.strip():
            self.definition = None
            if "definition_missing" not in self.issues:
                self.issues.append("definition_missing")
        return self

    @model_validator(mode="after")
    def check_definition_support(self) -> "Finding":
        """核对来源判定与输出一致；不足时必须空释义且无条件，不自动丢弃矛盾输出。

        旧Record的未知判定仅用于读取历史，新的模型响应必须明确判断并填写依据。
        此处检查协议一致性，不能替代对模型来源判定的语义审查。
        """
        if self.definition_supported is None:
            return self
        if self.definition_supported != (self.definition is not None) or (
            not self.definition_supported and self.conditions
        ):
            raise PydanticCustomError(
                "definition_support_conflict",
                "definition_supported=true requires a substantive definition; "
                "false requires definition=null and conditions=[]; "
                "keep source limitations only in support_reason/issues",
            )
        return self

    @field_validator("support_reason")
    @classmethod
    def support_reason_nonblank(cls, value: str | None) -> str | None:
        """返回去除首尾空白的来源判断依据；旧记录允许未知，新响应不得空白。"""
        return None if value is None else cls.nonblank(value)

    @model_validator(mode="after")
    def preserve_source_script(self, info: ValidationInfo) -> "Finding":
        """拒绝英文来源被主动翻译成中文，校验通过返回当前候选。"""
        validate_source_language(
            self.name,
            self.definition,
            self.aliases,
            self.conditions,
            self.evidence_ids,
            info,
        )
        return self

    @model_validator(mode="after")
    def keep_internal_ids_out_of_prose(self, info: ValidationInfo) -> "Finding":
        """拒绝把非原文固有的内部来源ID写进正文，保留引用字段中的定位。"""
        validate_prose_ids(
            self.name, self.definition, self.aliases, self.conditions, info
        )
        return self

    @model_validator(mode="after")
    def keep_aliases_and_translations_source_bound(
        self, info: ValidationInfo
    ) -> "Finding":
        """校验别名及括号译名可在来源中找到，拒绝擅加同义词或翻译。"""
        validate_source_aliases(self.name, self.aliases, self.evidence_ids, info)
        return self


class NameAnchor(EvidenceModel):
    """名称发现阶段的源文锚点；尚未执行释义任务，不含缺释义状态。"""

    name: Name

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """规范名称两端空白，拒绝没有可见字符的名称。"""
        return Finding.nonblank(value)

    @model_validator(mode="after")
    def source_bound(self, info: ValidationInfo) -> "NameAnchor":
        """复用语言、内部引用和擅加译名保护，不构造临时释义。"""
        validate_source_language(self.name, None, [], [], self.evidence_ids, info)
        validate_prose_ids(self.name, None, [], [], info)
        validate_source_aliases(self.name, [], self.evidence_ids, info)
        return self


class NameDiscovery(StrictModel):
    """只发现名称和来源的片段响应；上下文状态描述名称身份是否可确定。"""

    findings: list[NameAnchor]
    needs_context: bool
    context_reason: str


class ReviewedFinding(Finding):
    """复核后的知识候选及其草稿引用。

    草稿 ID 须遵守本阶段输入与完整分区约束；是否允许新增由任务协议指定。
    """

    draft_ids: list[str]


class NameDecision(EvidenceModel):
    """对一个输入候选的名称判定；理由及引用必须明确，不以缺少定义代替身份判断。"""

    candidate_id: Name
    # Schema先展示依据再展示结论；只改变输出引导顺序，不放松判定或引用校验。
    reason: Name
    decision: Literal["accept", "reject", "uncertain"]

    @field_validator("candidate_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """拒绝只有空白的身份或理由。"""
        return Finding.nonblank(value)


class SynthesisReview(StrictModel):
    """逐候选判定名称后生成释义；拒绝项有独立去向，不确定项必须保留。"""

    name_decisions: list[NameDecision]
    findings: list[ReviewedFinding]

    @model_validator(mode="after")
    def partition(self, info: ValidationInfo) -> "SynthesisReview":
        """严格覆盖全部名称判定及被保留成员，验证引用和允许的同义组边界。"""
        context = info.context or {}
        expected = context.get("draft_ids")
        decisions = [item.candidate_id for item in self.name_decisions]
        if len(decisions) != len(set(decisions)) or (
            expected is not None and set(decisions) != set(expected)
        ):
            raise PydanticCustomError(
                "name_decision_partition",
                partition_error(
                    "Name decisions must cover every candidate exactly once",
                    decisions,
                    expected,
                ),
            )
        retained = {
            item.candidate_id
            for item in self.name_decisions
            if item.decision != "reject"
        }
        ids = [key for finding in self.findings for key in finding.draft_ids]
        if (
            any(not finding.draft_ids for finding in self.findings)
            or len(ids) != len(set(ids))
            or set(ids) != retained
        ):
            raise PydanticCustomError(
                "synthesis_partition",
                partition_error(
                    "Findings must cover accept/uncertain candidates exactly once",
                    ids,
                    retained,
                ),
            )
        evidence = context.get("candidate_evidence_ids")
        groups = context.get("candidate_groups")
        if evidence is not None:
            for index, item in enumerate(self.name_decisions):
                # groups由已校验Grouping产生；uncertain只能是单例。
                # 仅名称身份可用已确认同组来源，Finding的成员范围仍在下方单独校验。
                group = next(
                    (
                        members
                        for members in groups or []
                        if item.candidate_id in members
                    ),
                    [item.candidate_id],
                )
                allowed = {key for member in group for key in evidence.get(member, [])}
                if not set(item.evidence_ids) <= allowed:
                    raise member_evidence_error(
                        ("name_decisions", index, "evidence_ids"),
                        "Name decision evidence must belong to its confirmed group",
                        group,
                        allowed,
                        unknown_count=len(set(item.evidence_ids) - allowed),
                    )
        context = info.context or {}
        groups = context.get("candidate_groups")
        evidence = context.get("candidate_evidence_ids")
        body = context.get("candidate_body_ids")
        for index, finding in enumerate(self.findings):
            members = set(finding.draft_ids)
            if groups is not None and not any(
                members <= set(group) for group in groups
            ):
                raise PydanticCustomError(
                    "synthesis_partition", "Synthesis cannot merge independent groups"
                )
            if evidence is not None:
                allowed = {key for member in members for key in evidence[member]}
                if not set(finding.evidence_ids) <= allowed:
                    raise member_evidence_error(
                        ("findings", index, "evidence_ids"),
                        "Finding evidence must belong to its members",
                        members,
                        allowed,
                        unknown_count=len(set(finding.evidence_ids) - allowed),
                    )
            if body is not None and finding.definition is not None:
                supported = {key for member in members for key in body[member]}
                if not supported or not set(finding.evidence_ids) <= supported:
                    raise member_evidence_error(
                        ("findings", index, "evidence_ids"),
                        "Name-only sources cannot support a definition",
                        members,
                        supported,
                        error_type="name_only_definition",
                        unknown_count=len(set(finding.evidence_ids) - supported),
                    )
            body_texts = context.get("candidate_body_texts")
            if body_texts is not None:
                texts: dict[str, list[str]] = {}
                name_texts = context.get("candidate_name_texts", {})
                for member in finding.draft_ids:
                    owned = [body_texts[member]]
                    if finding.definition is None:
                        owned.append(name_texts.get(member, {}))
                    for fragments in owned:
                        for key, text in fragments.items():
                            if key in finding.evidence_ids:
                                texts.setdefault(key, [])
                                if text not in texts[key]:
                                    texts[key].append(text)
                # 文字保护不能使用全批上下文：同Unit的其他成员片段、
                # 或中文候选均不能为当前成员的别名/翻译提供假支持。
                try:
                    Finding.model_validate(
                        finding.model_dump(exclude={"draft_ids"}),
                        context={
                            "evidence_ids": set(texts),
                            "evidence_texts": {
                                key: "\n".join(parts) for key, parts in texts.items()
                            },
                        },
                    )
                except ValidationError as error:
                    # 保留具体错误类型和成员位置，让原生reask能定位需修复的finding。
                    raise ValidationError.from_exception_data(
                        type(self).__name__,
                        [
                            {
                                "type": PydanticCustomError(
                                    item["type"], "{message}", {"message": item["msg"]}
                                ),
                                "loc": ("findings", index, *item["loc"]),
                                "input": item.get("input"),
                            }
                            for item in error.errors(include_url=False)
                        ],
                    ) from error
        return self


class IndependentFinding(EvidenceModel):
    """独立候选的语义字段；省略name表示沿用输入名称，空列表由脚本补齐。"""

    name: Name | None = None
    aliases: list[str] = Field(default_factory=list)
    category: Name
    support_reason: Name
    definition_supported: DefinitionSupported
    definition: Definition
    confidence: Confidence
    conditions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class IndependentItem(NameDecision):
    """独立候选的一次语义判断；身份只回写一次，不让模型填写成员分区。"""

    finding: IndependentFinding | None


class IndependentSynthesis(StrictModel):
    """独立候选综合响应，脚本按可信映射补齐交付协议中的成员ID。"""

    items: list[IndependentItem]
    _member_errors: dict[str, list[dict[str, Any]]] = PrivateAttr(default_factory=dict)
    _ordinary_attempts: int = PrivateAttr(default=0)

    @staticmethod
    def select_context(
        context: dict[str, Any], identifiers: Iterable[str]
    ) -> dict[str, Any]:
        """按请求内短编号选取独立成员上下文，保留原始来源与语言约束。"""
        selected = set(identifiers)
        mapping = {
            key: original
            for key, original in context["independent_ids"].items()
            if key in selected
        }
        return {
            **context,
            "independent_ids": mapping,
            "draft_ids": list(mapping.values()),
            "candidate_groups": [[original] for original in mapping.values()],
        }

    @model_validator(mode="wrap")
    @classmethod
    def retain_valid_members(
        cls,
        value: Any,
        handler: ModelWrapValidatorHandler,
        info: ValidationInfo,
    ) -> "IndependentSynthesis":
        """仅在独立综合重试模式保留逐项严格验证成功的响应，失败项交由调用方重试。

        非法外层、重复或未知编号无法安全归属，仍拒绝整响应；普通模式保持完整覆盖校验。
        缓存中只保存验证成功的项，不能把未验证字段带入交付记录。
        """
        context = info.context or {}
        if not context.get("independent_partial"):
            return handler(value)
        if any(len(group) != 1 for group in context["candidate_groups"]):
            raise ValueError("Partial synthesis requires independent groups")
        raw = value.model_dump() if isinstance(value, BaseModel) else value
        if (
            not isinstance(raw, dict)
            or set(raw) != {"items"}
            or not isinstance(raw["items"], list)
        ):
            raise ValueError("Expected an object containing only an items array")
        identifiers = [
            item.get("candidate_id") if isinstance(item, dict) else None
            for item in raw["items"]
        ]
        if any(
            not isinstance(key, str) or key not in context["independent_ids"]
            for key in identifiers
        ) or len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate or unknown independent candidate IDs")
        valid = []
        errors = {
            key: [{"type": "missing_candidate", "loc": ["items"]}]
            for key in context["independent_ids"]
            if key not in identifiers
        }
        fields = {
            "items",
            *IndependentItem.model_fields,
            *IndependentFinding.model_fields,
        }
        for item, key in zip(raw["items"], identifiers):
            member_context = cls.select_context(context, [key])
            member_context["independent_partial"] = False
            try:
                member = cls.model_validate(
                    {"items": [item]}, context=member_context, strict=True
                )
            except ValidationError as error:
                # 只保存程序字段路径及错误类型，绝不把模型伪造值或原文放进纠错诊断。
                errors[key] = [
                    {
                        "type": detail["type"],
                        "loc": [
                            part
                            if isinstance(part, int) or part in fields
                            else "<field>"
                            for part in detail["loc"][:12]
                        ],
                    }
                    for detail in error.errors(include_input=False)[:20]
                ]
                continue
            valid.extend(member.items)
        response = handler({"items": valid})
        response._member_errors = errors
        return response

    def to_review(self, context: dict[str, Any]) -> SynthesisReview:
        """根据请求内编号映射生成完整审计及释义，并执行原有全部来源校验。

        context须提供independent_ids及原综合校验上下文。漏项、重复、未知编号、
        拒绝却保留释义或接收却无finding均抛错，不自动补证或接受部分结果。
        """
        mapping = context["independent_ids"]
        represented = [item.candidate_id for item in self.items]
        if len(represented) != len(set(represented)) or set(represented) != set(
            mapping
        ):
            raise PydanticCustomError(
                "independent_partition",
                partition_error(
                    "Each item must occur exactly once", represented, mapping
                ),
            )
        decisions = []
        findings = []
        for item in self.items:
            if (item.decision == "reject") != (item.finding is None):
                raise PydanticCustomError(
                    "independent_finding",
                    "Reject requires finding=null; accept/uncertain requires a finding",
                )
            original = mapping[item.candidate_id]
            decisions.append(
                {**item.model_dump(exclude={"finding"}), "candidate_id": original}
            )
            if item.finding is not None:
                findings.append(
                    {
                        **item.finding.model_dump(),
                        "name": item.finding.name
                        or context["independent_names"][original],
                        "draft_ids": [original],
                    }
                )
        try:
            return SynthesisReview.model_validate(
                {"name_decisions": decisions, "findings": findings}, context=context
            )
        except ValidationError as error:
            # 纠错提示也使用模型见过的槽位和短编号，不要求模型反查内部长ID。
            retained_positions = [
                index
                for index, item in enumerate(self.items)
                if item.finding is not None
            ]
            details = []
            for detail in error.errors(include_url=False):
                location = detail["loc"]
                if len(location) >= 2 and location[0] == "findings":
                    location = (
                        "items",
                        retained_positions[location[1]],
                        "finding",
                        *location[2:],
                    )
                elif len(location) >= 2 and location[0] == "name_decisions":
                    location = ("items", *location[1:])
                message = detail["msg"]
                for short, original in mapping.items():
                    message = message.replace(original, short)
                details.append(
                    {
                        "type": PydanticCustomError(
                            detail["type"], "{message}", {"message": message}
                        ),
                        "loc": location,
                        "input": None,
                    }
                )
            raise ValidationError.from_exception_data(
                type(self).__name__, details
            ) from error

    @model_validator(mode="after")
    def validate_assembled_review(self, info: ValidationInfo) -> "IndependentSynthesis":
        """校验组装后的来源与覆盖；独立修复模式仅组装已通过逐项校验的成员。"""
        if info.context is not None and "independent_ids" in info.context:
            context = info.context
            if context.get("independent_partial"):
                context = self.select_context(
                    context, [item.candidate_id for item in self.items]
                )
            self.to_review(context)
        return self


class EvidenceSpan(StrictModel):
    """原始 Unit.text 中的字符半开区间；综合入口另校验所属单元和实际长度。"""

    unit_id: str
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def nonempty(self) -> "EvidenceSpan":
        """拒绝反向或空区间，不修改原文偏移。"""
        if self.end <= self.start:
            raise ValueError("Evidence span must be a nonempty half-open interval")
        return self


class Candidate(StrictModel):
    """逐书名称和来源锚点；不包含生成释义，名称出处与释义证据分开保存。"""

    candidate_id: str
    chunk_id: str
    scope: list[str]
    name: Name
    evidence_ids: list[str]
    aliases: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    origins: list[Literal["body", "toc", "index", "glossary"]] = Field(
        default_factory=lambda: ["body"]
    )
    name_evidence_ids: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """拒绝空白名称，沿用名称阶段的文字规范。"""
        return Finding.nonblank(value)

    @model_validator(mode="after")
    def validate_sources(self, info: ValidationInfo) -> "Candidate":
        """核验正文、名称来源及原文字符跨度；上下文存在时检查实际归属和长度。"""
        referenced = set(self.evidence_ids) | set(self.name_evidence_ids)
        if not referenced:
            raise ValueError("Candidate requires body or name evidence")
        context = info.context or {}
        allowed = context.get("evidence_ids")
        if allowed is not None and not referenced <= set(allowed):
            raise PydanticCustomError(
                "evidence_id_not_allowed",
                "Candidate references must belong to this book",
            )
        if self.evidence_spans and {s.unit_id for s in self.evidence_spans} != set(
            self.evidence_ids
        ):
            raise ValueError("Spans must cover exactly the candidate evidence IDs")
        texts = context.get("evidence_texts")
        if texts is not None:
            if not referenced <= texts.keys():
                raise ValueError("Candidate source text is missing")
            for span in self.evidence_spans:
                if span.end > len(texts[span.unit_id]):
                    raise ValueError("Evidence span is outside original Unit.text")
        return self


class Group(StrictModel):
    """建议同义的候选组或不确定单例；其成员 ID 来自当前书籍。"""

    candidate_ids: list[str] = Field(min_length=1)
    status: Literal["same", "uncertain"]


class Grouping(StrictModel):
    """输入候选的完整无重叠分组结果；不承担跨书合并。"""

    groups: list[Group]

    @model_validator(mode="after")
    def partition(self, info: ValidationInfo) -> "Grouping":
        """校验成员完整覆盖且无重复；不确定组必须是单例，违规时返回有界错误说明。"""
        members = [member for group in self.groups for member in group.candidate_ids]
        allowed = (info.context or {}).get("candidate_ids")
        if len(members) != len(set(members)):
            raise PydanticCustomError(
                "candidate_partition",
                partition_error(
                    "each candidate must appear exactly once", members, allowed
                ),
            )
        if allowed is not None and set(members) != set(allowed):
            raise PydanticCustomError(
                "candidate_partition",
                partition_error(
                    "groups must cover exactly the supplied candidate_ids",
                    members,
                    allowed,
                ),
            )
        if any(
            g.status == "uncertain" and len(g.candidate_ids) != 1 for g in self.groups
        ):
            raise PydanticCustomError(
                "candidate_partition",
                partition_error(
                    "uncertain candidates must remain separate singletons",
                    members,
                    allowed,
                ),
            )
        boundaries = (info.context or {}).get("comparison_buckets")
        if boundaries is not None and any(
            not any(set(group.candidate_ids) <= set(bucket) for bucket in boundaries)
            for group in self.groups
        ):
            raise PydanticCustomError(
                "candidate_partition", "Groups cannot merge across comparison_buckets"
            )
        return self


class Record(Finding):
    """逐书交付后置清洗的记录；保留运行、原候选和来源身份，不宣称全库唯一或已清洗。"""

    confidence: Literal["high", "medium", "low"] | None = None
    support_reason: Name | None = None
    definition_supported: bool | None = None

    record_id: str
    book_id: str
    run_id: str
    candidate_ids: list[str] = Field(min_length=1)
    scope: list[str]


class RegionDecision(StrictModel):
    """针对既有来源区域的保留、排除或不确定决定；不改写区域文本。"""

    region_id: str
    action: Literal["retain", "exclude", "uncertain"]
    reason: str


class Regions(StrictModel):
    """区域判断响应；未返回的区域由调用方保守保留。"""

    decisions: list[RegionDecision]

    @model_validator(mode="after")
    def known_regions(self, info: ValidationInfo) -> "Regions":
        """检查区域 ID 属于本次输入且不重复；未知区域或重复决定抛 ValueError。"""
        ids = [d.region_id for d in self.decisions]
        allowed = (info.context or {}).get("region_ids", [])
        if len(ids) != len(set(ids)) or not set(ids) <= set(allowed):
            raise ValueError("region IDs must be unique and drawn from region_ids")
        return self
