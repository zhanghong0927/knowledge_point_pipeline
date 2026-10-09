"""离线验证直接和模板思考参数的配置优先级及 CLI 接线。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book_extractor.cli import create_parser, main
from book_extractor.llm import load_service, merge_thinking_config


class ServiceConfigTests(unittest.TestCase):
    """确保不猜供应商、不丢附加参数，也不向请求写入矛盾双开关。"""

    def test_service_temperature_and_mode_validation(self) -> None:
        """只透传显式有效温度及输出模式，拒绝冲突位置和非法配置值。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "services.json"
            basic = {
                "base_url": "http://stub.invalid",
                "model": "stub",
                "api_key": "test-only",
            }

            def load(options: dict[str, object]) -> dict[str, object]:
                """写入无真实凭据的单服务夹具，再走实际配置加载入口。"""
                path.write_text(
                    json.dumps({"services": {"test": {**basic, **options}}}),
                    encoding="utf-8",
                )
                return load_service(path)

            defaults = load({})
            self.assertNotIn("temperature", defaults)
            self.assertNotIn("mode", defaults)
            for mode in ("json", "tools", "json_schema"):
                settings = load({"temperature": 0.7, "mode": mode})
                self.assertEqual(settings["temperature"], 0.7)
                self.assertEqual(settings["mode"], mode)
            for invalid in (True, None, "0.7", -1, 3, float("inf"), float("nan")):
                with self.subTest(temperature=invalid), self.assertRaises(ValueError):
                    load({"temperature": invalid})
            for invalid in (None, [], "JSON", "unknown"):
                with self.subTest(mode=invalid), self.assertRaises(ValueError):
                    load({"mode": invalid})
            with self.assertRaises(ValueError):
                load({"extra_body": {"temperature": 0.7}})

    def test_defaults_and_explicit_overrides(self) -> None:
        """缺配置不注入供应商参数，已有配置保持值，显式开关在正确层级覆盖。"""
        self.assertEqual(merge_thinking_config(), {})
        for original in [
            {"enable_thinking": True, "top_k": 20},
            {
                "chat_template_kwargs": {"enable_thinking": True, "other": "x"},
                "top_k": 20,
            },
            {"chat_template_kwargs": {}, "top_k": 20},
            {"top_k": 20},
        ]:
            with self.subTest(original=original):
                snapshot = copy.deepcopy(original)
                self.assertEqual(merge_thinking_config(original), original)
                for override in (True, False):
                    result = merge_thinking_config(original, override)
                    target = result.get("chat_template_kwargs", result)
                    self.assertIs(target["enable_thinking"], override)
                    self.assertEqual(result["top_k"], 20)
                    if "chat_template_kwargs" in result:
                        self.assertNotIn("enable_thinking", result)
                self.assertEqual(original, snapshot)

    def test_duplicate_switches_and_invalid_shapes(self) -> None:
        """一致的双开关归一到模板；矛盾、非对象及非布尔值必须拒绝。"""
        self.assertEqual(
            merge_thinking_config(
                {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            ),
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        for value in [
            [],
            "invalid",
            {"chat_template_kwargs": None},
            {"chat_template_kwargs": []},
            {"enable_thinking": "false"},
            {"enable_thinking": 0},
            {"chat_template_kwargs": {"enable_thinking": 1}},
            {
                "enable_thinking": True,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                merge_thinking_config(value, False)

    def test_private_config_and_cli_wiring(self) -> None:
        """配置中的模板对象传入客户端，未显式指定时不被 CLI 默认值覆盖。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "services.json"
            extra = {"chat_template_kwargs": {"enable_thinking": True}, "top_k": 20}
            data = {
                "services": {
                    "test": {
                        "base_url": "http://stub.invalid",
                        "model": "stub",
                        "api_key": "test-only",
                        "extra_body": extra,
                    }
                }
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_service(path)["extra_body"], extra)
            invalid = json.loads(json.dumps(data))
            invalid["services"]["test"].pop("api_key")
            invalid["services"]["test"]["api_key_env"] = "sk-inline-is-not-env"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_service(path)
            path.write_text(json.dumps(data), encoding="utf-8")
            for options, expected in [
                ([], True),
                (["--no-thinking"], False),
                (["--thinking"], True),
            ]:
                with (
                    patch(
                        "sys.argv",
                        [
                            "extract",
                            "--input",
                            "unused.md",
                            "--services",
                            str(path),
                            *options,
                        ],
                    ),
                    patch("book_extractor.cli.LLMClient") as constructor,
                    patch(
                        "book_extractor.cli.run_books",
                        return_value=[{"status": "complete"}],
                    ),
                ):
                    main()
                body = constructor.call_args.kwargs["extra_body"]
                self.assertEqual(
                    body,
                    {
                        "chat_template_kwargs": {"enable_thinking": expected},
                        "top_k": 20,
                    },
                )
            for invalid in (None, [], "invalid"):
                data["services"]["test"]["extra_body"] = invalid
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_service(path)

    def test_parser_preserves_unspecified_state(self) -> None:
        """未给开关解析为 None，保证与显式禁用可区分。"""
        parser = create_parser()
        self.assertIsNone(parser.parse_args(["--input", "book.md"]).thinking)
        self.assertIs(
            parser.parse_args(["--input", "book.md", "--thinking"]).thinking, True
        )
        self.assertIs(
            parser.parse_args(["--input", "book.md", "--no-thinking"]).thinking, False
        )


if __name__ == "__main__":
    unittest.main()
