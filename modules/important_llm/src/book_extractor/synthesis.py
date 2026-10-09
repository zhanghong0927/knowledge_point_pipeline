"""确认单书候选分组、综合释义，并执行选择性原文重核。

名称和别名只提出比较对象，不证明同义；无法确认或执行失败的候选保留给后续处理。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, TypeVar

from instructor.core.exceptions import IncompleteOutputException
from openai import APIStatusError
from pydantic import BaseModel

from .llm import (
    DEFAULT_ATTEMPTS,
    ContextBudgetError,
    ExtractionCancelledError,
    LLMClient,
    fatal_service_error,
)
from .markdown import Unit, token_count
from .models import (
    Candidate,
    Finding,
    Grouping,
    IndependentSynthesis,
    Record,
    SynthesisReview,
    is_english_source,
)
from .prompts import (
    GROUP_PROMPT,
    INDEPENDENT_SYNTHESIS_PROMPT,
    SOURCE_ENGLISH_SUFFIX,
    SYNTHESIS_PROMPT,
    VERIFICATION_PROMPT,
)
from .rule_candidates import strip_structural_prefix
from .storage import replace_checkpoint
from .telemetry import TelemetryWriteError

T = TypeVar("T", bound=BaseModel)
PAYLOAD_TOKENS = 10500
GROUP_SIZE = 12
HIGH_CONFIDENCE_SAMPLE_PERCENT = 10
INDEPENDENT_REPAIR_PROMPT = """
【逐项修复】
本次仅包含尚未通过校验的候选。逐项重新核对自己的来源、语言、别名及名称资格；
严格返回全部输入编号各一次，不引用未提供或其他候选的来源，不补造别名。
已通过的候选由程序保存，无须重复生成。
"""
MEMBER_REPAIR_HINTS = {
    "definition_support_conflict": (
        "先判断来源能否支持实质释义；不足时definition=null且conditions=[]，"
        "缺失说明放issues。"
    ),
    "source_language_changed": "名称、释义、别名和条件保持引用原文语言，不译成中文。",
    "source_alias_not_found": "删去无原文依据的别名；没有明确同义称呼时用空数组。",
    "evidence_id_not_allowed": "只选本候选提供的来源，不借用其他候选的来源。",
    "missing": "补齐指定位置的必填字段；无释义证据时definition可为null。",
    "missing_candidate": "该候选未返回，必须按其编号补回一次。",
    "extra_forbidden": "删除Schema未定义的字段，不补draft_ids等由脚本负责的字段。",
    "invalid_control_character": "LaTeX反斜杠按JSON正确转义，不输出控制字符。",
    "list_type": "该字段必须为JSON数组，没有内容时用空数组。",
    "independent_finding": "reject的finding必须为null；accept/uncertain必须有finding。",
}


def _candidate_ids(value: Any, mapping: dict[str, str], field: str = "") -> Any:
    """仅改写协议中的候选编号；原文、名称、释义及来源单元编号保持逐字不变。

    value为载荷、校验上下文或响应，mapping为原编号与短编号的双向映射之一。
    返回新对象，不修改调用者数据；未知编号保持原样，由正常校验拒绝。
    """
    identity_fields = {
        "candidate_id",
        "candidate_ids",
        "draft_ids",
        "candidate_groups",
        "comparison_buckets",
    }
    lookup_fields = {
        "candidate_evidence_ids",
        "candidate_body_ids",
        "candidate_body_texts",
        "candidate_name_texts",
    }
    if isinstance(value, dict):
        return {
            mapping.get(key, key) if field in lookup_fields else key: _candidate_ids(
                child, mapping, key
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_candidate_ids(child, mapping, field) for child in value]
    if isinstance(value, str) and field in identity_fields:
        return mapping.get(value, value)
    return value


INHERITED_ISSUE_CODES = frozenset(
    {
        "name_uncertain",
        "name_unreviewed",
        "context_incomplete",
        "context_extension_failed",
        "source_unit_split",
        "rule_hits_truncated",
        "rule_context_truncated",
        "rule_heading_context_truncated",
        "rule_large_table_row_only_or_unsupported",
        "rule_evidence_excluded_by_preprocess",
        "insufficient_body_evidence",
        "source_formula_uncertain",
        "grouping_uncertain",
        "cross_batch_grouping_deferred",
        "synthesis_context_oversized",
        "grouping_failed",
        "synthesis_failed",
        "verification_failed",
    }
)


def _json(value: Any) -> str:
    """把 value 序列化为稳定的 Unicode JSON，用于提示输入和缓存身份。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cache_key(
    client: LLMClient,
    model: type[T],
    prompt: str,
    payload: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """返回原生响应缓存身份；审计与实际读写复用同一算法。"""
    if model is IndependentSynthesis and context.get("independent_partial"):
        # 重试序号和上一轮错误只指导修复，不改变候选及校验约束。
        # 同一剩余成员最终通过后，恢复应直接复用成功结果，不能重走未缓存的失败轮。
        prompt = prompt.replace(INDEPENDENT_REPAIR_PROMPT, "")
        payload = {"candidates": payload["candidates"]}
    identity = {
        "version": 1,
        "model": client.model,
        "schema": model.model_json_schema(),
        "prompt": prompt,
        "payload": payload,
        "context": context,
        "client": getattr(client, "identity", {"model": client.model}),
        "max_output_tokens": getattr(client, "max_output_tokens", None),
    }
    return hashlib.sha256(_json(identity).encode()).hexdigest()


def _cached_call(
    client: LLMClient,
    model: type[T],
    prompt: str,
    payload: dict[str, Any],
    context: dict[str, Any],
    directory: Path,
    *,
    attempts: int | None = None,
) -> T:
    """按模型、Schema、提示、payload 与 context 查缓存或调用 client。

    缓存和新响应均通过上下文校验，成功后原子持久化；校验、服务和存储错误交给调用方处理。
    attempts可缩小本次普通请求额度。独立综合缓存同时保存已验证成员和脱敏错误，
    返回对象的_ordinary_attempts仅统计本次新增HTTP，命中缓存时为零。
    """
    if model in {Finding, SynthesisReview, IndependentSynthesis} and is_english_source(
        "\n".join(context.get("evidence_texts", {}).values())
    ):
        # 实测开头的英文声明无效，末尾字段级约束改善了同源短批的输出语言。
        # 仅使用保守英语判据；不能因来源是其他拉丁文字就要求译成英语。
        prompt += SOURCE_ENGLISH_SUFFIX
    digest = _cache_key(client, model, prompt, payload, context)
    path = directory / f"{model.__name__.lower()}-{digest}.json"
    if model is Finding:
        stage = "verification"
    elif model is Grouping:
        stage = "grouping"
    else:
        stage = "synthesis"
    scope = (
        client.scope(stage=stage, work_id=digest)
        if hasattr(client, "scope")
        else nullcontext()
    )
    with scope:
        # ready仅在响应完整写入并fsync后发布；恢复不重发已成功的模型请求。
        # 每次使用独立文件名，并发相同请求不会读取仍在写入的临时文件。
        cached = (
            path
            if path.exists()
            else next(directory.glob(f"{path.name}.*.ready"), None)
        )
        if cached is not None:
            try:
                stored = cached.read_text(encoding="utf-8")
            except FileNotFoundError:
                # 另一线程可能刚完成ready提升，正式缓存仍须重新校验。
                stored = path.read_text(encoding="utf-8")
            if model is IndependentSynthesis and context.get("independent_partial"):
                saved = json.loads(stored)
                response = model.model_validate(saved["response"], context=context)
                response._member_errors = saved["member_errors"]
            else:
                response = model.model_validate_json(stored, context=context)
            if not (
                isinstance(response, IndependentSynthesis)
                and context.get("independent_partial")
                and not response.items
            ):
                if cached != path:
                    try:
                        replace_checkpoint(cached, path)
                    except FileNotFoundError:
                        if not path.exists() or cached.exists():
                            raise
                if hasattr(client, "event"):
                    client.event("cache_hit", cache=model.__name__)
                return response
        if hasattr(client, "event"):
            client.event("cache_miss", cache=model.__name__)
        # 只改变传输组织，不改变语义请求和成功缓存。模型先按短编号执行同一套校验，
        # 再由脚本还原编号并重新验证，避免长ID的生成成本和跨候选引用错误。
        mapping = {}
        if model in {Grouping, SynthesisReview}:
            identifiers = context.get("candidate_ids", context.get("draft_ids", []))
            mapping = {key: f"c{index}" for index, key in enumerate(identifiers, 1)}
        wire_payload = _candidate_ids(payload, mapping)
        wire_context = _candidate_ids(context, mapping)
        wire_prompt = prompt
        response = client.call(
            model,
            [
                {"role": "system", "content": wire_prompt},
                {"role": "user", "content": _json(wire_payload)},
            ],
            context=wire_context,
            **(
                {"attempts": attempts}
                if attempts is not None
                and hasattr(client, "last_call_ordinary_attempts")
                else {}
            ),
        )
        if mapping:
            response = model.model_validate(response.model_dump(), context=wire_context)
            response = model.model_validate(
                _candidate_ids(
                    response.model_dump(), {v: k for k, v in mapping.items()}
                ),
                context=context,
            )
    # Revalidate even fake/custom clients; a successful HTTP response alone is not
    # sufficient to accept invented references or an incomplete partition.
    member_errors = getattr(response, "_member_errors", {})
    response = model.model_validate(response.model_dump(), context=context)
    if isinstance(response, IndependentSynthesis) and context.get(
        "independent_partial"
    ):
        response._member_errors.update(member_errors)
        response._ordinary_attempts = getattr(client, "last_call_ordinary_attempts", 1)
        if not response.items:
            # 没有任何已验证成果，不发布成功检查点；下次恢复仍允许重试这些成员。
            return response
        saved = {
            "response": response.model_dump(),
            "member_errors": response._member_errors,
        }
        serialized = json.dumps(saved, ensure_ascii=False, indent=2)
    else:
        serialized = response.model_dump_json(indent=2)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=path.name + ".", suffix=".tmp"
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    # Exhausted storage retries preserve the validated response for diagnosis.
    ready = Path(temporary).with_suffix(".ready")
    os.replace(temporary, ready)
    try:
        replace_checkpoint(ready, path)
    except FileNotFoundError:
        # 相同请求的另一线程可能已将完整ready提升为正式缓存。
        if not path.exists() or ready.exists():
            raise
    return response


def _buckets(candidates: list[Candidate]) -> list[list[Candidate]]:
    """按候选名称或显式别名提出比较桶，返回不丢成员的分组列表。

    比较键忽略首尾空白和大小写，显示名称及别名不改写。
    只扩展直接同名线索，不递归追踪别名链，也不把词面相同当作语义相同。
    """
    comparison_names = {
        item.candidate_id: [
            item.name,
            *item.aliases,
        ]
        for item in candidates
    }
    index: dict[str, list[int]] = {}
    for position, candidate in enumerate(candidates):
        for name in set(comparison_names[candidate.candidate_id]):
            if name.strip():
                index.setdefault(name.strip().casefold(), []).append(position)
    pending = set(range(len(candidates)))
    result: list[list[Candidate]] = []
    for position, candidate in enumerate(candidates):
        if position not in pending:
            continue
        members = {position}
        # Pop each posting once: repeated common aliases cannot trigger an N²
        # scan. No recursive expansion through aliases of newly found members.
        for name in comparison_names[candidate.candidate_id]:
            members.update(index.pop(name.strip().casefold(), []))
        selected = sorted(members & pending)
        pending.difference_update(selected)
        result.append([candidates[item] for item in selected])
    return result


def _payload(
    members: list[Candidate],
    source: dict[str, Unit],
) -> dict[str, Any]:
    """回取精确原文区间并标记所属候选；共享区间去重，不混成无归属的整单元。"""
    body_parts: dict[tuple[str, int, int], dict[str, Any]] = {}
    name_parts: dict[tuple[str, int, int], dict[str, Any]] = {}

    def include(target: dict, key: str, start: int, end: int, owner: str) -> None:
        """同一区间只发送一次，同时保留全部合法成员，不改变原始偏移。"""
        part = target.setdefault(
            (key, start, end),
            {
                "id": key,
                "start": start,
                "end": end,
                "text": source[key].text[start:end],
                "headings": source[key].headings,
                "candidate_ids": [],
            },
        )
        if owner not in part["candidate_ids"]:
            part["candidate_ids"].append(owner)

    for item in members:
        texts = {
            key: source[key].text
            for key in set(item.evidence_ids) | set(item.name_evidence_ids)
            if key in source
        }
        Candidate.model_validate(
            item.model_dump(),
            context={
                "evidence_ids": texts.keys(),
                "evidence_texts": texts,
            },
        )
        spans = [(span.unit_id, span.start, span.end) for span in item.evidence_spans]
        if not spans:
            spans = [(key, 0, len(source[key].text)) for key in item.evidence_ids]
        for key, start, end in spans:
            include(body_parts, key, start, end, item.candidate_id)
        for key in item.name_evidence_ids:
            own = [(start, end) for unit_id, start, end in spans if unit_id == key]
            for start, end in own or [(0, len(source[key].text))]:
                body = body_parts.get((key, start, end))
                # 同成员同区间的正文已证明名称出处，不再重复发送整段原文。
                # 另一成员即使拥有相同区间，也不能替当前成员供证。
                if body is None or item.candidate_id not in body["candidate_ids"]:
                    include(name_parts, key, start, end, item.candidate_id)
    payload = {
        "candidates": [
            item.model_dump(exclude={"chunk_id", "evidence_spans"}) for item in members
        ],
        "sources": list(body_parts.values()),
        "name_sources": list(name_parts.values()),
    }
    return payload


def _member_texts(parts: list[dict[str, Any]], member_ids: set[str]) -> dict[str, str]:
    """只合并指定成员拥有的来源区间，保持原单元ID并去重相同区间。"""
    text: dict[str, list[str]] = {}
    seen: set[tuple[str, int, int]] = set()
    for part in parts:
        span = (part["id"], part["start"], part["end"])
        if member_ids.intersection(part["candidate_ids"]) and span not in seen:
            seen.add(span)
            text.setdefault(part["id"], []).append(part["text"])
    return {key: "\n".join(pieces) for key, pieces in text.items()}


def _current_issues(finding: Finding, previous: list[str]) -> list[str]:
    """保留新 finding 的问题及 previous 中已知运行、来源风险码，返回去重列表。

    旧自由文本仍存于 candidate/checkpoint；当前语义问题以新 Finding 为准。
    单例及失败回退的 Finding 来自原候选，因此其原问题不会被丢弃。
    非空释义沿用既有规则移除 definition_missing，空释义由模型校验补标。
    """
    issues = list(
        dict.fromkeys(
            [
                *finding.issues,
                *(
                    issue
                    for issue in previous
                    if issue.split(":", 1)[0].strip() in INHERITED_ISSUE_CODES
                ),
            ]
        )
    )
    if finding.definition is not None:
        issues = [
            issue
            for issue in issues
            if issue.split(":", 1)[0].strip() != "definition_missing"
        ]
    return issues


def _record(
    finding: Finding,
    members: list[Candidate],
    book_id: str,
    run_id: str,
    issues: list[str],
) -> Record:
    """用 finding 构造最终记录，并保留 members 的身份、章节和处理状态。

    不从旧草稿恢复被复核移除的条件、别名或引用；记录 ID 由本次运行和候选成员确定。
    """
    data = finding.model_dump()
    conditions = list(dict.fromkeys(finding.conditions))
    definition = finding.definition
    aliases = list(dict.fromkeys(finding.aliases))
    combined_issues = _current_issues(
        finding, [*issues, *(i for member in members for i in member.issues)]
    )
    data.update(
        name=strip_structural_prefix(finding.name),
        definition=definition,
        conditions=conditions,
        aliases=aliases,
        # Raw member references remain in candidates. Reintroducing them
        # here would undo source review that deliberately removed a bad link.
        evidence_ids=list(dict.fromkeys(finding.evidence_ids)),
        issues=combined_issues,
    )
    ids = [member.candidate_id for member in members]
    digest = hashlib.sha256(_json([book_id, run_id, sorted(ids)]).encode()).hexdigest()[
        :20
    ]
    return Record(
        **data,
        record_id=f"r{digest}",
        book_id=book_id,
        run_id=run_id,
        candidate_ids=ids,
        scope=list(dict.fromkeys(s for item in members for s in item.scope)),
    )


def _verify_records(
    records: list[Record],
    source: dict[str, Unit],
    client: LLMClient,
    directory: Path,
    *,
    candidate_lookup: dict[str, Candidate] | None = None,
    workers: int = 1,
    scope: dict[str, Any] | None = None,
) -> tuple[list[Record], list[dict[str, Any]]]:
    """有界滚动执行独立记录的原文核验，按输入顺序返回；致命错误阻止新补位。

    默认单线程保留直接调用者上下文；并行时由scope显式传入执行日志归属。
    普通单条失败仍保留对应记录，其余记录及各自缓存继续独立完成。
    """
    if workers < 1:
        raise ValueError("verification workers must be positive")
    if workers == 1 or len(records) < 2:
        return _verify_records_serial(
            records, source, client, directory, candidate_lookup=candidate_lookup
        )
    stopped = Event()

    def process(record: Record) -> tuple[list[Record], list[dict[str, Any]]]:
        """绑定当前记录的线程上下文，保留置信度选择和失败处理。"""
        if stopped.is_set():
            return [], []
        attribution = (
            client.scope(**scope)
            if scope is not None and hasattr(client, "scope")
            else nullcontext()
        )
        try:
            with attribution:
                return _verify_records_serial(
                    [record],
                    source,
                    client,
                    directory,
                    candidate_lookup=candidate_lookup,
                )
        except BaseException:
            stopped.set()
            if hasattr(client, "cancel"):
                client.cancel()
            raise

    completed: dict[int, tuple[list[Record], list[dict[str, Any]]]] = {}
    remaining = iter(enumerate(records))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for _ in range(min(workers, len(records))):
            index, record = next(remaining)
            pending[pool.submit(process, record)] = index
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                completed[pending.pop(future)] = future.result()
            while len(pending) < workers and not stopped.is_set():
                item = next(remaining, None)
                if item is None:
                    break
                index, record = item
                pending[pool.submit(process, record)] = index
    verified = []
    failures = []
    for index in range(len(records)):
        found, errors = completed[index]
        verified.extend(found)
        failures.extend(errors)
    return verified, failures


def verification_reason(record: Record) -> str | None:
    """返回置信度复核或高分抽检原因；不选中时返回None。

    抽样使用书籍和候选身份的SHA256，不随run_id、调度或进程随机种子变化。
    百分比为稳定哈希抽样的期望比例，小批次不保证恰好10%；不评判正文关键词。
    """
    if record.definition is None:
        return None
    if record.confidence != "high":
        return "confidence_" + (record.confidence or "unknown")
    identity = _json([record.book_id, sorted(record.candidate_ids)])
    rank = int(hashlib.sha256(identity.encode()).hexdigest(), 16) % 100
    return "high_confidence_sample" if rank < HIGH_CONFIDENCE_SAMPLE_PERCENT else None


def _verify_records_serial(
    records: list[Record],
    source: dict[str, Unit],
    client: LLMClient,
    directory: Path,
    *,
    candidate_lookup: dict[str, Candidate] | None = None,
) -> tuple[list[Record], list[dict[str, Any]]]:
    """复核中低分并抽检高分非空释义，返回记录及失败列表。

    模型仅接收名称与来源，不读取旧释义；硬校验始终执行，不受置信度影响。
    每条最多独立复核一轮，低置信度结果继续保留其评价，不循环求高分。
    普通失败保留记录并报告partial，服务拒绝和账本失败向上传播。
    """
    verified: list[Record] = []
    failures: list[dict[str, Any]] = []
    for record in records:
        if record.definition is None:
            verified.append(record)
            continue
        record_source = source
        if candidate_lookup is not None:
            parts = _payload(
                [candidate_lookup[key] for key in record.candidate_ids], source
            )["sources"]
            record_source = {
                key: replace(source[key], text=text)
                for key, text in _member_texts(parts, set(record.candidate_ids)).items()
            }
        reason = verification_reason(record)
        if reason is None:
            verified.append(record)
            continue
        reasons = [reason]
        payload = {
            "name": record.name,
            "scope": record.scope,
            "sources": [
                {
                    "id": key,
                    "text": record_source[key].text,
                    "headings": record_source[key].headings,
                }
                for key in record.evidence_ids
            ],
        }
        scope = (
            client.scope(stage="verification", work_id=record.record_id)
            if hasattr(client, "scope")
            else nullcontext()
        )
        started = monotonic()
        status = "failed"
        with scope:
            if hasattr(client, "event"):
                client.event(
                    "verification_selected",
                    record_id=record.record_id,
                    confidence=record.confidence,
                    definition_supported=record.definition_supported,
                    selection_reasons=reasons,
                )
                client.event(
                    "stage_started", stage="verification", work_id=record.record_id
                )
            try:
                finding = _cached_call(
                    client,
                    Finding,
                    VERIFICATION_PROMPT,
                    payload,
                    {
                        "expected_name": record.name,
                        "evidence_ids": record.evidence_ids,
                        "evidence_texts": {
                            part["id"]: part["text"] for part in payload["sources"]
                        },
                    },
                    directory,
                )
                # 线索只决定是否送审；不通过正则修改释义，也不把先前生成的
                # 定义、条件、别名作为模型输入。运行与成员身份始终由程序保留。
                data = {**record.model_dump(), **finding.model_dump()}
                data["issues"] = _current_issues(finding, record.issues)
                revised = Record.model_validate(data)
                verified.append(revised)
                status = "complete"
                if hasattr(client, "event"):
                    client.event(
                        "verification_result",
                        record_id=record.record_id,
                        changed=revised.model_dump() != record.model_dump(),
                        definition_changed=revised.definition != record.definition,
                        definition_removed=record.definition is not None
                        and revised.definition is None,
                        confidence=revised.confidence,
                        definition_supported=revised.definition_supported,
                        status=status,
                    )
            except (TelemetryWriteError, ExtractionCancelledError, OSError):
                raise
            except Exception as error:
                if fatal_service_error(error):
                    raise
                issue = f"verification_failed:{type(error).__name__}"
                verified.append(
                    record.model_copy(
                        update={"issues": list(dict.fromkeys([*record.issues, issue]))}
                    )
                )
                failures.append(
                    {
                        "stage": "verification",
                        "severity": "error",
                        "issue": issue,
                        "record_id": record.record_id,
                        "candidate_ids": record.candidate_ids,
                    }
                )
                if hasattr(client, "event"):
                    client.event(
                        "verification_result",
                        record_id=record.record_id,
                        status="partial",
                        error_type=type(error).__name__,
                    )
            finally:
                if hasattr(client, "event"):
                    client.event(
                        "stage_finished",
                        stage="verification",
                        work_id=record.record_id,
                        status=status,
                        elapsed_seconds=monotonic() - started,
                    )
    return verified, failures


def _synthesize_batch(
    batch: list[Candidate],
    split: bool,
    source: dict[str, Unit],
    client: LLMClient,
    book_id: str,
    run_id: str,
    checkpoint_dir: Path,
    stopped: Event,
    comparison_buckets: list[list[str]] | None = None,
) -> tuple[list[Record], list[dict]]:
    """用真实证据分组，再合批填写所有成员释义；失败保留 null 记录并登记错误。"""
    records: list[Record] = []
    limitations: list[dict] = []

    def failed(
        issue: str, members: list[Candidate] | None = None
    ) -> tuple[list[Record], list[dict]]:
        """不伪造释义；每个未完成成员留下一条可追溯的空定义和失败状态。"""
        unresolved = batch if members is None else members
        for member in unresolved:
            limitations.append(
                {
                    "stage": "name_decision",
                    "severity": "limitation",
                    "issue": "name_decision",
                    "candidate_id": member.candidate_id,
                    "name": member.name,
                    "decision": "unreviewed",
                    "reason": issue,
                    "failed": True,
                    "group_candidate_ids": [member.candidate_id],
                    "grouping_cache_key": None,
                    "evidence_ids": list(
                        dict.fromkeys([*member.evidence_ids, *member.name_evidence_ids])
                    ),
                }
            )
            finding = Finding(
                support_reason="生成未完成，无法交付有依据的释义。",
                definition_supported=False,
                confidence="low",
                name=member.name,
                aliases=member.aliases,
                category="待确认",
                definition=None,
                conditions=[],
                issues=[*member.issues, issue, "name_unreviewed"],
                evidence_ids=member.evidence_ids or member.name_evidence_ids,
            )
            records.append(_record(finding, [member], book_id, run_id, [issue]))
        limitations.append(
            {
                "stage": "synthesis",
                "severity": "error",
                "issue": issue,
                "candidate_ids": [c.candidate_id for c in unresolved],
            }
        )
        return records, limitations

    payload = _payload(batch, source)
    if stopped.is_set():
        return [], []
    groups = [[member.candidate_id] for member in batch]
    uncertain: set[str] = set()
    grouping_cache_key = None
    boundaries = comparison_buckets or [
        [member.candidate_id for member in bucket] for bucket in _buckets(batch)
    ]

    def budget_fallback(
        stage: str, reason: str = "ContextBudgetError"
    ) -> tuple[list[Record], list[dict]]:
        """请求超预算或输出截断后缩小批次；优先保留完整桶，不裁原文。

        每个子批严格小于父批，单候选失败即停止；跨子批的比较标为未完成。
        """
        if len(batch) < 2:
            return failed(f"{stage}_failed:{reason}")
        middle = len(batch) // 2
        if len(boundaries) > 1:
            # 合批边界已经可信，不必因一个长响应拆散原始同主体比较桶。
            left_ids = set().union(*boundaries[: len(boundaries) // 2])
        else:
            left_ids = {member.candidate_id for member in batch[:middle]}
        crosses = any(
            set(bucket) & left_ids and set(bucket) - left_ids for bucket in boundaries
        )
        found, errors = [], []
        children = (
            [member for member in batch if member.candidate_id in left_ids],
            [member for member in batch if member.candidate_id not in left_ids],
        )
        for child in children:
            child_ids = {member.candidate_id for member in child}
            child_boundaries = [
                [key for key in bucket if key in child_ids] for bucket in boundaries
            ]
            rows, issues = _synthesize_batch(
                child,
                split or crosses,
                source,
                client,
                book_id,
                run_id,
                checkpoint_dir,
                stopped,
                comparison_buckets=[bucket for bucket in child_boundaries if bucket],
            )
            found.extend(rows)
            errors.extend(issues)
        if crosses:
            errors.append(
                {
                    "stage": "synthesis",
                    "severity": "error",
                    "issue": "cross_batch_grouping_deferred",
                    "candidate_ids": [c.candidate_id for c in batch],
                }
            )
        return found, errors

    if any(len(bucket) > 1 for bucket in boundaries):
        try:
            grouping_payload = {**payload, "comparison_buckets": boundaries}
            grouping_context = {
                "candidate_ids": [c.candidate_id for c in batch],
                "comparison_buckets": boundaries,
            }
            grouping_cache_key = _cache_key(
                client, Grouping, GROUP_PROMPT, grouping_payload, grouping_context
            )
            grouping = _cached_call(
                client,
                Grouping,
                GROUP_PROMPT,
                grouping_payload,
                grouping_context,
                checkpoint_dir,
            )
            groups = [group.candidate_ids for group in grouping.groups]
            uncertain = {
                key
                for group in grouping.groups
                if group.status == "uncertain"
                for key in group.candidate_ids
            }
        except ContextBudgetError:
            return budget_fallback("grouping")
        except (TelemetryWriteError, ExtractionCancelledError, OSError):
            raise
        except APIStatusError as error:
            if fatal_service_error(error):
                raise
            return failed(f"grouping_failed:{type(error).__name__}")
        except Exception as error:
            return failed(f"grouping_failed:{type(error).__name__}")
    if stopped.is_set():
        return [], []
    # 分组只限制可合并成员；一批不同单例也共同生成，避免每名称一个请求。
    payload["candidate_groups"] = groups
    parts = [*payload["name_sources"], *payload["sources"]]
    evidence_ids = list(dict.fromkeys(part["id"] for part in parts))
    context = {
        "draft_ids": [c.candidate_id for c in batch],
        "candidate_groups": groups,
        "evidence_ids": evidence_ids,
        "evidence_texts": {
            key: "\n".join(p["text"] for p in parts if p["id"] == key)
            for key in evidence_ids
        },
        "candidate_evidence_ids": {
            c.candidate_id: [*c.evidence_ids, *c.name_evidence_ids] for c in batch
        },
        "candidate_body_ids": {c.candidate_id: c.evidence_ids for c in batch},
        "candidate_body_texts": {
            c.candidate_id: _member_texts(payload["sources"], {c.candidate_id})
            for c in batch
        },
        "candidate_name_texts": {
            c.candidate_id: _member_texts(payload["name_sources"], {c.candidate_id})
            for c in batch
        },
    }
    try:
        if all(len(group) == 1 for group in groups):
            # 独立成员的身份归属已确定，模型只回传一次编号；脚本补齐审计和draft_ids。
            independent = []
            mapping = {}
            for index, member in enumerate(batch, 1):
                key = f"c{index}"
                mapping[key] = member.candidate_id
                owned = _payload([member], source)
                independent.append(
                    {
                        "candidate_id": key,
                        "name": member.name,
                        "aliases": member.aliases,
                        "scope": member.scope,
                        "issues": member.issues,
                        "sources": [
                            {k: v for k, v in part.items() if k != "candidate_ids"}
                            for part in owned["sources"]
                        ],
                        "name_sources": [
                            {k: v for k, v in part.items() if k != "candidate_ids"}
                            for part in owned["name_sources"]
                        ],
                    }
                )
            context["independent_ids"] = mapping
            context["independent_names"] = {
                member.candidate_id: member.name for member in batch
            }
            context["independent_partial"] = True
            accepted = {}
            pending = independent
            remaining_attempts = getattr(client, "attempts", DEFAULT_ATTEMPTS)
            feedback = {}
            attempt = 0
            while remaining_attempts > 0:
                selected = IndependentSynthesis.select_context(
                    context, [row["candidate_id"] for row in pending]
                )
                prompt = INDEPENDENT_SYNTHESIS_PROMPT
                if attempt:
                    prompt += INDEPENDENT_REPAIR_PROMPT
                try:
                    answer = _cached_call(
                        client,
                        IndependentSynthesis,
                        prompt,
                        {
                            "candidates": pending,
                            "repair_round": attempt,
                            "validation_errors": feedback,
                            "repair_instructions": {
                                code: MEMBER_REPAIR_HINTS[code]
                                for code in {
                                    error["type"]
                                    for errors in feedback.values()
                                    for error in errors
                                }
                                if code in MEMBER_REPAIR_HINTS
                            },
                        },
                        selected,
                        checkpoint_dir,
                        attempts=remaining_attempts,
                    )
                except (ContextBudgetError, IncompleteOutputException):
                    if not accepted:
                        raise
                    # 成功项已落盘；仅将剩余独立成员交回现有预算拆分路径。
                    unresolved_ids = set(selected["draft_ids"])
                    rows, errors = _synthesize_batch(
                        [c for c in batch if c.candidate_id in unresolved_ids],
                        split,
                        source,
                        client,
                        book_id,
                        run_id,
                        checkpoint_dir,
                        stopped,
                        comparison_buckets=[
                            [c.candidate_id]
                            for c in batch
                            if c.candidate_id in unresolved_ids
                        ],
                    )
                    records.extend(rows)
                    limitations.extend(errors)
                    pending = []
                    break
                except (TelemetryWriteError, ExtractionCancelledError, OSError):
                    raise
                except Exception as error:
                    if fatal_service_error(error):
                        raise
                    # 网络/外层格式错误仍由客户端执行原有有界重试；最终失败不丢成功项。
                    unresolved_ids = set(selected["draft_ids"])
                    failed(
                        f"synthesis_failed:{type(error).__name__}",
                        [c for c in batch if c.candidate_id in unresolved_ids],
                    )
                    pending = []
                    break
                remaining_attempts -= answer._ordinary_attempts
                feedback = answer._member_errors
                accepted.update({item.candidate_id: item for item in answer.items})
                pending = [
                    row for row in pending if row["candidate_id"] not in accepted
                ]
                if hasattr(client, "event"):
                    client.event(
                        "synthesis_members_validated",
                        stage="synthesis",
                        repair_round=attempt,
                        accepted=len(answer.items),
                        remaining=len(pending),
                        consumed_attempts=answer._ordinary_attempts,
                        remaining_attempts=remaining_attempts,
                        error_counts=dict(
                            Counter(
                                error["type"]
                                for errors in feedback.values()
                                for error in errors
                            )
                        ),
                    )
                if not pending:
                    break
                attempt += 1
            if pending:
                unresolved_ids = {mapping[row["candidate_id"]] for row in pending}
                failed(
                    "synthesis_failed:MemberValidation",
                    [c for c in batch if c.candidate_id in unresolved_ids],
                )
            answer = IndependentSynthesis(
                items=[accepted[key] for key in mapping if key in accepted]
            )
            reviewed = answer.to_review(
                IndependentSynthesis.select_context(context, accepted)
            )
        else:
            reviewed = _cached_call(
                client,
                SynthesisReview,
                SYNTHESIS_PROMPT,
                payload,
                context,
                checkpoint_dir,
            )
    except ContextBudgetError:
        return budget_fallback("synthesis")
    except IncompleteOutputException:
        return budget_fallback("synthesis", "IncompleteOutputException")
    except (TelemetryWriteError, ExtractionCancelledError, OSError):
        raise
    except APIStatusError as error:
        if fatal_service_error(error):
            raise
        return failed(f"synthesis_failed:{type(error).__name__}")
    except Exception as error:
        return failed(f"synthesis_failed:{type(error).__name__}")
    lookup = {c.candidate_id: c for c in batch}
    for decision in reviewed.name_decisions:
        limitations.append(
            {
                "stage": "name_decision",
                "severity": "limitation",
                "issue": "name_decision",
                **decision.model_dump(),
                "name": lookup[decision.candidate_id].name,
                "failed": False,
                "group_candidate_ids": next(
                    list(group) for group in groups if decision.candidate_id in group
                ),
                "grouping_cache_key": grouping_cache_key,
            }
        )
    uncertain_names = {
        d.candidate_id for d in reviewed.name_decisions if d.decision == "uncertain"
    }
    for result in reviewed.findings:
        members = [lookup[key] for key in result.draft_ids]
        issues = []
        if set(result.draft_ids) & uncertain_names:
            issues.append("name_uncertain")
        if set(result.draft_ids) & uncertain:
            issues.append("grouping_uncertain")
            limitations.append(
                {
                    "stage": "synthesis",
                    "severity": "limitation",
                    "issue": "grouping_uncertain",
                    "candidate_ids": result.draft_ids,
                }
            )
        if split:
            issues.append("cross_batch_grouping_deferred")
        finding = Finding.model_validate(result.model_dump(exclude={"draft_ids"}))
        records.append(_record(finding, members, book_id, run_id, issues))
    order = {member.candidate_id: index for index, member in enumerate(batch)}
    records.sort(key=lambda record: min(order[key] for key in record.candidate_ids))
    return records, limitations


def synthesize(
    candidates: list[Candidate],
    units: list[Unit],
    client: LLMClient,
    book_id: str,
    run_id: str,
    checkpoint_dir: Path,
    *,
    workers: int = 1,
    scope: dict[str, Any] | None = None,
) -> tuple[list[Record], list[dict]]:
    """对本书 candidates 进行有界分组、综合及选择性重核，返回记录和限制/错误。

    units 提供完整原文，checkpoint_dir 保存原生缓存；
    单例每批最多12个；较大完整碰撞组只有在既有payload阈值内才整体提交。
    多个完整小碰撞桶可在同一预算内合批，分组响应不得跨原桶边界。
    workers 限制书内独立批次并发，默认 1；scope 显式传递线程统计归属。
    综合批次完成即排入逐条复核，与剩余综合共用 workers 个槽位；
    主线程仍按原批次及记录顺序合并，复核失败不阻塞其他批次。
    失败保留空定义及错误；单例合批生成释义，非法身份或来源区间抛出 ValueError。
    """
    if workers < 1:
        raise ValueError("workers must be positive")
    source = {unit.id: unit for unit in units}
    if len(source) != len(units) or len({c.candidate_id for c in candidates}) != len(
        candidates
    ):
        raise ValueError("Source and candidate IDs must be unique")
    for candidate in candidates:
        _payload([candidate], source)
    records: list[Record] = []
    limitations: list[dict] = []

    jobs: list[tuple[list[Candidate], bool, list[dict], list[list[str]]]] = []
    buckets = _buckets(candidates)
    singletons = [bucket[0] for bucket in buckets if len(bucket) == 1]
    batches_to_process = [bucket for bucket in buckets if len(bucket) > 1]
    if singletons:
        batches_to_process.append(singletons)
    for bucket in batches_to_process:
        # 保留完整身份比较：小组由SDK裁决，大组须先满足既有payload预算。
        whole_collision = any(len(group) > 1 for group in _buckets(bucket)) and (
            len(bucket) <= GROUP_SIZE
            or token_count(_json(_payload(bucket, source))) <= PAYLOAD_TOKENS
        )
        batches: list[list[Candidate]] = []
        batch: list[Candidate] = []
        for member in bucket:
            if (
                batch
                and not whole_collision
                and (
                    len(batch) >= GROUP_SIZE
                    or token_count(_json(_payload([*batch, member], source)))
                    > PAYLOAD_TOKENS
                )
            ):
                batches.append(batch)
                batch = []
            batch.append(member)
        if batch:
            batches.append(batch)
        split = len(batches) > 1 and any(len(group) > 1 for group in _buckets(bucket))
        prefix: list[dict] = []
        if split:
            prefix.append(
                {
                    "stage": "synthesis",
                    "severity": "error",
                    "issue": "cross_batch_grouping_deferred",
                    "candidate_ids": [c.candidate_id for c in bucket],
                }
            )
        for index, batch in enumerate(batches):
            boundaries = (
                [[member.candidate_id for member in batch]]
                if any(len(group) > 1 for group in _buckets(bucket))
                else [[member.candidate_id] for member in batch]
            )
            jobs.append((batch, split, prefix if index == 0 else [], boundaries))

    # 只合并完整且本来就能装下的小碰撞桶；大组、拆组与单例任务保持原路径。
    packed_jobs: list[tuple[list[Candidate], bool, list[dict], list[list[str]]]] = []
    pending_batch: list[Candidate] = []
    pending_boundaries: list[list[str]] = []
    for batch, split, prefix, boundaries in jobs:
        eligible = (
            not split
            and not prefix
            and len(boundaries) == 1
            and 1 < len(batch) <= GROUP_SIZE
            and token_count(
                _json(
                    {
                        **_payload(batch, source),
                        "comparison_buckets": boundaries,
                    }
                )
            )
            <= PAYLOAD_TOKENS
        )
        if pending_batch and (
            not eligible
            or len(pending_batch) + len(batch) > GROUP_SIZE
            or token_count(
                _json(
                    {
                        **_payload([*pending_batch, *batch], source),
                        "comparison_buckets": [*pending_boundaries, *boundaries],
                    }
                )
            )
            > PAYLOAD_TOKENS
        ):
            packed_jobs.append((pending_batch, False, [], pending_boundaries))
            pending_batch, pending_boundaries = [], []
        if eligible:
            pending_batch.extend(batch)
            pending_boundaries.extend(boundaries)
        else:
            packed_jobs.append((batch, split, prefix, boundaries))
    if pending_batch:
        packed_jobs.append((pending_batch, False, [], pending_boundaries))
    jobs = packed_jobs

    stopped = Event()
    candidate_lookup = {candidate.candidate_id: candidate for candidate in candidates}

    def verify(record: Record) -> tuple[list[Record], list[dict]]:
        """复核一条已综合记录，继承书籍归属并向共享调度器传播致命失败。"""
        if stopped.is_set():
            return [], []
        attribution = (
            client.scope(**scope)
            if scope is not None and hasattr(client, "scope")
            else nullcontext()
        )
        try:
            with attribution:
                return _verify_records_serial(
                    [record],
                    source,
                    client,
                    checkpoint_dir / "verification",
                    candidate_lookup=candidate_lookup,
                )
        except BaseException:
            stopped.set()
            if hasattr(client, "cancel"):
                client.cancel()
            raise

    def run_job(
        job: tuple[list[Candidate], bool, list[dict], list[list[str]]],
    ) -> tuple[list[Record], list[dict]]:
        """在线程中显式继承统计归属，处理局部结果并传播致命失败。"""
        if stopped.is_set():
            return [], []
        batch, split, prefix, boundaries = job
        attribution = (
            client.scope(**scope)
            if scope is not None and hasattr(client, "scope")
            else nullcontext()
        )
        try:
            with attribution:
                found, issues = _synthesize_batch(
                    batch,
                    split,
                    source,
                    client,
                    book_id,
                    run_id,
                    checkpoint_dir,
                    stopped,
                    comparison_buckets=boundaries,
                )
            return found, [*prefix, *issues]
        except BaseException:
            stopped.set()
            if hasattr(client, "cancel"):
                client.cancel()
            raise

    verification_errors: list[dict] = []
    # 单线程也逐批复核，保留调用者线程上下文；两种调度输出顺序一致。
    if workers == 1:
        for job in jobs:
            found, issues = run_job(job)
            for record in found:
                verified, errors = verify(record)
                records.extend(verified)
                verification_errors.extend(errors)
            limitations.extend(issues)
    else:
        completed: dict[int, tuple[list[Record], list[dict]]] = {}
        verified_rows: dict[tuple[int, int], tuple[list[Record], list[dict]]] = {}
        ready: deque[tuple[int, int, Record]] = deque()
        remaining = iter(enumerate(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            for _ in range(min(workers, len(jobs))):
                index, job = next(remaining)
                pending[pool.submit(run_job, job)] = (index, None)
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                # 先获取所有已完成结果，使致命失败在补位前传播。
                for future in finished:
                    index, position = pending.pop(future)
                    result = future.result()
                    if position is None:
                        completed[index] = result
                        ready.extend(
                            (index, number, record)
                            for number, record in enumerate(result[0])
                        )
                    else:
                        verified_rows[index, position] = result
                while len(pending) < workers and not stopped.is_set():
                    # 优先消化已产出的记录：只用一个线程池，避免每批再开复核池。
                    # 未提交的复核最多来自当前在途综合批次，不随全书批次数堆积。
                    if ready:
                        index, position, record = ready.popleft()
                        pending[pool.submit(verify, record)] = (index, position)
                        continue
                    item = next(remaining, None)
                    if item is None:
                        break
                    index, job = item
                    pending[pool.submit(run_job, job)] = (index, None)
        # 执行顺序可变，导出和限制记录仍严格按原批次顺序合并。
        for index in range(len(jobs)):
            found, issues = completed[index]
            for position in range(len(found)):
                verified, errors = verified_rows[index, position]
                records.extend(verified)
                verification_errors.extend(errors)
            limitations.extend(issues)
    # 同一对象仍可能存在非词面同义表述；没有跨书检索或无约束的全书两两比较。
    if len(candidates) > 1:
        limitations.append(
            {
                "stage": "synthesis",
                "severity": "limitation",
                "issue": "nonlexical_grouping_not_attempted",
                "candidate_count": len(candidates),
            }
        )
    if len(records) > 1 and any(c.aliases for c in candidates):
        limitations.append(
            {
                "stage": "synthesis",
                "severity": "limitation",
                "issue": "alias_bridge_recomparison_not_attempted",
            }
        )
    return records, [*limitations, *verification_errors]
