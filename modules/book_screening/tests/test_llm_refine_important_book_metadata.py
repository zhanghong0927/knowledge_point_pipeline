from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from llm_refine_important_book_metadata import (  # noqa: E402
    build_request_body,
    build_system_prompt,
    build_user_prompt,
    call_batch_resilient,
    load_checkpoint,
    parse_batch_response,
)


class MetadataLlmRefinementTests(unittest.TestCase):
    def test_request_disables_qwen_thinking(self) -> None:
        body = build_request_body(
            model="test-model",
            system_prompt="system",
            rows=[{"record_id": "r1", "title": "结构工程"}],
            max_tokens=800,
        )

        self.assertEqual({"enable_thinking": False}, body["chat_template_kwargs"])
        self.assertFalse(body["stream"])

    def test_failed_multi_record_batch_is_split_without_relaxing_validation(self) -> None:
        rows = [{"record_id": f"r{i}"} for i in range(4)]

        def fake_call(*args, **kwargs):
            batch = args[3]
            if len(batch) > 2:
                try:
                    raise ValueError("duplicate record_id")
                except ValueError as exc:
                    raise RuntimeError("malformed batch") from exc
            return [
                {"record_id": row["record_id"], "decision": "KEEP", "evidence_fields": []}
                for row in batch
            ]

        with patch("llm_refine_important_book_metadata.call_batch", side_effect=fake_call):
            results = call_batch_resilient("url", "model", "prompt", rows, 1, 0, 100)

        self.assertEqual(["r0", "r1", "r2", "r3"], [row["record_id"] for row in results])

    def test_checkpoint_results_can_be_loaded_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "checkpoint.jsonl"
            path.write_text(
                '{"record_id":"r1","decision":"KEEP","evidence_fields":[]}\n'
                '{"record_id":"r2","decision":"DROP","evidence_fields":[]}\n',
                encoding="utf-8",
            )

            results = load_checkpoint(path)

        self.assertEqual({"r1", "r2"}, set(results))
        self.assertEqual("DROP", results["r2"]["decision"])

    def test_subject_boundary_attachment_is_injected_into_prompt(self) -> None:
        config = {
            "subject_name": "土木工程",
            "boundary": {
                "core_scope": "结构、岩土、桥梁和隧道工程",
                "accepted_adjacent": ["工程材料", "工程管理"],
                "excluded_scope": ["室内装饰", "数据结构"],
            },
        }

        prompt = build_system_prompt(config)

        self.assertIn("土木工程", prompt)
        self.assertIn("结构、岩土、桥梁和隧道工程", prompt)
        self.assertIn("工程材料", prompt)
        self.assertIn("数据结构", prompt)

    def test_existing_term_list_boundary_is_supported(self) -> None:
        config = {
            "subject_name": "心理学",
            "boundary": {
                "strong_terms": ["认知心理", "心理测量"],
                "adjacent_subject_labels": ["教育学"],
                "exclude_phrases": ["心理惊悚小说"],
            },
        }

        prompt = build_system_prompt(config)

        self.assertIn("认知心理", prompt)
        self.assertIn("教育学", prompt)
        self.assertIn("心理惊悚小说", prompt)

    def test_user_prompt_contains_only_selected_metadata_and_record_id(self) -> None:
        prompt = build_user_prompt([
            {
                "record_id": "r000001",
                "identifier": "book-1",
                "title": "结构工程原理",
                "abstract": "讨论结构分析的基本概念。",
                "documentpath": "oss://secret/source.pdf",
                "parsed_path": "oss://secret/source.md",
            }
        ])

        self.assertIn("r000001", prompt)
        self.assertIn("结构工程原理", prompt)
        self.assertIn("讨论结构分析", prompt)
        self.assertNotIn("documentpath", prompt)
        self.assertNotIn("parsed_path", prompt)
        self.assertNotIn("oss://secret", prompt)

    def test_thinking_prefix_and_fenced_json_are_parsed_per_book(self) -> None:
        response = """分析完成。</think>
```json
{"results":[
  {"record_id":"r1","decision":"KEEP","subject_fit":"core","knowledge_extraction_fit":"high","book_type":"textbook","evidence_fields":["title","abstract"],"reason":"核心教材"},
  {"record_id":"r2","decision":"drop","subject_fit":"outside","knowledge_extraction_fit":"low","book_type":"fiction","evidence_fields":["title"],"reason":"小说"}
]}
```
"""

        results = parse_batch_response(response, ["r1", "r2"])

        self.assertEqual(["KEEP", "DROP"], [item["decision"] for item in results])
        self.assertEqual(["r1", "r2"], [item["record_id"] for item in results])

    def test_missing_record_is_rejected(self) -> None:
        response = '{"results":[{"record_id":"r1","decision":"KEEP"}]}'
        with self.assertRaisesRegex(ValueError, "缺少"):
            parse_batch_response(response, ["r1", "r2"])


if __name__ == "__main__":
    unittest.main()
