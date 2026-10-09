"""提供有界的结构化模型调用、验证反馈与逐次请求统计。HTTP 超时按阶段生效；

token 由模型原生分词器与实际聊天模板精确计数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import ssl
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event, Lock, local
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import instructor
from instructor.core.exceptions import (
    IncompleteOutputException,
    InstructorRetryException,
    ResponseParsingError,
)
from openai import APIConnectionError, APIStatusError, OpenAI
from pydantic import (
    BaseModel,
    ModelWrapValidatorHandler,
    ValidationError,
    ValidationInfo,
    create_model,
    model_validator,
)
from tenacity import RetryCallState, Retrying, retry_if_exception

from .telemetry import EventLog, TelemetryWriteError
from .tokenization import get_tokenizer, use_tokenizer

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_KEEPALIVE_CONNECTIONS = 20
DEFAULT_OUTPUT_TOKENS = 8192
MIN_ADAPTIVE_OUTPUT_TOKENS = 4096
DEFAULT_ATTEMPTS = 3
DEFAULT_GATEWAY_RETRIES = 10
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_CONTEXT_LIMIT = 32768
DEFAULT_TEMPERATURE = 0.0
CONNECT_TIMEOUT_SECONDS = 10.0
RETRY_FIRST_WAIT_SECONDS = 2.0
RETRY_LATER_WAIT_SECONDS = 8.0
RETRY_AFTER_MAX_SECONDS = 60.0
GATEWAY_RETRY_WAIT_SECONDS = 30.0
GATEWAY_RETRY_MAX_WAIT_SECONDS = 120.0
GATEWAY_RETRY_JITTER_SECONDS = 10.0
ERROR_ITEM_LIMIT = 30
ERROR_LOCATION_DEPTH = 12
ERROR_INDEX_LIMIT = 1_000_000
PROVIDER_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
ERROR_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
ERROR_LOCATION_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,47}")


_PRIVATE_RETRY_LOG: ContextVar[bool] = ContextVar(
    "book_extractor_private_retry", default=False
)


class _PrivateRetryFilter(logging.Filter):
    """仅屏蔽本提取调用中的Instructor原始异常日志，其他调用者保持原行为。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """根据当前调用上下文返回是否放行record；不读取或改写敏感消息。"""
        return not _PRIVATE_RETRY_LOG.get()


_RETRY_LOG_FILTER = _PrivateRetryFilter()


def fatal_service_error(error: BaseException) -> bool:
    """判断服务是否明确拒绝请求；仅瞬态HTTP状态允许上层继续补做。"""
    return (
        isinstance(error, APIStatusError)
        and 400 <= error.status_code < 500
        and error.status_code not in {408, 409, 429}
    )


def _validation_model(
    response_model: type[T], context: dict[str, Any] | None
) -> type[T]:
    """根据响应类型和调用上下文返回验证模型；

    保留原 Schema 与校验器，避免 Instructor 将原文当作模板。
    """
    if context is None:
        return response_model

    @model_validator(mode="wrap")
    @classmethod
    def validate(
        cls: type[BaseModel],
        value: Any,
        handler: ModelWrapValidatorHandler,
        info: ValidationInfo,
    ) -> T:
        """用原始类型严格校验输入并返回实例；

        绑定本次上下文，验证失败向 Instructor 抛出原校验异常。
        """
        # Instructor 1.17 couples context to Jinja rendering. Keep original
        # source strings out of that renderer while retaining native reasks.
        return response_model.model_validate(value, context=context, strict=True)

    return cast(
        type[T],
        create_model(
            response_model.__name__,
            __base__=response_model,
            __doc__=response_model.__doc__,
            __validators__={"_bound_context": validate},
        ),
    )


class ExtractionCancelledError(RuntimeError):
    """客户端已取消；停止后续请求及重试，已发送请求允许自然收尾。"""


class ContextBudgetError(ValueError):
    """完整请求与输出预留超过模型上下文预算时抛出的异常；此时尚未发送 HTTP。"""


class DeferredCallError(RuntimeError):
    """传输故障达到本轮上限；保留未完成工作，等待后续有界恢复。"""


def normalize_base_url(value: str) -> str:
    """将value中的服务根地址规范为含独立v1路径段的URL并返回。

    已有v1路径段不重复添加；主机名、查询参数和v10不算v1路径段。
    返回地址保留原查询与片段。非绝对HTTP(S)地址或非法端口抛ValueError。
    """
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Expected an absolute HTTP service URL")
        _ = parsed.port
    except ValueError:
        raise ValueError("Invalid HTTP service base_url") from None
    path = parsed.path.rstrip("/")
    if "v1" not in path.split("/"):
        path += "/v1"
    return urlunsplit(parsed._replace(path=path))


