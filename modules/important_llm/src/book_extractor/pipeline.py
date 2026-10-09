"""编排单本 MD 提取、断点恢复和候选交付。

阶段结果写入独立运行目录；预算或可恢复失败返回 partial，服务拒绝与账本失败向上传播。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import socket
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from itertools import islice
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4

from instructor.core.exceptions import (
    IncompleteOutputException,
    InstructorRetryException,
)
from openai import APIStatusError
from pydantic import ValidationError

from .llm import (
    ContextBudgetError,
    ExtractionCancelledError,
    LLMClient,
    fatal_service_error,
)
from .markdown import Chunk, Unit, chunk_units, parse_markdown, token_count
from .models import Candidate, EvidenceSpan, NameDiscovery, Record, Regions
from .prompts import DISCOVER, REGION_PROMPT
from .rule_candidates import extract_rule_candidates, rule_only_units
from .storage import replace_checkpoint
from .synthesis import synthesize
from .telemetry import EventLog, TelemetryWriteError, summarize_events

IMPLEMENTATION_MODULES = (
    "llm",
    "markdown",
    "models",
    "pipeline",
    "prompts",
    "rule_candidates",
    "storage",
    "synthesis",
    "telemetry",
    "tokenization",
)

PROMPT_VERSION = "md-v3.2"
# 可调的执行预算统一放置；块正文与区域判断分别计量，避免联动修改。
DEFAULT_CHUNK_TOKENS = 4000
DEFAULT_CHUNK_WORKERS = 4
REGION_CONTEXT_TOKENS = 4000
DISCOVERY_NEIGHBOR_TOKENS = 512
MAX_STAGE_ATTEMPTS = 3
REGION_EDGE_DIVISOR = 20
REGION_SIGNAL_PATTERNS = (
    r"\bISBN\b|\bCIP\b|图书在版编目"
    r"|catalog(?:u)?ing.in.publication|library of congress",
    r"^\s*(?:出版社?|出版发行|印刷|发行)\s*[:：]"
    r"|^\s*[^\n。！？]{1,50}出版社\s*$|\b(?:published|printed|distributed) by\b",
    r"^\s*(?:责任编辑|定价|版次|印次|出版日期)\s*[:：]"
    r"|^\s*第[一二三四五六七八九十0-9]{1,5}版\s*$|\bcopyright\b|©|all rights reserved",
)


def write_json(path: Path, value: Any) -> None:
    """将 value 原子写入 path 指定的 UTF-8 JSON 检查点。

    创建父目录并在同卷替换临时文件；写入或替换失败向上传播，不暴露半份 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    replace_checkpoint(temporary, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """把 rows 完整序列化后替换 path；父目录由调用方准备。

    每条记录独占一行，替换失败保留异常供任务恢复，不能把临时结果当成功输出。
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    replace_checkpoint(temporary, path)


def digest(value: bytes) -> str:
    """计算原始字节 value 的 SHA-256 十六进制值，用于来源和运行身份校验。"""
    return hashlib.sha256(value).hexdigest()


def exhausted_validation(
    error: BaseException, client: LLMClient, attempt_limit: int
) -> bool:
    """判断 error 是否为当前调用预算耗尽后的验证失败。

    结合 client 的实际尝试数与 attempt_limit，只允许已耗尽的格式修复进入结构二分。
    """
    return (
        isinstance(error, InstructorRetryException)
        and isinstance(error.__cause__, ValidationError)
        and 0 < attempt_limit <= client.attempts
        and client.last_call_attempts >= attempt_limit
    )


def region_candidates(units: list[Unit]) -> list[Unit]:
    """从 units 首尾区域筛出待判断的出版信息或转换元数据单元。

    相邻非空单元中至少两类书目信号才能触发，目标自身也须包含信号；独立对象文本只提名、不直接删除。
    短版权页不设 token 下限；正文中的泛化出版词不当作书目字段。
    """
    result: list[Unit] = []
    nonblank = [unit for unit in units if unit.text.strip()]
    edge = max(1, len(nonblank) // REGION_EDGE_DIVISOR)
    for index, unit in enumerate(nonblank):
        if index >= edge and index < len(nonblank) - edge:
            continue
        if unit.kind in {"table", "code", "math"}:
            continue
        if unit.text.strip().startswith("{") and unit.text.strip().endswith("}"):
            result.append(unit)
            continue
        flags = re.I | re.M
        if not any(
            re.search(pattern, unit.text, flags) for pattern in REGION_SIGNAL_PATTERNS
        ):
            continue
        nearby = "\n".join(
            item.text for item in nonblank[max(0, index - 1) : index + 2]
        )
        signals = sum(
            bool(re.search(pattern, nearby, flags))
            for pattern in REGION_SIGNAL_PATTERNS
        )
        # 没有PDF页位置，不猜页眉页脚；重复短行、孤立数字都保留。
        if signals >= 2:
            result.append(unit)
    return result


@contextmanager
def stage_scope(
    client: LLMClient,
    log: EventLog,
    scope: dict[str, Any],
    name: str,
    work_id: str = "",
) -> Iterator[None]:
    """将本次 name/work_id 阶段绑定到客户端线程上下文并记录耗时。

    退出时始终写阶段完成事件；异常标为 failed 后原样抛出，不能吞掉账本写入失败。
    """
    started = monotonic()
    status = "complete"
    log.event("stage_started", stage=name, work_id=work_id)
    try:
        with client.scope(**scope, stage=name, work_id=work_id):
            yield
    except BaseException:
        status = "failed"
        raise
    finally:
        log.event(
            "stage_finished",
            stage=name,
            work_id=work_id,
            status=status,
            elapsed_seconds=monotonic() - started,
        )


def preprocess(
    units: list[Unit], client: LLMClient, directory: Path
) -> tuple[list[Unit], list[dict[str, Any]]]:
    """读取或生成稀疏区域决策列表，返回保留单元和已记录的判断。

    只对疑似出版区或转换元数据调用模型；缺失、超预算和普通失败保留原文，服务拒绝与账本失败抛出。
    regions.json 不包含所有单元；空列表表示没有已记录决策，不证明全文没有噪声。
    另写不含正文的 preprocess_summary.json，区分未触发、缓存恢复和未解决判断。
    """
    checkpoint = directory / "regions.json"
    cache_reused = checkpoint.exists()
    candidates = region_candidates(units)
    llm_calls = 0
    if cache_reused:
        rows = json.loads(checkpoint.read_text(encoding="utf-8"))
    else:
        rows = []
    retry_ids = {
        row["region_id"]
        for row in rows
        if row.get("decision_origin") == "runtime_error"
    }
    if not cache_reused or retry_ids:
        # 只恢复未执行成功的区域；模型已作出的 uncertain 和预算限制仍复用。
        rows = [row for row in rows if row["region_id"] not in retry_ids]
        positions = {unit.id: i for i, unit in enumerate(units)}
        for unit in candidates:
            if cache_reused and unit.id not in retry_ids:
                continue
            index = positions[unit.id]
            context = units[max(0, index - 2) : index + 3]
            if (
                token_count(
                    json.dumps([asdict(u) for u in context], ensure_ascii=False)
                )
                > REGION_CONTEXT_TOKENS
            ):
                rows.append(
                    {
                        "region_id": unit.id,
                        "action": "uncertain",
                        "reason": "region_context_oversized",
                        "decision_origin": "budget",
                    }
                )
                continue
            try:
                llm_calls += 1
                result = client.call(
                    Regions,
                    [
                        {"role": "system", "content": REGION_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "target": unit.id,
                                    "context": [asdict(u) for u in context],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    context={"region_ids": [unit.id]},
                )
                decision = next(
                    (
                        {**d.model_dump(), "decision_origin": "model"}
                        for d in result.decisions
                        if d.region_id == unit.id
                    ),
                    None,
                )
                rows.append(
                    decision
                    or {
                        "region_id": unit.id,
                        "action": "uncertain",
                        "reason": "omitted",
                        "decision_origin": "omitted",
                    }
                )
            except (TelemetryWriteError, ExtractionCancelledError, OSError):
                raise
            except APIStatusError as error:
                if fatal_service_error(error):
                    raise
                rows.append(
                    {
                        "region_id": unit.id,
                        "action": "uncertain",
                        "reason": type(error).__name__,
                        "decision_origin": "runtime_error",
                    }
                )
            except Exception as error:
                # 错误消息可能含服务请求片段，仅持久化异常类型，不写密钥或响应头。
                rows.append(
                    {
                        "region_id": unit.id,
                        "action": "uncertain",
                        "reason": type(error).__name__,
                        "decision_origin": "runtime_error",
                    }
                )
        write_json(checkpoint, rows)
    excluded = {row["region_id"] for row in rows if row["action"] == "exclude"}
    recorded = {row["region_id"] for row in rows}
    unrecorded = {unit.id for unit in candidates} - recorded
    uncertain = sum(row["action"] == "uncertain" for row in rows)
    write_json(
        directory / "preprocess_summary.json",
        {
            "decision_scope": "sparse_routed_regions; unlisted_units_retained",
            "cache_reused": cache_reused,
            "total_units": len(units),
            "nonblank_units": sum(bool(unit.text.strip()) for unit in units),
            "protected_units": sum(
                unit.kind in {"table", "code", "math"} for unit in units
            ),
            "current_candidates": len(candidates),
            "unrecorded_candidates": len(unrecorded),
            "recorded_decisions": len(rows),
            "excluded_units": len(excluded),
            "retained_decisions": sum(row["action"] == "retain" for row in rows),
            "uncertain_decisions": uncertain,
            "failed_decisions": sum(
                row.get("decision_origin") in {"budget", "omitted", "runtime_error"}
                for row in rows
            ),
            "default_retained_units": sum(unit.id not in recorded for unit in units),
            "llm_logical_calls_this_execution": llm_calls,
            "routing_status": "candidates_found" if candidates else "zero_candidates",
            "decision_status": "unresolved" if uncertain or unrecorded else "resolved",
        },
    )
    return [unit for unit in units if unit.id not in excluded], rows


def chunk_payloads(
    units: list[Unit], chunks: list[Chunk]
) -> list[list[dict[str, Any]]]:
    """将 chunks 的连续核心范围与 units 求交，返回每块的来源切片列表。

    不会按引用 ID 重新展开超长单元；切片无法精确回拼块正文时抛出 ValueError。
    """
    spans: list[tuple[int, int, Unit]] = []
    offset = 0
    for unit in units:
        spans.append((offset, offset + len(unit.text), unit))
        offset += len(unit.text)
    payloads = []
    cursor = 0
    unit_cursor = 0
    for chunk in chunks:
        end = cursor + len(chunk.text)
        while unit_cursor < len(spans) and spans[unit_cursor][1] <= cursor:
            unit_cursor += 1
        pieces = []
        for start, stop, unit in islice(spans, unit_cursor, None):
            if start >= end:
                break
            pieces.append(
                {
                    "id": unit.id,
                    "text": unit.text[max(cursor - start, 0) : min(end, stop) - start],
                    "start": max(cursor - start, 0),
                    "end": min(end, stop) - start,
                }
            )
        if "".join(piece["text"] for piece in pieces) != chunk.text:
            raise ValueError("Chunk/source intersection failed")
        payloads.append(pieces)
        cursor = end
    return payloads


def split_payload(core: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 core 的字符总长中点二分，返回两组仍携带原 evidence ID 的切片。

    不重写或丢弃字符；同一原单元可以在两个子块中出现。
    """
    midpoint = sum(len(part["text"]) for part in core) // 2
    children: list[list[dict[str, Any]]] = [[], []]
    cursor = 0
    for part in core:
        boundary = max(0, min(len(part["text"]), midpoint - cursor))
        for index, text in enumerate(
            (part["text"][:boundary], part["text"][boundary:])
        ):
            if text:
                piece = {"id": part["id"], "text": text}
                if "start" in part:
                    piece["start"] = part["start"] + (boundary if index else 0)
                    piece["end"] = piece["start"] + len(text)
                children[index].append(piece)
        cursor += len(part["text"])
    return children


def neighbor_payloads(
    pieces: list[list[dict[str, Any]]], index: int, budget: int
) -> list[dict[str, Any]]:
    """为 pieces[index] 取前后最近的源切片，总预算按两侧平分。

    返回带原 ID 的邻文，按字符边界缩短超限文本；没有可用邻块则返回空列表。
    """
    neighbors: list[dict[str, Any]] = []
    for position in (index - 1, index + 1):
        if not 0 <= position < len(pieces):
            continue
        remaining = budget // 2
        selected: list[dict[str, Any]] = []
        parts = (
            reversed(pieces[position]) if position < index else iter(pieces[position])
        )
        for part in parts:
            if not part["text"].strip():
                continue
            text = part["text"]
            while token_count(text) > remaining and text:
                text = (
                    text[(len(text) + 1) // 2 :]
                    if position < index
                    else text[: len(text) // 2]
                )
            if text.strip():
                piece = {"id": part["id"], "text": text}
                if "start" in part:
                    shift = len(part["text"]) - len(text) if position < index else 0
                    piece["start"] = part["start"] + shift
                    piece["end"] = piece["start"] + len(text)
                selected.append(piece)
                remaining -= token_count(text)
            if remaining <= 0:
                break
        neighbors.extend(reversed(selected) if position < index else selected)
    return neighbors


def source_context(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """从实际提供的原文切片构建引用校验上下文；相同单元的切片按顺序保留。"""
    ids = list(dict.fromkeys(part["id"] for part in parts))
    return {
        "evidence_ids": ids,
        "evidence_texts": {
            key: "\n".join(part["text"] for part in parts if part["id"] == key)
            for key in ids
        },
    }


def formula_context_ids(
    referenced: list[str],
    units: dict[str, Unit],
    full_read: set[str],
    following: dict[str, Unit],
) -> list[str]:
    """从实际已读单元补齐明确的公式引导、展示公式及紧随的符号解释。

    只跨空白，不能跨标题、排除或未读单元；仅加入完整单元，总增量沿用邻文预算。
    不搜索其他章节，不截断公式或解释，不新增模型请求。
    """
    added: list[str] = []
    remaining = DISCOVERY_NEIGHBOR_TOKENS
    for key in referenced:
        unit = units[key]
        if key not in full_read:
            continue
        formula = unit
        chain: list[Unit] = []
        if unit.kind != "math":
            if unit.kind != "paragraph" or not re.search(
                r"(?:条件(?:是|为)|如下|定义为|given by|defined as|as follows)"
                r"\s*[:：]\s*$",
                unit.text,
                re.I,
            ):
                continue
            formula = following.get(key)
            if formula is None or formula.kind != "math":
                continue
            chain.append(formula)
        explanation = following.get(formula.id)
        if (
            explanation is None
            or explanation.kind != "paragraph"
            or not re.match(r"\s*(?:其中|式中|where\b)", explanation.text, re.I)
        ):
            continue
        chain.append(explanation)
        if any(
            part.id not in full_read or part.headings != unit.headings for part in chain
        ):
            continue
        new = [
            part for part in chain if part.id not in referenced and part.id not in added
        ]
        cost = sum(token_count(part.text) for part in new)
        if cost <= remaining:
            added.extend(part.id for part in new)
            remaining -= cost
    return added


def make_candidates(
    result: NameDiscovery,
    identity: str,
    units: dict[str, Unit],
    parts: list[dict[str, Any]],
) -> list[Candidate]:
    """把名称发现转成证据候选，保留实际读过的引用及明确公式依赖切片。

    parts 必须来自本次 core 与实际发送的 neighbors；坐标相对完整 Unit.text。
    无坐标的独立调用仅接受唯一原文匹配，重复位置不猜测，抛出 ValueError。
    不生成释义、分类或条件；内容与坐标共同决定稳定候选 ID。
    """
    if not isinstance(result, NameDiscovery):
        raise TypeError("Candidates require name discovery results")
    full_read = {
        part["id"]
        for part in parts
        if part["id"] in units
        and part["text"] == units[part["id"]].text
        and part.get("start", 0) == 0
        and part.get("end", len(part["text"])) == len(part["text"])
    }
    nonblank = [unit for unit in units.values() if unit.text.strip()]
    following = {a.id: b for a, b in zip(nonblank, nonblank[1:])}
    candidates: dict[str, Candidate] = {}
    for finding in result.findings:
        added_ids = formula_context_ids(
            finding.evidence_ids, units, full_read, following
        )
        evidence_ids = [*finding.evidence_ids, *added_ids]
        spans: list[EvidenceSpan] = []
        for key in evidence_ids:
            matched = [part for part in parts if part["id"] == key]
            if not matched:
                raise ValueError("Referenced unit was not read")
            for part in matched:
                text = part["text"]
                original = units[key].text
                start = part.get("start")
                if start is None:
                    start = original.find(text)
                    if start < 0 or original.find(text, start + 1) >= 0:
                        raise ValueError(
                            "Source slice location is missing or ambiguous"
                        )
                end = part.get("end", start + len(text))
                span = EvidenceSpan(unit_id=key, start=start, end=end)
                if end > len(original) or original[start:end] != text:
                    raise ValueError("Source slice coordinates do not match text")
                if span not in spans:
                    spans.append(span)
        issues = ["context_incomplete"] if result.needs_context else []
        if added_ids:
            issues.append("formula_context_added:" + ",".join(added_ids))
        if any(
            span.start or span.end != len(units[span.unit_id].text) for span in spans
        ):
            issues.append("source_unit_split")
        data = {
            "name": finding.name,
            "evidence_ids": evidence_ids,
            "name_evidence_ids": finding.evidence_ids,
            "evidence_spans": [span.model_dump() for span in spans],
            "origins": ["body"],
            "issues": issues,
        }
        content_hash = digest(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        )[:20]
        candidate_id = f"{identity}-{content_hash}"
        candidates[candidate_id] = Candidate(
            **data,
            candidate_id=candidate_id,
            chunk_id=identity,
            scope=list(
                dict.fromkeys(
                    heading
                    for key in finding.evidence_ids
                    for heading in units[key].headings
                )
            ),
        )
    return list(candidates.values())


def validate_resume(
    output: Path,
    run_id: str,
    raw: bytes,
    config: dict[str, Any],
    compatible_implementations: tuple[str, ...] = (),
) -> tuple[Path, dict[str, Any]]:
    """校验原目录恢复身份，返回真实目录及原 manifest，不写入文件。

    raw 为本次原文件字节，config 为当前完整语义配置。仅允许白名单中的旧源码
    指纹变化，模型、提示词、分词器、来源和所有其他配置必须一致；路径越界、
    旧清单篡改或语义不兼容均抛 ValueError，不能通过恢复参数绕过新运行身份。
    """
    if re.fullmatch(r"[0-9a-f]{20}", run_id) is None:
        raise ValueError("Invalid resume run_id")
    directory = (output / run_id).resolve()
    if directory.parent != output.resolve():
        raise ValueError("Resume directory escapes output")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    previous = manifest.get("config")
    if not isinstance(previous, dict):
        raise ValueError("Resume manifest has no configuration")
    implementation = previous.get("implementation")
    if (
        not isinstance(implementation, str)
        or re.fullmatch(r"[0-9a-f]{64}", implementation) is None
    ):
        raise ValueError("Resume implementation fingerprint is invalid")
    if (
        implementation != config["implementation"]
        and implementation not in compatible_implementations
    ):
        raise ValueError("Resume implementation is not explicitly compatible")
    if {key: value for key, value in previous.items() if key != "implementation"} != {
        key: value for key, value in config.items() if key != "implementation"
    }:
        raise ValueError("Resume semantic configuration differs")
    expected_run = digest(
        raw + json.dumps(previous, sort_keys=True, ensure_ascii=False).encode()
    )[:20]
    if (
        expected_run != run_id
        or manifest.get("run_id") != run_id
        or manifest.get("source_sha256") != digest(raw)
        or manifest.get("text_sha256") != digest(raw.decode("utf-8-sig").encode())
        or manifest.get("book_id") != config["book_id"]
        or manifest.get("title") != config["title"]
    ):
        raise ValueError("Resume source or run identity differs")
    return directory, manifest


def _validate_complete_resume(directory: Path, manifest: dict[str, Any]) -> None:
    """复用完整书前检查原文、必要产物、记录身份和计数；不发模型请求。

    directory 已通过恢复身份校验且持有运行锁；manifest 只能为 complete。
    返回 None，发现缺失、运行失败标记或记录不合法则抛异常，不能当作完整成功。
    """
    if any(error.get("severity") != "limitation" for error in manifest["errors"]):
        raise ValueError("Complete resume has unresolved errors")
    text = (directory / "source.md").read_bytes().decode("utf-8")
    units = json.loads((directory / "units.json").read_text(encoding="utf-8"))
    if (
        digest(text.encode()) != manifest["text_sha256"]
        or "".join(unit["text"] for unit in units) != text
    ):
        raise ValueError("Complete resume source snapshot differs")
    unit_ids = {unit["id"] for unit in units}
    records = [
        Record.model_validate_json(line, context={"evidence_ids": unit_ids})
        for line in (directory / "records.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    if (
        len(unit_ids) != len(units)
        or len(records) != manifest["records"]
        or len({record.record_id for record in records}) != len(records)
        or any(
            record.run_id != manifest["run_id"]
            or record.book_id != manifest["book_id"]
            or any(
                "failed" in issue or "unreviewed" in issue for issue in record.issues
            )
            for record in records
        )
    ):
        raise ValueError("Complete resume record identity or completeness differs")


def extract_book(
    source: Path,
    output: Path,
    client: LLMClient,
    *,
    title: str | None = None,
    subject: str = "",
    book_id: str | None = None,
    expected_sha256: str | None = None,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    workers: int = DEFAULT_CHUNK_WORKERS,
    resume_run_id: str | None = None,
    compatible_implementations: tuple[str, ...] = (),
) -> dict[str, Any]:
    """读取 source 单本 MD，在 output 下创建或恢复按来源及配置区分的运行。

    返回运行摘要并写来源、候选、最终记录和执行统计；workers 控制块并发。
    expected_sha256 可指定冻结清单的原始字节哈希，不匹配则在创建产物或调用模型前失败。
    resume_run_id 显式复用原目录；旧源码指纹必须在 compatible_implementations 白名单
    内或等于当前指纹，其他语义配置不允许变化。完整结果经过离线检查后直接返回。
    非 MD、空文本或非法并发数抛出 ValueError，已有运行锁或致命服务错误不静默重试。
    """
    if source.suffix.lower() != ".md":
        raise ValueError("First version accepts .md files only")
    if workers < 1:
        raise ValueError("workers must be positive")
    raw = source.read_bytes()
    text = raw.decode("utf-8-sig")
    if not text.strip():
        raise ValueError("Empty Markdown input")
    source_hash = digest(raw)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ValueError("Source SHA256 differs from the frozen manifest")
    book_id = book_id or source_hash[:16]
    title = title or source.stem
    config = {
        "prompt": PROMPT_VERSION,
        "model": client.model,
        "chunk_tokens": chunk_tokens,
        "implementation": digest(
            b"".join(
                p.read_bytes()
                for p in (
                    Path(__file__).with_name(name + ".py")
                    for name in IMPLEMENTATION_MODULES
                )
            )
        ),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in [
                "markdown-it-py",
                "chonkie",
                "transformers",
                "tokenizers",
                "instructor",
                "openai",
                "pydantic",
            ]
        },
        "python": platform.python_version(),
        "service": getattr(client, "identity", {}),
        "max_output_tokens": client.max_output_tokens,
        "extra_body": client.extra_body,
        "title": title,
        "subject": subject,
        "book_id": book_id,
    }
    run_id = digest(
        raw + json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
    )[:20]
    directory = output / run_id
    current_config = config
    previous_manifest: dict[str, Any] | None = None
    if resume_run_id is not None:
        directory, previous_manifest = validate_resume(
            output, resume_run_id, raw, config, compatible_implementations
        )
        run_id = resume_run_id
        # 缓存身份固定在原语义配置；实际代码版本单独记入本次执行审计。
        config = previous_manifest["config"]
    directory.mkdir(parents=True, exist_ok=True)
    # A lock file prevents concurrent writers for the same run, without a server.
    lock = directory / ".running"
    with lock.open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "recovery": (
                    "Remove this lock only after confirming "
                    "that this process has stopped."
                ),
            },
            handle,
        )
    if previous_manifest is not None and previous_manifest.get("status") == "complete":
        try:
            _validate_complete_resume(directory, previous_manifest)
            return previous_manifest
        finally:
            lock.unlink(missing_ok=True)
    execution_id = uuid4().hex
    execution_directory = directory / "executions" / execution_id
    execution_started = monotonic()
    try:
        log = EventLog(
            execution_directory / "events.jsonl",
            execution_id=execution_id,
            run_id=run_id,
            book_id=book_id,
        )
    except BaseException:
        lock.unlink(missing_ok=True)
        raise
    result: dict[str, Any] = {}
    execution_metadata = {
        "implementation": current_config["implementation"],
        "cache_implementation": config["implementation"],
        "resumed_run_id": resume_run_id,
        "service": current_config["service"],
        "workers": workers,
        "http_timeout_seconds": getattr(client, "timeout", None),
        "max_connections": getattr(client, "max_connections", None),
        "gateway_retries": getattr(client, "gateway_retries", None),
        "transport_failure_limit": getattr(client, "transport_failure_limit", None),
    }
    try:
        write_json(execution_directory / "execution.json", execution_metadata)
        log.event(
            "book_started",
            source_characters=len(text),
            source_bytes=len(raw),
            model=client.model,
            http_timeout_seconds=getattr(client, "timeout", None),
            workers=workers,
            chunk_tokens=chunk_tokens,
            execution_metadata=execution_metadata,
        )
        with client.scope() if hasattr(client, "scope") else nullcontext():
            result = _run(
                source,
                text,
                source_hash,
                book_id,
                run_id,
                title,
                subject,
                config,
                directory,
                client,
                chunk_tokens,
                workers,
                log,
                execution_id,
                execution_metadata,
            )
        return result
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt) and hasattr(client, "cancel"):
            client.cancel()
        manifest_path = directory / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {"run_id": run_id, "config": config}
        )
        manifest.update(
            status="interrupted"
            if isinstance(error, (ExtractionCancelledError, KeyboardInterrupt))
            else "failed",
            fatal_error=type(error).__name__,
        )
        result = manifest
        write_json(manifest_path, manifest)
        raise
    finally:
        try:
            try:
                log.event(
                    "book_finished",
                    elapsed_seconds=monotonic() - execution_started,
                    **{
                        key: result[key]
                        for key in ("status", "records", "candidates", "chunks")
                        if key in result
                    },
                )
            finally:
                log.close()
            summary = summarize_events(execution_directory / "events.jsonl")
            write_json(execution_directory / "summary.json", summary)
            if result:
                result.update(
                    execution_id=execution_id,
                    execution_stats=summary,
                    current_execution=execution_metadata,
                    executions=[
                        str(path.relative_to(directory))
                        for path in sorted(
                            (directory / "executions").glob("*/summary.json")
                        )
                    ],
                )
                result.pop("client_stats", None)
                write_json(directory / "manifest.json", result)
        finally:
            lock.unlink(missing_ok=True)


