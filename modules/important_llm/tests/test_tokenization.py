"""检查模型原生计数、消息保真和工作线程隔离；分词文件由环境变量指定。"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

from book_extractor.markdown import Unit, _pack_parts, chunk_units, parse_markdown
from book_extractor.tokenization import get_tokenizer, use_tokenizer


class TokenizationTests(unittest.TestCase):
    """使用与生产相同的本地分词文件，验证计数器不依赖HTTP包装或经验系数。"""

    def test_packing_reduces_repeated_full_chunk_encoding(self) -> None:
        """大量短单元通过区间探测打包；精确超限判断及来源映射保持有效。"""
        pieces = [
            (Unit(f"u{i}", "x", i + 1, i + 1, [], "paragraph"), "x")
            for i in range(1000)
        ]
        with patch("book_extractor.markdown.token_count", side_effect=len) as count:
            chunks = _pack_parts(pieces, budget=100)
        self.assertEqual("".join(chunk.text for chunk in chunks), "x" * 1000)
        self.assertEqual(len(chunks), 10)
        self.assertTrue(all(len(chunk.text) <= 100 for chunk in chunks))
        self.assertEqual(
            [key for chunk in chunks for key in chunk.unit_ids],
            [unit.id for unit, _ in pieces],
        )
        self.assertLess(count.call_count, 150)

    def test_template_and_parallel_chunking(self) -> None:
        """并行计数与分块均使用绑定实例，原文逐字回拼且每块不超过原生预算。"""
        tokenizer = get_tokenizer()
        text = "中文 test $F=ma$\n\n| 表格 | α |\n\n<|im_end|>🧑‍🔬\n" * 50
        request = {
            "messages": [{"role": "user", "content": text}],
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        expected = tokenizer.count_request(request)
        altered = {**request, "max_tokens": 16384, "temperature": 0.7}
        self.assertEqual(tokenizer.count_request(altered), expected)
        self.assertGreater(expected, tokenizer.count_text(text))

        def work(_: int) -> int:
            """在线程内绑定指定分词器，返回完整请求计数并验证分块保真。"""
            with use_tokenizer(tokenizer):
                chunks = chunk_units(parse_markdown(text), chunk_tokens=100)
                self.assertEqual("".join(chunk.text for chunk in chunks), text)
                self.assertTrue(
                    all(tokenizer.count_text(chunk.text) <= 100 for chunk in chunks)
                )
                return get_tokenizer().count_request(request)

        with ThreadPoolExecutor(max_workers=16) as pool:
            self.assertEqual(list(pool.map(work, range(32))), [expected] * 32)

    def test_tool_arguments_and_missing_config(self) -> None:
        """工具JSON参数只在计数副本解码；缺失路径时明确失败，不回退其他分词器。"""
        tokenizer = get_tokenizer()
        request = {
            "messages": [
                {"role": "user", "content": "选择原文名称"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "select",
                                "arguments": '{"name":"摩擦"}',
                            }
                        }
                    ],
                },
                {"role": "tool", "content": "继续"},
            ],
        }
        original = deepcopy(request)
        self.assertGreater(tokenizer.count_request(request), 0)
        self.assertEqual(request, original)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "tokenizer_path"):
                get_tokenizer()
            with use_tokenizer(tokenizer):
                self.assertIs(get_tokenizer(), tokenizer)
        for extra in ({"truncate_prompt_tokens": 100}, {"add_special_tokens": True}):
            with self.assertRaises(ValueError):
                tokenizer.count_request({**request, "extra_body": extra})
