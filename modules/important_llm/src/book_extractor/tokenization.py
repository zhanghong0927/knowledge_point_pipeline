"""复用模型原生分词器和聊天模板计数，不使用跨模型估算或经验倍率。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

TOKENIZER_PATH_ENV = "BOOK_EXTRACTOR_TOKENIZER_PATH"
_active: ContextVar[ModelTokenizer | None] = ContextVar("model_tokenizer", default=None)


class ModelTokenizer:
    """只加载本地模型分词文件；文本分块和完整消息计量共用同一实例。"""

    def __init__(self, path: Path) -> None:
        """从path读取分词器及模板并记录内容指纹；缺文件或无模板立即失败。"""
        self.path = path.resolve(strict=True)
        files = [self.path / "tokenizer.json", self.path / "tokenizer_config.json"]
        template = self.path / "chat_template.jinja"
        if template.exists():
            files.append(template)
        self.fingerprint = hashlib.sha256(
            b"".join(file.name.encode() + b"\0" + file.read_bytes() for file in files)
        ).hexdigest()
        self.backend = AutoTokenizer.from_pretrained(
            str(self.path), local_files_only=True, trust_remote_code=False
        )
        if not self.backend.chat_template:
            raise ValueError("Model tokenizer must provide its chat template")

    def count_text(self, text: str) -> int:
        """返回原文text的模型token数；不额外添加聊天边界或特殊前后缀。"""
        return len(self.backend.encode(text, add_special_tokens=False))

    def count_request(self, request: dict[str, Any]) -> int:
        """对最终消息应用原生聊天模板并计数，Schema以实际消息/工具形式纳入。

        温度、max_tokens和HTTP JSON包装不是模型输入，不参与计数。
        只支持本模块的文本聊天；图片、音视频与嵌入输入须另接对应处理器，禁止估算。
        """
        messages = deepcopy(request["messages"])
        if any(
            not isinstance(message.get("content"), (str, type(None)))
            for message in messages
        ):
            raise ValueError("Exact token counting only supports text chat messages")
        extra = request.get("extra_body") or {}
        # OpenAI传输协议中的arguments是JSON字符串；vLLM渲染模板前会解析为对象。
        # 仅修改计数副本，实际发送的消息仍遵循OpenAI格式。
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call["function"]
                if isinstance(function.get("arguments"), str):
                    function["arguments"] = json.loads(function["arguments"])
        options = {
            **extra.get("chat_template_kwargs", {}),
            **request.get("chat_template_kwargs", {}),
        }
        for key in (
            "add_generation_prompt",
            "continue_final_message",
            "chat_template",
        ):
            if key in extra:
                options[key] = extra[key]
            if key in request:
                options[key] = request[key]
        if extra.get("add_special_tokens") or request.get("add_special_tokens"):
            raise ValueError("Text chat counting requires add_special_tokens=false")
        if extra.get("truncate_prompt_tokens") or request.get("truncate_prompt_tokens"):
            raise ValueError("Prompt truncation would discard source evidence")
        options.setdefault("add_generation_prompt", True)
        tools = request.get("tools")
        if request.get("tool_choice") == "none":
            tools = None
        token_ids = self.backend.apply_chat_template(
            messages, tools=tools, tokenize=True, return_dict=False, **options
        )
        return len(token_ids)


@lru_cache(maxsize=4)
def _load(path: str) -> ModelTokenizer:
    """按规范绝对路径复用只读分词器，返回实例，不下载模型或执行远程代码。"""
    return ModelTokenizer(Path(path))


def get_tokenizer(path: str | Path | None = None) -> ModelTokenizer:
    """优先使用显式路径，其次当前调用作用域或环境配置；未配置不回退估算。"""
    if path is None and _active.get() is not None:
        return _active.get()
    selected = path or os.environ.get(TOKENIZER_PATH_ENV)
    if not selected:
        raise ValueError(f"Configure tokenizer_path or {TOKENIZER_PATH_ENV}")
    return _load(str(Path(selected).expanduser().resolve(strict=True)))


@contextmanager
def use_tokenizer(tokenizer: ModelTokenizer) -> Iterator[None]:
    """在当前执行上下文绑定tokenizer，退出后恢复；工作线程由client.scope显式绑定。"""
    token = _active.set(tokenizer)
    try:
        yield
    finally:
        _active.reset(token)
