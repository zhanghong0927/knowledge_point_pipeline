"""用真实 Pydantic 和 Instructor 异常验证诊断信息的脱敏与数量上限。"""

import json
import unittest

from instructor.core.exceptions import InstructorRetryException
from pydantic import ValidationError

from book_extractor.llm import LLMClient


class DiagnosticTests(unittest.TestCase):
    """保留定位错误所需的字段和类型，禁止序列化模型控制的内容。"""

    def test_wrapped_validation_errors_are_bounded_and_redacted(self) -> None:
        """区分引用、别名、缺失和 JSON 错误，移除私密载荷并限制诊断条数。"""
        details = [
            {
                "type": "value_error",
                "loc": ("findings", 0, "evidence_ids"),
                "input": ["SECRET_INPUT"],
                "ctx": {"error": ValueError("SECRET_MESSAGE")},
            },
            {
                "type": "missing",
                "loc": ("findings", 0, "aliases"),
                "input": {"PRIVATE_INPUT_KEY": "SECRET_INPUT"},
            },
            {
                "type": "extra_forbidden",
                "loc": ("findings", 0, "PRIVATE_IDENTIFIER"),
                "input": "SECRET_INPUT",
            },
            {"type": "string_type", "loc": ("sensitive-field content",), "input": 42},
            {"type": "string_type", "loc": ("x" * 80,), "input": 42},
            {
                "type": "json_invalid",
                "loc": (),
                "input": "SECRET_JSON",
                "ctx": {"error": "SECRET_PARSE_REASON"},
            },
        ]
        error = ValidationError.from_exception_data("TestResponse", details)
        wrapped = InstructorRetryException(
            "SECRET_WRAPPER_MESSAGE",
            n_attempts=3,
            total_usage={},
            messages=[{"content": "SECRET_PROMPT"}],
            last_completion={"api_key": "SECRET_KEY"},
        )
        wrapped.__cause__ = error
        result = LLMClient._error_fields(wrapped)
        encoded = json.dumps(result)
        self.assertEqual(result["exception_type"], "ValidationError")
        self.assertEqual(result["validation_error_count"], 6)
        self.assertIn(
            {"type": "value_error", "loc": ["findings", 0, "evidence_ids"]},
            result["validation_errors"],
        )
        self.assertIn(
            {"type": "missing", "loc": ["findings", 0, "aliases"]},
            result["validation_errors"],
        )
        self.assertIn(
            {"type": "extra_forbidden", "loc": ["findings", 0, "<extra-field>"]},
            result["validation_errors"],
        )
        self.assertIn({"type": "json_invalid", "loc": []}, result["validation_errors"])
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("sensitive-field", encoded)
        self.assertNotIn("x" * 80, encoded)
        self.assertTrue(
            all(set(item) == {"type", "loc"} for item in result["validation_errors"])
        )
        many = ValidationError.from_exception_data(
            "Many", [{"type": "missing", "loc": ("aliases",), "input": {}}] * 31
        )
        limited = LLMClient._error_fields(many)
        self.assertEqual(len(limited["validation_errors"]), 30)
        self.assertEqual(limited["validation_error_count"], 31)
        self.assertTrue(limited["validation_errors_truncated"])


if __name__ == "__main__":
    unittest.main()