def load_service(
    path: Path, service: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """从私密配置选择服务和模型，返回LLMClient构造参数。

    参数：
        path：含services映射及可选default的JSON文件路径。
        service：显式服务名；为None时选default或唯一服务。
        model：显式模型名；为None时读取所选服务的model。

    返回：
        含base_url、api_key、model及可选extra_body、temperature、mode、tokenizer_path；不可写入日志。
        密钥优先读取环境变量，也接受明确的api_key；api_key_env只能是变量名。

    服务不明确、配置缺失或缺少模型/密钥时抛错，不静默切换服务或模型。
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    services = data["services"]
    selected = service or data.get("default")
    if selected is None and len(services) == 1:
        selected = next(iter(services))
    if selected is None:
        raise ValueError("Select a service or configure a default service")
    if selected not in services:
        raise ValueError("Selected service is not configured")
    config = services[selected]
    reference = config.get("api_key_env", "")
    key = os.environ.get(reference, "") if reference else ""
    if not key:
        key = config.get("api_key", "")
    selected_model = model or config.get("model")
    if not key:
        raise ValueError(
            ("Service API key is missing; set the configured environment variable")
        )
    if not selected_model:
        raise ValueError("An explicit model is required for this service")
    base_url = normalize_base_url(config["base_url"])
    result: dict[str, Any] = {
        "base_url": base_url,
        "api_key": key,
        "model": selected_model,
    }
    if "extra_body" in config:
        if not isinstance(config["extra_body"], dict):
            raise ValueError("Service extra_body must be an object")
        result["extra_body"] = merge_thinking_config(config["extra_body"])
    if "temperature" in config:
        result["temperature"] = _validate_temperature(config["temperature"])
    if "tokenizer_path" in config:
        location = Path(config["tokenizer_path"]).expanduser()
        result["tokenizer_path"] = str(
            (path.parent / location).resolve()
            if not location.is_absolute()
            else location
        )
    if "mode" in config:
        mode = config["mode"]
        if not isinstance(mode, str) or mode not in {"json", "tools", "json_schema"}:
            raise ValueError("mode must be json, tools or json_schema")
        result["mode"] = mode
    if "gateway_retries" in config:
        result["gateway_retries"] = config["gateway_retries"]
    return result


def _validate_temperature(value: Any) -> float:
    """返回规范化浮点温度；拒绝布尔、非数值、非有限值及 0..2 之外的参数。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 2
        or not math.isfinite(value)
    ):
        raise ValueError("temperature must be a finite number between 0 and 2")
    return float(value)


def merge_thinking_config(
    extra_body: dict[str, Any] | None = None, thinking: bool | None = None
) -> dict[str, Any]:
    """校验服务附加参数并按显式 thinking 覆盖，返回不修改输入的新字典。

    有 chat_template_kwargs 时在模板对象内设置开关，否则兼容直接开关。
    未指定覆盖时保留配置；完全未配置时不向服务注入供应商参数。
    非对象、非布尔开关及矛盾开关抛 ValueError，不根据 URL 猜供应商。
    """
    if thinking is not None and not isinstance(thinking, bool):
        raise ValueError("Thinking override must be a boolean")
    if extra_body is None:
        return {} if thinking is None else {"enable_thinking": thinking}
    if not isinstance(extra_body, dict):
        raise ValueError("Service extra_body must be an object")
    if "temperature" in extra_body:
        raise ValueError("Use service-level temperature, not extra_body.temperature")
    result = dict(extra_body)
    if "enable_thinking" in result and not isinstance(result["enable_thinking"], bool):
        raise ValueError("enable_thinking must be a boolean")
    if "chat_template_kwargs" not in result:
        if thinking is not None:
            result["enable_thinking"] = thinking
        return result
    template = result["chat_template_kwargs"]
    if not isinstance(template, dict):
        raise ValueError("chat_template_kwargs must be an object")
    template = dict(template)
    if "enable_thinking" in template and not isinstance(
        template["enable_thinking"], bool
    ):
        raise ValueError("chat_template_kwargs.enable_thinking must be a boolean")
    if "enable_thinking" in result:
        direct = result.pop("enable_thinking")
        if "enable_thinking" in template and template["enable_thinking"] != direct:
            raise ValueError("Conflicting direct and template thinking settings")
        template["enable_thinking"] = direct
    if thinking is not None:
        template["enable_thinking"] = thinking
    result["chat_template_kwargs"] = template
    return result


def _retryable(error: BaseException) -> bool:
    """根据异常类型与 HTTP 状态返回是否允许重试；鉴权、参数和本地预算错误不重试。"""
    if isinstance(error, APIStatusError):
        return error.status_code in {408, 409, 429} or error.status_code >= 500
    return isinstance(
        error,
        (
            APIConnectionError,
            ValidationError,
            json.JSONDecodeError,
            ResponseParsingError,
        ),
    )