def _run(
    source: Path,
    text: str,
    source_hash: str,
    book_id: str,
    run_id: str,
    title: str,
    subject: str,
    config: dict[str, Any],
    directory: Path,
    client: LLMClient,
    chunk_tokens: int,
    workers: int,
    log: EventLog,
    execution_id: str,
    execution_metadata: dict[str, Any],
) -> dict[str, Any]:
    """在调用方持有运行锁时执行解析、区域判断、发现、复核和综合。

    使用固定来源与 config 持久化各阶段结果，返回 complete/partial 摘要；
    致命异常由外层登记失败。
    """
    scope = {
        "log": log,
        "execution_id": execution_id,
        "run_id": run_id,
        "book_id": book_id,
    }
    with stage_scope(client, log, scope, "parse"):
        units = parse_markdown(text)
        write_json(directory / "units.json", [asdict(u) for u in units])
        (directory / "source.md").write_text(text, encoding="utf-8", newline="")
    manifest: dict[str, Any] = {
        "book_id": book_id,
        "run_id": run_id,
        "title": title,
        "source": str(source.resolve()),
        "source_sha256": source_hash,
        "text_sha256": digest(text.encode()),
        "config": config,
        "current_execution": execution_metadata,
        "status": "running",
        "directory": str(directory.resolve()),
        "source_statistics": {
            "characters": len(text),
            "tokens": token_count(text),
            "units": len(units),
            "nonblank_units": sum(bool(unit.text.strip()) for unit in units),
        },
    }
    write_json(directory / "manifest.json", manifest)
    # 目录、索引等名称来源必须在任何区域排除之前读取。
    rule_candidates = extract_rule_candidates(units)
    if (directory / "regions.json").exists():
        log.event("cache_hit", stage="preprocess", work_id="regions")
    with stage_scope(client, log, scope, "preprocess"):
        active, decisions = preprocess(units, client, directory)
    active_ids = {unit.id for unit in active}
    filtered_rule_candidates = 0
    for candidate in rule_candidates:
        retained = [key for key in candidate.evidence_ids if key in active_ids]
        if retained == candidate.evidence_ids:
            continue
        # 名称提名保留，但已排除的出版/非正文区域不能经规则反查回流为释义。
        filtered_rule_candidates += 1
        candidate.evidence_ids = retained
        candidate.evidence_spans = [
            span for span in candidate.evidence_spans if span.unit_id in active_ids
        ]
        candidate.issues.append("rule_evidence_excluded_by_preprocess")
        if not retained and "insufficient_body_evidence" not in candidate.issues:
            candidate.issues.append("insufficient_body_evidence")
    write_jsonl(
        directory / "rule_candidates.jsonl", [c.model_dump() for c in rule_candidates]
    )
    preprocessing = json.loads(
        (directory / "preprocess_summary.json").read_text(encoding="utf-8")
    )
    manifest["preprocess_statistics"] = preprocessing
    # active决定证据可用性，discovery_units仅决定发现输入；分流不删除原文。
    routed = {
        key: origin
        for key, origin in rule_only_units(units, rule_candidates).items()
        if key in active_ids
    }
    discovery_units = [unit for unit in active if unit.id not in routed]
    write_json(directory / "discovery_routing.json", routed)
    log.event(
        "discovery_routing",
        stage="chunking",
        rule_only_units=len(routed),
        discovery_units=len(discovery_units),
    )
    with stage_scope(client, log, scope, "chunking"):
        chunks = chunk_units(discovery_units, chunk_tokens)
        pieces = chunk_payloads(discovery_units, chunks)
        write_json(directory / "chunks.json", [asdict(c) for c in chunks])
    errors: list[dict[str, Any]] = []
    # 保守保留原文不会丢数据，但调用失败不能被整书 complete 状态掩盖。
    # 模型正常返回 uncertain 是语义未定；它不同于预算超限或未完成调用。
    if preprocessing["failed_decisions"] or preprocessing["unrecorded_candidates"]:
        errors.append(
            {
                "stage": "preprocess",
                "error": "region_decisions_incomplete",
                "failed_decisions": preprocessing["failed_decisions"],
                "unrecorded_candidates": preprocessing["unrecorded_candidates"],
            }
        )
    elif preprocessing["uncertain_decisions"]:
        errors.append(
            {
                "stage": "preprocess",
                "error": "region_decisions_uncertain",
                "severity": "limitation",
                "uncertain_decisions": preprocessing["uncertain_decisions"],
            }
        )
    unit_lookup = {unit.id: unit for unit in units}
    discovery_counts: dict[str, int] = {}

    def observe_discovery(
        identity: str, checkpoint: dict[str, Any], *, from_cache: bool
    ) -> None:
        """记录成功叶子块的名称发现数量；后续综合失败不撤销已成功的发现。

        缓存 split 父块只读取子检查点，不重复统计父结果。名称阶段失败、
        无成功 raw 或纯空白跳过不计为空；书级名称数为叶子数量之和，未全书去重。
        """
        if checkpoint.get("split"):
            for child_index in range(2):
                child_id = f"{identity}-s{child_index}"
                path = directory / "discovery" / f"{child_id}.json"
                if path.exists():
                    observe_discovery(
                        child_id,
                        json.loads(path.read_text(encoding="utf-8")),
                        from_cache=from_cache,
                    )
            return
        raw = checkpoint.get("raw_discovery")
        if raw is None or any(
            error.get("stage") == "discovery" for error in checkpoint.get("errors", [])
        ):
            return
        count = len(raw["findings"])
        # 各工作线程仅写自己的唯一块 ID；汇总在全部 future 完成后读取。
        discovery_counts[identity] = count
        log.event(
            "discovery_result",
            stage="discovery",
            work_id=identity,
            name_count=count,
            is_empty=count == 0,
            from_cache=from_cache,
        )

    def discover(index: int) -> tuple[list[Candidate], list[dict[str, Any]]]:
        """处理 chunks[index] 的核心块，返回候选及未完成原因。

        绑定块级计时；只有允许的预算、截断或验证耗尽才进入一次父块二分。
        """
        chunk = chunks[index]

        def process(
            core: list[dict[str, Any]],
            identity: str,
            split_allowed: bool,
            child_neighbors: list[dict[str, Any]] | None = None,
        ) -> tuple[list[Candidate], list[dict[str, Any]]]:
            """执行或恢复指定 identity 核心切片的名称发现，返回候选和错误列表。

            通过检查点区分名称、补取和子块状态；
            split_allowed 禁止子块继续递归二分。
            每次成功立即持久化，邻文及原因随名称保存；后续综合失败不重做名称发现。
            """
            path = directory / "discovery" / (identity + ".json")
            previous = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
            if previous.get("complete"):
                restored = [
                    Candidate.model_validate(
                        candidate,
                        context={
                            "evidence_ids": set(unit_lookup),
                            "evidence_texts": {
                                key: unit_lookup[key].text
                                for key in set(candidate["evidence_ids"])
                                | set(candidate.get("name_evidence_ids", []))
                                if key in unit_lookup
                            },
                        },
                    )
                    for candidate in previous["candidates"]
                ]
                if not previous.get("split") and previous.get("raw_discovery"):
                    read_parts = [*core, *previous.get("draft_neighbors", [])]
                    names = NameDiscovery.model_validate(
                        previous["raw_discovery"], context=source_context(read_parts)
                    )
                    expected = make_candidates(names, identity, unit_lookup, read_parts)
                    if restored != expected:
                        raise ValueError("Cached candidates differ from read evidence")
                log.event("cache_hit", stage="discovery", work_id=identity)
                observe_discovery(identity, previous, from_cache=True)
                return restored, previous["errors"]
            core = [part for part in core if part["text"].strip()]
            if not core:
                write_json(
                    path,
                    {
                        "complete": True,
                        "responses": [],
                        "candidates": [],
                        "errors": [],
                        "skip_reason": "whitespace_only",
                    },
                )
                return [], []
            payload: dict[str, Any] = {
                "book": title,
                "subject": subject,
                "headings": chunk.headings,
                "core": core,
            }
            responses: list[dict[str, Any]] = list(previous.get("responses", []))
            failures: list[dict[str, Any]] = []
            candidates: list[Candidate] = []
            split_state = bool(previous.get("split"))
            draft = (
                NameDiscovery.model_validate(
                    previous["raw_discovery"],
                    context=source_context(
                        [*core, *previous.get("draft_neighbors", [])]
                    ),
                )
                if previous.get("raw_discovery") is not None
                else None
            )
            resume_followup = draft is not None and not previous.get("draft_ready")
            discovery_ready = bool(previous.get("draft_ready"))
            read_neighbors = previous.get("draft_neighbors", [])
            call_attempt_limit = min(
                MAX_STAGE_ATTEMPTS, getattr(client, "attempts", MAX_STAGE_ATTEMPTS)
            )
            try:
                # 已有草稿与已二分父块共用一处输入恢复，防止某条路径漏掉
                # 邻文或原因。新发现仍不附加空字段，保持首次请求的边界。
                if draft is not None or split_state:
                    payload["neighbors"] = previous.get("draft_neighbors", [])
                    if previous.get("draft_context_reason") is not None:
                        payload["context_reason"] = previous["draft_context_reason"]
                if split_state:
                    raise IncompleteOutputException()
                if draft is not None:
                    log.event(
                        "cache_hit",
                        stage="discovery",
                        work_id=identity,
                        cache="raw_discovery",
                    )
                if draft is None or resume_followup:
                    # 本次执行使用自己的有界尝试预算；已持久化的首轮不重复调用。
                    remaining = min(
                        MAX_STAGE_ATTEMPTS,
                        getattr(client, "attempts", MAX_STAGE_ATTEMPTS),
                    )
                    collected: list[Any] = (
                        list(draft.findings) if draft is not None else []
                    )
                    first_round = 0
                    if resume_followup:
                        # 非空已保存邻文表示补取曾成功；崩溃恢复不追加第三轮名称发现。
                        # 未解决的上下文状态随证据候选登记。
                        first_round = 2
                        if draft.needs_context and not payload["neighbors"]:
                            neighbors = (
                                child_neighbors
                                if child_neighbors is not None
                                else neighbor_payloads(
                                    pieces, index, DISCOVERY_NEIGHBOR_TOKENS
                                )
                            )
                            if neighbors:
                                payload.update(
                                    neighbors=neighbors,
                                    context_reason=draft.context_reason,
                                )
                                first_round = 1
                    for round_index in range(first_round, 2):
                        call_attempt_limit = remaining
                        with stage_scope(client, log, scope, "discovery", identity):
                            result = client.call(
                                NameDiscovery,
                                [
                                    {"role": "system", "content": DISCOVER},
                                    {
                                        "role": "user",
                                        "content": json.dumps(
                                            payload, ensure_ascii=False
                                        ),
                                    },
                                ],
                                context=source_context(
                                    [*core, *payload.get("neighbors", [])]
                                ),
                                attempts=remaining,
                            )
                        result = NameDiscovery.model_validate(
                            result.model_dump(),
                            context=source_context(
                                [*core, *payload.get("neighbors", [])]
                            ),
                        )
                        read_neighbors = payload.get("neighbors", [])
                        remaining -= client.last_call_attempts
                        responses.append(result.model_dump())
                        collected.extend(result.findings)
                        # 只合并名称锚点，不生成或回流局部释义。
                        unique = {
                            finding.model_dump_json(): finding for finding in collected
                        }
                        draft = NameDiscovery(
                            findings=list(unique.values()),
                            needs_context=result.needs_context,
                            context_reason=result.context_reason,
                        )
                        write_json(
                            path,
                            {
                                "complete": False,
                                "responses": responses,
                                "raw_discovery": draft.model_dump(),
                                "draft_ready": not draft.needs_context,
                                "draft_neighbors": payload.get("neighbors", []),
                                "draft_context_reason": payload.get("context_reason"),
                                "candidates": [],
                                "errors": [],
                            },
                        )
                        if not result.needs_context:
                            break
                        if round_index == 1 or remaining <= 0:
                            break
                        neighbors = (
                            child_neighbors
                            if child_neighbors is not None
                            else neighbor_payloads(
                                pieces, index, DISCOVERY_NEIGHBOR_TOKENS
                            )
                        )
                        if not neighbors:
                            break
                        payload.update(
                            neighbors=neighbors, context_reason=result.context_reason
                        )
                assert draft is not None
                discovery_ready = True
                observe_discovery(
                    identity,
                    {"raw_discovery": draft.model_dump()},
                    from_cache=len(responses) == len(previous.get("responses", [])),
                )
                if draft.needs_context:
                    failures.append(
                        {
                            "stage": "discovery",
                            "chunk_id": identity,
                            "error": "context_incomplete",
                        }
                    )
                candidates = make_candidates(
                    draft,
                    identity,
                    unit_lookup,
                    [*core, *read_neighbors],
                )
            except (
                IncompleteOutputException,
                ContextBudgetError,
                InstructorRetryException,
            ) as error:
                repair_exhausted = exhausted_validation(
                    error, client, call_attempt_limit
                )
                if (
                    split_allowed
                    and (
                        not isinstance(error, InstructorRetryException)
                        or repair_exhausted
                    )
                    and len("".join(p["text"] for p in core)) > 1
                ):
                    split_state = True
                    children = split_payload(core)
                    write_json(
                        path,
                        {
                            "complete": False,
                            "split": True,
                            "responses": responses,
                            "raw_discovery": draft.model_dump() if draft else None,
                            "draft_neighbors": payload.get("neighbors", []),
                            "draft_context_reason": payload.get("context_reason"),
                            "candidates": [],
                            "errors": [],
                        },
                    )
                    for child_index, child in enumerate(children):
                        found, failed = process(
                            child,
                            f"{identity}-s{child_index}",
                            False,
                            neighbor_payloads(
                                children, child_index, DISCOVERY_NEIGHBOR_TOKENS
                            ),
                        )
                        candidates.extend(found)
                        failures.extend(failed)
                else:
                    failures.append(
                        {
                            "stage": "discovery",
                            "chunk_id": identity,
                            "error": type(error).__name__,
                        }
                    )
            except (TelemetryWriteError, ExtractionCancelledError, OSError):
                raise
            except APIStatusError as error:
                if fatal_service_error(error):
                    raise
                failures.append(
                    {
                        "stage": "discovery",
                        "chunk_id": identity,
                        "error": type(error).__name__,
                    }
                )
            except Exception as error:
                failures.append(
                    {
                        "stage": "discovery",
                        "chunk_id": identity,
                        "error": type(error).__name__,
                    }
                )
            data = {
                "complete": not failures,
                "responses": responses,
                "raw_discovery": draft.model_dump() if draft else None,
                "draft_ready": discovery_ready,
                # 失败请求的邻文不能冒充已读证据，恢复时仍需完成该次补取。
                "draft_neighbors": read_neighbors,
                "draft_context_reason": payload.get("context_reason"),
                "candidates": [candidate.model_dump() for candidate in candidates],
                "errors": failures,
            }
            if split_state:
                data["split"] = True
            write_json(path, data)
            return candidates, failures

        with stage_scope(client, log, scope, "chunk", chunk.id):
            return process(pieces[index], chunk.id, True)

    candidates: list[Candidate] = []
    stopped = Event()

    def run_chunk(index: int) -> tuple[list[Candidate], list[dict[str, Any]]]:
        """按索引执行核心块，返回候选列表和错误列表。

        致命失败通知调度器停止补位；已在途块仍可结束。
        """
        if stopped.is_set():
            return [], []
        try:
            return discover(index)
        except BaseException:
            stopped.set()
            if hasattr(client, "cancel"):
                client.cancel()
            raise

    # 最多提交 workers 个块；快块完成即补位，不等待同波最慢块。
    completed: dict[int, tuple[list[Candidate], list[dict[str, Any]]]] = {}
    remaining = iter(range(len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for _ in range(min(workers, len(chunks))):
            index = next(remaining)
            pending[pool.submit(run_chunk, index)] = index
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            # 先传播已完成任务的异常，再提交新块。
            for future in finished:
                completed[pending.pop(future)] = future.result()
            while len(pending) < workers and not stopped.is_set():
                index = next(remaining, None)
                if index is None:
                    break
                pending[pool.submit(run_chunk, index)] = index
    # 完成时序不影响候选、错误及后续综合的原始输入顺序。
    for index in range(len(chunks)):
        found, failed = completed[index]
        candidates.extend(found)
        errors.extend(failed)
    # 不按同名强并，保留独立来源及其原始候选身份供综合阶段判断。
    body_count = len(candidates)
    candidates.extend(rule_candidates)
    with stage_scope(client, log, scope, "candidate_export"):
        write_jsonl(
            directory / "candidates.jsonl", [c.model_dump() for c in candidates]
        )
    with stage_scope(client, log, scope, "synthesis"):
        records, synthesis_errors = synthesize(
            candidates,
            units,
            client,
            book_id,
            run_id,
            directory / "synthesis",
            workers=workers,
            scope=scope,
        )
    name_audit = [
        {
            key: value
            for key, value in row.items()
            if key not in {"stage", "severity", "issue"}
        }
        for row in synthesis_errors
        if row.get("stage") == "name_decision"
    ]
    audit_ids = [row["candidate_id"] for row in name_audit]
    if len(audit_ids) != len(set(audit_ids)) or set(audit_ids) != {
        c.candidate_id for c in candidates
    }:
        raise ValueError("Name audit must account for every candidate exactly once")
    errors.extend(
        row for row in synthesis_errors if row.get("stage") != "name_decision"
    )
    write_jsonl(directory / "name_decisions.jsonl", name_audit)
    name_statistics = {
        label: sum(row["decision"] == decision for row in name_audit)
        for label, decision in [
            ("accepted", "accept"),
            ("rejected", "reject"),
            ("uncertain", "uncertain"),
            ("unreviewed", "unreviewed"),
        ]
    }
    name_statistics["failed"] = sum(row["failed"] for row in name_audit)
    with stage_scope(client, log, scope, "record_export"):
        write_jsonl(directory / "records.jsonl", [r.model_dump() for r in records])
    manifest.update(
        status="partial"
        if any(e.get("severity") != "limitation" for e in errors)
        else "complete",
        chunks=len(chunks),
        candidates=len(candidates),
        records=len(records),
        errors=errors,
        excluded_units=sum(d["action"] == "exclude" for d in decisions),
        name_decision_statistics=name_statistics,
        output_statistics={
            "name_decisions": name_statistics,
            "successful_discovery_blocks": len(discovery_counts),
            "empty_discovery_blocks": sum(
                count == 0 for count in discovery_counts.values()
            ),
            "discovered_names": sum(discovery_counts.values()),
            "body_candidates": body_count,
            "rule_candidates": len(rule_candidates),
            "rule_only_units": len(routed),
            "rule_candidates_with_excluded_evidence": filtered_rule_candidates,
            "unique_candidate_names": len({candidate.name for candidate in candidates}),
            "candidate_evidence_spans": sum(len(c.evidence_spans) for c in candidates),
            "formula_context_enriched_candidates": sum(
                any(issue.startswith("formula_context_added:") for issue in c.issues)
                for c in candidates
            ),
            "formula_context_added_units": sum(
                len(set(c.evidence_ids) - set(c.name_evidence_ids))
                for c in candidates
                if "body" in c.origins
            ),
            "name_only_candidates": sum(not c.evidence_ids for c in candidates),
            "candidate_origins": {
                origin: sum(origin in c.origins for c in candidates)
                for origin in ("body", "toc", "index", "glossary")
            },
            "missing_definitions": sum(record.definition is None for record in records),
            "merged_records": sum(len(record.candidate_ids) > 1 for record in records),
            "records_with_issues": sum(bool(record.issues) for record in records),
        },
    )
    write_json(directory / "manifest.json", manifest)
    return manifest