def _retry_wait(state: RetryCallState) -> float:
    """根据本次失败返回等待秒数；

    解析 Retry-After 数字或日期并限幅，无效值使用固定回退间隔。
    """
    fallback = (
        RETRY_FIRST_WAIT_SECONDS
        if state.attempt_number == 1
        else RETRY_LATER_WAIT_SECONDS
    )
    error = state.outcome.exception() if state.outcome is not None else None
    gateway_timeout = isinstance(error, APIStatusError) and error.status_code == 504
    if gateway_timeout:
        fallback = min(
            GATEWAY_RETRY_MAX_WAIT_SECONDS,
            GATEWAY_RETRY_WAIT_SECONDS * 2 ** min(state.attempt_number - 1, 2),
        ) + random.uniform(0, GATEWAY_RETRY_JITTER_SECONDS)
    if not isinstance(error, APIStatusError):
        return fallback
    header = error.response.headers.get("Retry-After")
    if not header:
        return fallback
    try:
        seconds = float(header)
    except ValueError:
        try:
            date = parsedate_to_datetime(header)
            # Valid HTTP dates carry GMT; reject ambiguous local-time values.
            if date.tzinfo is None:
                return fallback
            seconds = max(0.0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return fallback
    if not math.isfinite(seconds) or seconds < 0:
        return fallback
    header_wait = min(seconds, RETRY_AFTER_MAX_SECONDS)
    return max(fallback, header_wait) if gateway_timeout else header_wait


class LLMClient:
    """共享连接池的结构化客户端；普通尝试与504重试分别有界，每线程独立记账。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = DEFAULT_OUTPUT_TOKENS,
        attempts: int = DEFAULT_ATTEMPTS,
        gateway_retries: int = DEFAULT_GATEWAY_RETRIES,
        transport_failure_limit: int | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        mode: str = "json",
        temperature: float = DEFAULT_TEMPERATURE,
        extra_body: dict[str, Any] | None = None,
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        tokenizer_path: str | Path | None = None,
    ) -> None:
        """创建共享连接池及有界的结构化调用器，不立即发起推理请求。

        base_url、api_key、model指定兼容服务；建议由load_service解析配置。
        max_output_tokens是请求输出上限，context_limit用于核验实际请求预算。
        tokenizer_path指定模型分词文件；缺省时需设置分词路径环境变量。
        attempts包含首次尝试；timeout是HTTP阶段超时，不是总墙钟取消时限。
        mode选择json、tools或json_schema；extra_body追加服务参数，
        但不能覆盖模型、消息、输出结构和预算等保留字段。
        temperature为0..2的有限数值，服务配置应放在服务级而不是extra_body中。

        无返回值。非法参数抛ValueError；关闭SDK隐式重试并使用系统CA。
        max_connections同时限制全局在途请求和HTTP连接数，不进入语义缓存身份。
        使用结束后调用close释放连接池。

        gateway_retries为每次逻辑调用额外允许的504重试数，0表示遇504即停止。
        504不扣attempts；其他错误和成功请求仍使用原普通尝试额度。
        transport_failure_limit限制一次调用内的连接及5xx故障总数；None沿用原预算。
        达限后抛DeferredCallError，不将未完成记录视为成功，也不自动重跑整书。
        """
        if transport_failure_limit is not None and (
            isinstance(transport_failure_limit, bool)
            or not isinstance(transport_failure_limit, int)
            or transport_failure_limit < 1
        ):
            raise ValueError("transport_failure_limit must be a positive integer")
        if (
            isinstance(gateway_retries, bool)
            or not isinstance(gateway_retries, int)
            or gateway_retries < 0
        ):
            raise ValueError("gateway_retries must be a nonnegative integer")
        if isinstance(max_connections, bool) or not isinstance(max_connections, int):
            raise ValueError("max_connections must be a positive integer")
        if (
            min(max_output_tokens, attempts, timeout, context_limit, max_connections)
            <= 0
        ):
            raise ValueError("Budgets, attempts and timeout must be positive")
        modes = {
            "json": instructor.Mode.JSON,
            "tools": instructor.Mode.TOOLS,
            "json_schema": instructor.Mode.JSON_SCHEMA,
        }
        if not isinstance(mode, str) or mode not in modes:
            raise ValueError("mode must be json, tools or json_schema")
        temperature = _validate_temperature(temperature)
        if extra_body and set(extra_body) & {
            "model",
            "messages",
            "tools",
            "response_format",
            "max_tokens",
            "max_completion_tokens",
            "stream",
            "temperature",
        }:
            raise ValueError("extra_body cannot override request structure or budgets")
        self.model = model
        self.mode = mode
        self.max_output_tokens = max_output_tokens
        self.attempts = attempts
        self.gateway_retries = gateway_retries
        self.transport_failure_limit = transport_failure_limit
        self.context_limit = context_limit
        self.max_connections = max_connections
        self.timeout = timeout
        self.temperature = temperature
        self.extra_body = dict(extra_body or {})
        self._identity = {
            "base_url_sha256": hashlib.sha256(
                base_url.rstrip("/").encode()
            ).hexdigest(),
            "model": model,
            "mode": mode,
            "temperature": temperature,
            "context_limit": context_limit,
            "max_output_tokens": max_output_tokens,
            "attempts": attempts,
            "extra_body_sha256": hashlib.sha256(
                json.dumps(self.extra_body, sort_keys=True).encode()
            ).hexdigest(),
        }
        self._cancelled = Event()
        self._thread = local()
        self.tokenizer = get_tokenizer(tokenizer_path)
        self._identity["tokenizer_sha256"] = self.tokenizer.fingerprint
        self._lock = Lock()
        self._request_lock = Lock()
        self._request_order: deque[str] = deque()
        self._request_waiters: dict[str, deque[Event]] = {}
        self._admitted_requests: set[Event] = set()
        self._peak_requests = 0
        self._stats = {
            "calls": 0,
            "responses": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "usage_missing": 0,
            "budget_rejections": 0,
        }
        self._sdk = OpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            timeout=httpx.Timeout(
                timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)
            ),
            # Preserve certificate verification while using Windows'
            # trusted system roots, including the deployed service CA.
            http_client=httpx.Client(
                verify=ssl.create_default_context(),
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=min(
                        DEFAULT_KEEPALIVE_CONNECTIONS, max_connections
                    ),
                ),
                event_hooks={"response": [self._observe_response]},
            ),
        )
        self._create = self._sdk.chat.completions.create
        # Instructor assembles Schema and reask messages before reaching this hook.
        # Checking here includes every retry's actual prompt, not just the caller text.
        self._sdk.chat.completions.create = self._tracked_create
        # 固定版本的重试logger直接输出异常及模型input_value；诊断使用脱敏事件。
        retry_logger = logging.getLogger("instructor.v2.retry")
        if _RETRY_LOG_FILTER not in retry_logger.filters:
            retry_logger.addFilter(_RETRY_LOG_FILTER)
        self._client = instructor.from_openai(self._sdk, mode=modes[mode])
        self._client.on("parse:error", self._observe_parse_error)

    def cancel(self) -> None:
        """永久停止本客户端的新请求；不关闭连接池、不撤销服务端在途推理。"""
        self._cancelled.set()
        with self._request_lock:
            for queue in self._request_waiters.values():
                for ticket in queue:
                    ticket.set()

    def raise_if_cancelled(self) -> None:
        """取消后抛不可重试异常，供请求边界和调度器及时停止后续工作。"""
        if self._cancelled.is_set():
            raise ExtractionCancelledError("Extraction was cancelled")

    @property
    def request_queue_state(self) -> dict[str, int]:
        """返回全局在途、等待、排队书数和上限，供补书调度及性能观测使用。"""
        with self._request_lock:
            return {
                "active": len(self._admitted_requests),
                "waiting": sum(map(len, self._request_waiters.values())),
                "waiting_books": len(self._request_order),
                "limit": self.max_connections,
                "peak": self._peak_requests,
            }

    def _dispatch_requests(self) -> None:
        """在持有队列锁时轮转分配空闲槽位，仅唤醒获准的线程，避免全队列争抢。"""
        while (
            not self._cancelled.is_set()
            and len(self._admitted_requests) < self.max_connections
            and self._request_order
        ):
            owner = self._request_order.popleft()
            ticket = self._request_waiters[owner].popleft()
            if self._request_waiters[owner]:
                self._request_order.append(owner)
            else:
                del self._request_waiters[owner]
            self._admitted_requests.add(ticket)
            self._peak_requests = max(self._peak_requests, len(self._admitted_requests))
            ticket.set()

    @contextmanager
    def _request_slot(self) -> Iterator[dict[str, int | float]]:
        """按书轮转授予一个全局HTTP槽位，返回排队统计；退出时必定归还。

        同书内部FIFO，只有一书就绪时可借用全部额度。不占用HTTP槽位等待重试；
        取消或排队异常会清除票据，不能堵住队首。已发送请求允许正常收尾。
        """
        scope = getattr(self._thread, "scope", {})
        owner = scope.get("book_id") or scope.get("run_id") or "unscoped"
        ticket = Event()
        queued_at = time.perf_counter()
        with self._request_lock:
            self.raise_if_cancelled()
            if owner not in self._request_waiters:
                self._request_waiters[owner] = deque()
                self._request_order.append(owner)
            self._request_waiters[owner].append(ticket)
            self._dispatch_requests()
        try:
            ticket.wait()
            self.raise_if_cancelled()
            with self._request_lock:
                state = {
                    "queue_wait_seconds": time.perf_counter() - queued_at,
                    "global_inflight": len(self._admitted_requests),
                    "global_waiting": sum(map(len, self._request_waiters.values())),
                    "concurrency_limit": self.max_connections,
                }
            yield state
        finally:
            with self._request_lock:
                if ticket in self._admitted_requests:
                    self._admitted_requests.remove(ticket)
                else:
                    self._request_waiters[owner].remove(ticket)
                    if not self._request_waiters[owner]:
                        del self._request_waiters[owner]
                        self._request_order.remove(owner)
                self._dispatch_requests()

    @property
    def identity(self) -> dict[str, Any]:
        """返回不含凭据的配置副本，用于区分可恢复运行；地址和附加参数仅保留哈希。"""
        return dict(self._identity)

    @property
    def last_call_attempts(self) -> int:
        """返回当前线程最近一次调用实际进入 SDK 的次数；失败后仍保留该计数。"""
        return getattr(self._thread, "attempts", 0)

    @property
    def last_call_ordinary_attempts(self) -> int:
        """返回当前线程最近调用的普通 HTTP 尝试数，504 消耗独立网关预算。"""
        return self.last_call_attempts - getattr(self._thread, "gateway_failures", 0)

    @property
    def stats(self) -> dict[str, int]:
        """返回加锁后的兼容统计副本；

        token 是已知累计量，必须结合 usage_missing 理解。
        """
        with self._lock:
            return dict(self._stats)

    @contextmanager
    def scope(self, *, log: EventLog | None = None, **metadata: str) -> Iterator[None]:
        """临时绑定本线程的日志与任务身份，退出时恢复；

        嵌套继承已有字段，工作线程须显式绑定。
        """
        if set(metadata) - {"execution_id", "run_id", "book_id", "stage", "work_id"}:
            raise ValueError("Unknown accounting scope field")
        previous = getattr(self._thread, "scope", {})
        current = {**previous, **metadata}
        if log is not None:
            current["log"] = log
        self._thread.scope = current
        try:
            with use_tokenizer(self.tokenizer):
                yield
        finally:
            self._thread.scope = previous

    def event(self, kind: str, **metadata: Any) -> None:
        """将事件类型与安全元数据写入当前线程日志；

        未绑定日志则跳过，写入失败向外抛出。
        """
        scope = dict(getattr(self._thread, "scope", {}))
        writer = scope.pop("log", None)
        if writer is not None:
            call_event = kind in {
                "call_started",
                "call_finished",
                "request_queued",
                "request_started",
                "request_finished",
                "budget_rejected",
                "parse_failed",
                "reask_regenerated",
                "output_budget_reduced",
            }
            fields = {
                **scope,
                "model": self.model,
                "call_id": getattr(self._thread, "call_id", None)
                if call_event
                else None,
                **metadata,
            }
            writer.event(kind, **fields)

    def _observe_response(self, response: httpx.Response) -> None:
        """从 HTTP 响应记录当前线程的真实状态码；不保存响应正文或头部。"""
        self._thread.http_status = response.status_code

    def _observe_parse_error(self, error: Exception, **metadata: Any) -> None:
        """记录每次解析/验证失败的安全字段，关联刚完成的请求和当前调用。

        使用原生 parse:error 钩子，不修改异常或响应；保存安全字段供预算紧张时反馈。
        忽略钩子附带数据，只复用有界脱敏诊断和本客户端维护的请求标识。
        """
        fields = self._error_fields(error)
        self._thread.last_parse_error = deepcopy(
            {
                key: fields[key]
                for key in ("exception_type", "validation_errors")
                if key in fields
            }
        )
        # 成员来源校验器只在ctx放程序生成的可信ID；用于纠错，不扩大日志内容。
        if isinstance(error, ValidationError):
            feedback = self._thread.last_parse_error.get("validation_errors", [])
            for safe, detail in zip(feedback, error.errors(include_input=False)):
                if detail["type"] not in {
                    "evidence_id_not_allowed",
                    "name_only_definition",
                }:
                    continue
                context = detail.get("ctx", {})
                for key in ("candidate_ids", "allowed_evidence_ids"):
                    values = context.get(key)
                    if isinstance(values, list):
                        safe[key] = [
                            value
                            for value in values[:ERROR_ITEM_LIMIT]
                            if isinstance(value, str)
                            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
                        ]
                for key in ("allowed_evidence_count", "unknown_count"):
                    count = context.get(key)
                    if isinstance(count, int) and 0 <= count <= ERROR_INDEX_LIMIT:
                        safe[key] = count
        fields["http_status"] = getattr(self._thread, "http_status", None)
        self.event(
            "parse_failed",
            request_id=getattr(self._thread, "request_id", None),
            attempt=self.last_call_attempts,
            **fields,
        )

    @staticmethod
    def _error_fields(error: BaseException) -> dict[str, Any]:
        """从异常返回有界的安全诊断字段；解开重试包装，仅记录类型、合法位置与状态码。"""
        seen: set[int] = set()
        while (
            isinstance(error, InstructorRetryException)
            and error.__cause__ is not None
            and id(error) not in seen
        ):
            seen.add(id(error))
            error = error.__cause__
        code = getattr(error, "code", None)
        if (
            not isinstance(code, (str, int))
            or not PROVIDER_CODE_PATTERN.fullmatch(str(code))
            or str(code).startswith("sk-")
        ):
            code = None
        fields: dict[str, Any] = {
            "exception_type": type(error).__name__,
            "http_status": getattr(error, "status_code", None),
            "provider_code": code,
        }
        if not isinstance(error, ValidationError):
            return fields
        # Pydantic has no include_msg switch. Select only type/loc from its
        # result; never persist the returned message, context, input or URL.
        errors = error.errors(
            include_input=False, include_context=False, include_url=False
        )
        safe_errors = []
        for item in errors[:ERROR_ITEM_LIMIT]:
            error_type = item["type"]
            safe_type = (
                error_type
                if ERROR_TYPE_PATTERN.fullmatch(error_type)
                else "custom_error"
            )
            location: list[str | int] = []
            for index, part in enumerate(item["loc"][:ERROR_LOCATION_DEPTH]):
                if error_type == "extra_forbidden" and index == len(item["loc"]) - 1:
                    # Extra keys originate in model output, even if they look
                    # like identifiers. Never emit their actual spelling.
                    location.append("<extra-field>")
                elif (
                    isinstance(part, int)
                    and not isinstance(part, bool)
                    and 0 <= part <= ERROR_INDEX_LIMIT
                ):
                    location.append(part)
                elif isinstance(part, str) and ERROR_LOCATION_PATTERN.fullmatch(part):
                    location.append(part)
                else:
                    location.append("<redacted>")
            safe_errors.append({"type": safe_type, "loc": location})
        fields.update(
            validation_error_count=len(errors),
            validation_errors=safe_errors,
            validation_errors_truncated=len(errors) > ERROR_ITEM_LIMIT,
        )
        return fields

    def _count_request_tokens(self, request: dict[str, Any]) -> int:
        """计算Instructor已组装的最终模型输入，包括Schema、历史纠错和聊天模板。"""
        return self.tokenizer.count_request(request)

    def _tracked_create(self, *args: Any, **kwargs: Any) -> Any:
        """计量组装后的完整请求并记录真实调用；

        预算不足先拒绝，统计写入失败禁止继续重试。
        """
        self.raise_if_cancelled()
        removed_messages = 0
        can_regenerate = False
        regenerated = False
        messages = kwargs.get("messages")
        if (
            self.mode in {"json", "json_schema"}
            and not kwargs.get("stream")
            and isinstance(messages, list)
        ):
            initial = getattr(self._thread, "initial_messages", None)
            if initial is None:
                # 保存Instructor已组装的Schema和原文，而不是调用者的未组装消息。
                self._thread.initial_messages = deepcopy(messages)
            else:
                suffix = messages[len(initial) :]
                # 只识别当前Instructor JSON协议；陌生形状、工具调用或前缀变化不裁剪。
                valid_pairs = len(suffix) >= 2 and len(suffix) % 2 == 0
                for index in range(0, len(suffix), 2):
                    if not valid_pairs:
                        break
                    assistant, correction = suffix[index : index + 2]
                    valid_pairs = (
                        isinstance(assistant, dict)
                        and isinstance(correction, dict)
                        and assistant.get("role") == "assistant"
                        and not assistant.get("tool_calls")
                        and correction.get("role") == "user"
                        and isinstance(correction.get("content"), str)
                        and self._thread.last_parse_error is not None
                    )
                if messages[: len(initial)] == initial and valid_pairs:
                    can_regenerate = True
                    removed_messages = len(suffix) - 2
                    kwargs = {
                        **kwargs,
                        "messages": deepcopy(initial) + deepcopy(suffix[-2:]),
                    }
        # 仍先保留最新失败响应；只有它使完整请求超预算才改为从原始来源重新生成。
        input_tokens = self._count_request_tokens(kwargs)
        input_limit = self.context_limit
        # 每次纠错均受原生 attempts 总上限约束；不能再加“一次再生”门槛，
        # 否则第二次纠错成功可容纳的原请求会被失败JSON挤出，提前失去第三次机会。
        if (
            input_tokens + self.max_output_tokens > input_limit
            and can_regenerate
            and self._thread.last_parse_error is not None
        ):
            before = input_tokens
            correction = {
                "role": "user",
                "content": (
                    "The previous JSON failed validation and has been discarded. "
                    "Generate a complete new JSON instance from the original source "
                    "and Schema above; preserve all original constraints. "
                    "Do not return a patch. Validation errors (type/location): "
                    + json.dumps(self._thread.last_parse_error, ensure_ascii=False)
                ),
            }
            kwargs = {**kwargs, "messages": deepcopy(initial) + [correction]}
            input_tokens = self._count_request_tokens(kwargs)
            regenerated = True
            self.event(
                "reask_regenerated",
                attempt=self._thread.attempts + 1,
                previous_input_tokens=before,
                input_tokens=input_tokens,
                discarded_reask_messages=len(messages) - len(initial),
            )
        request_id = uuid4().hex
        attempt = self._thread.attempts + 1
        output_tokens = self.max_output_tokens
        available = input_limit - input_tokens
        if (
            input_tokens + output_tokens > input_limit
            and MIN_ADAPTIVE_OUTPUT_TOKENS <= available < output_tokens
            and kwargs.get("max_tokens") == output_tokens
        ):
            # max_tokens是生成上限，不必因预留满额而拒绝本来可生成完整JSON的请求。
            # 原文不变，至少留下4096输出tokens；max_tokens不改变输入计数。
            output_tokens = available
            kwargs = {**kwargs, "max_tokens": output_tokens}
        details = {
            "request_id": request_id,
            "attempt": attempt,
            "retry_index": attempt - 1,
            "removed_reask_messages": removed_messages,
            "removed_reask_rounds": removed_messages // 2,
            "reask_regenerated": regenerated,
            "input_tokens": input_tokens,
            "tokenizer_sha256": self.tokenizer.fingerprint,
            "requested_output_tokens": self.max_output_tokens,
            "reserved_output_tokens": output_tokens,
            "context_limit": self.context_limit,
        }
        if input_tokens + output_tokens > self.context_limit:
            with self._lock:
                self._stats["budget_rejections"] += 1
            self.event(
                "budget_rejected",
                **details,
                http_attempted=False,
                exception_type="ContextBudgetError",
            )
            raise ContextBudgetError(
                "Assembled request plus reserved output exceeds context budget"
            )
        self.event("request_queued", **details)
        with self._request_slot() as admission:
            return self._send_request(args, kwargs, {**details, **admission})

    def _send_request(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], details: dict[str, Any]
    ) -> Any:
        """在已获全局槽位后发送一次HTTP并记账，返回原SDK响应；不负责重试。"""
        # Persist intent before contacting the service. A crash after this point
        # leaves an explicitly unresolved attempt, not an invented zero-cost call.
        self.raise_if_cancelled()
        if details["reserved_output_tokens"] < self.max_output_tokens:
            self.event("output_budget_reduced", **details)
        self.event("request_started", **details)
        with self._lock:
            self._stats["calls"] += 1
        self._thread.attempts += 1
        self._thread.request_id = details["request_id"]
        started = time.perf_counter()
        self._thread.http_status = None
        try:
            response = self._create(*args, **kwargs)
        except Exception as error:
            self.event(
                "request_finished",
                **details,
                elapsed_seconds=time.perf_counter() - started,
                http_success=False,
                finish_reason=None,
                usage={
                    key: None
                    for key in [
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "cached_tokens",
                        "reasoning_tokens",
                    ]
                },
                **self._error_fields(error),
            )
            raise
        elapsed = time.perf_counter() - started
        usage = response.usage
        observed = {
            key: getattr(usage, key, None)
            for key in ["prompt_tokens", "completion_tokens", "total_tokens"]
        }
        observed.update(
            cached_tokens=getattr(
                getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
            ),
            reasoning_tokens=getattr(
                getattr(usage, "completion_tokens_details", None),
                "reasoning_tokens",
                None,
            ),
        )
        with self._lock:
            self._stats["responses"] += 1
            if (
                usage is None
                or observed["prompt_tokens"] is None
                or observed["completion_tokens"] is None
            ):
                self._stats["usage_missing"] += 1
            else:
                self._stats["prompt_tokens"] += observed["prompt_tokens"]
                self._stats["completion_tokens"] += observed["completion_tokens"]
        self.event(
            "request_finished",
            **details,
            elapsed_seconds=elapsed,
            http_success=True,
            http_status=self._thread.http_status,
            provider_code=None,
            exception_type=None,
            finish_reason=response.choices[0].finish_reason
            if response.choices
            else None,
            input_token_delta=(
                observed["prompt_tokens"] - details["input_tokens"]
                if observed["prompt_tokens"] is not None
                else None
            ),
            usage=observed,
        )
        return response

    def call(
        self,
        response_model: type[T],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> T:
        """按响应模型严格校验消息结果并返回原类型实例；

        attempts约束普通尝试；504另用gateway_retries额度。失败抛错，不接受截断。
        """
        self._thread.attempts = 0
        self._thread.gateway_failures = 0
        self._thread.initial_messages = None
        self._thread.last_parse_error = None
        self._thread.request_id = None
        allowed_attempts = self.attempts if attempts is None else attempts
        if not 1 <= allowed_attempts <= self.attempts:
            raise ValueError(
                ("Per-call attempts must be positive and not exceed the client budget")
            )
        self._thread.call_id = uuid4().hex
        gateway_failures = 0
        transport_failures = 0
        deferred = False

        def stop_retry(state: RetryCallState) -> bool:
            """按当前失败消耗对应额度并返回是否停止；504不会重置普通失败计数。"""
            nonlocal gateway_failures, transport_failures, deferred
            error = state.outcome.exception() if state.outcome is not None else None
            if isinstance(error, APIConnectionError) or (
                isinstance(error, APIStatusError) and error.status_code >= 500
            ):
                transport_failures += 1
            if isinstance(error, APIStatusError) and error.status_code == 504:
                gateway_failures += 1
                self._thread.gateway_failures = gateway_failures
            if (
                self.transport_failure_limit is not None
                and transport_failures >= self.transport_failure_limit
            ):
                deferred = True
                return True
            if isinstance(error, APIStatusError) and error.status_code == 504:
                return gateway_failures > self.gateway_retries
            return state.attempt_number - gateway_failures >= allowed_attempts

        def before_retry(state: RetryCallState) -> None:
            """记录即将执行的重试类型、已耗额度及等待秒数，不保存异常正文。"""
            error = state.outcome.exception() if state.outcome is not None else None
            self.event(
                "retry_scheduled",
                retry_kind="gateway_504"
                if isinstance(error, APIStatusError) and error.status_code == 504
                else "ordinary",
                gateway_failures=gateway_failures,
                ordinary_attempts=state.attempt_number - gateway_failures,
                wait_seconds=state.next_action.sleep,
            )

        started = time.perf_counter()
        self.event(
            "call_started",
            schema_model=response_model.__name__,
            allowed_attempts=allowed_attempts,
            allowed_gateway_retries=self.gateway_retries,
            transport_failure_limit=self.transport_failure_limit,
        )
        logging_token = _PRIVATE_RETRY_LOG.set(True)
        try:
            request_messages = deepcopy(messages)
            if self.mode == "json_schema":
                # 原生约束不会自动向模型解释字段；显式说明与约束共用同一Schema。
                # 复制消息列表和字典，避免污染调用方或下一次缓存请求的输入。
                instruction = (
                    "\n返回符合该Schema的JSON实例，不返回Schema本身：\n"
                    + json.dumps(response_model.model_json_schema(), ensure_ascii=False)
                )
                for message in request_messages:
                    if message.get("role") == "system" and isinstance(
                        message.get("content"), str
                    ):
                        message["content"] += instruction
                        break
                else:
                    request_messages.insert(
                        0, {"role": "system", "content": instruction}
                    )
            result = self._client.chat.completions.create(
                model=self.model,
                messages=request_messages,
                response_model=_validation_model(response_model, context),
                context=None,
                temperature=self.temperature,
                max_tokens=self.max_output_tokens,
                extra_body=self.extra_body,
                max_retries=Retrying(
                    stop=stop_retry,
                    retry=retry_if_exception(
                        lambda error: not self._cancelled.is_set() and _retryable(error)
                    ),
                    sleep=self._cancelled.wait,
                    wait=_retry_wait,
                    before_sleep=before_retry,
                    reraise=True,
                ),
            )
        except Exception as error:
            # Expose deterministic failures to the orchestrator, which may split a
            # truncated chunk but must not repeat bad authentication or oversized input.
            cause = (
                error.__cause__
                if isinstance(error, InstructorRetryException) and error.__cause__
                else error
            )
            if isinstance(cause, TelemetryWriteError):
                raise cause from None
            if self._cancelled.is_set():
                cause = ExtractionCancelledError("Extraction was cancelled")
            elif deferred:
                self.event(
                    "call_deferred",
                    transport_failures=transport_failures,
                    elapsed_seconds=time.perf_counter() - started,
                )
                cause = DeferredCallError("Transport failures deferred this work")
            self.event(
                "call_finished",
                parse_success=False,
                actual_attempts=self.last_call_attempts,
                gateway_failures=gateway_failures,
                elapsed_seconds=time.perf_counter() - started,
                **self._error_fields(cause),
            )
            if cause is not error and isinstance(
                cause,
                (
                    APIStatusError,
                    ContextBudgetError,
                    IncompleteOutputException,
                    ExtractionCancelledError,
                    DeferredCallError,
                ),
            ):
                raise cause from None
            raise
        finally:
            _PRIVATE_RETRY_LOG.reset(logging_token)
            self._thread.initial_messages = None
        self.event(
            "call_finished",
            parse_success=True,
            actual_attempts=self.last_call_attempts,
            gateway_failures=gateway_failures,
            elapsed_seconds=time.perf_counter() - started,
            exception_type=None,
        )
        return result

    def close(self) -> None:
        """关闭本客户端拥有的连接池；调用方应先等待全部工作线程结束。"""
        self._sdk.close()
