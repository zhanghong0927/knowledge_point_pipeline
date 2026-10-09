from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_clean.py"
SPEC = importlib.util.spec_from_file_location("books_model_clean", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ModelCleanTests(unittest.TestCase):
    def args(self, **changes):
        values = {
            "confidence_threshold": 0.8,
            "model": "test-model",
            "fix_swapped_languages": False,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def request_args(self, **changes):
        values = {
            "model": "test-model",
            "temperature": 0.0,
            "max_tokens": 1024,
            "enable_thinking": False,
            "response_format": "off",
            "retries": 0,
            "single_retries": 0,
            "confidence_threshold": 0.8,
            "fix_swapped_languages": False,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def keep_item(self, name="齿轮", knowledge_point="gear", **changes):
        item = {
            "decision": "keep",
            "confidence": 0.95,
            "subject_relevant": True,
            "field_results": {
                "name": {"decision": "keep", "reason": "中文名称有效"},
                "knowledge_point": {"decision": "keep", "reason": "英文名称有效"},
            },
            "pair_consistency": "consistent",
            "name": name,
            "knowledge_point": knowledge_point,
            "definition": "",
            "en_definition": "",
            "description": "",
            "en_description": "",
            "reason": "测试",
            "quality_flags": [],
            "evidence": name,
        }
        item.update(changes)
        return item

    def test_api_url_normalization(self):
        self.assertEqual(
            MODULE.normalize_api_url("http://service"),
            "http://service/v1/chat/completions",
        )
        self.assertEqual(
            MODULE.normalize_api_url("http://service/v1"),
            "http://service/v1/chat/completions",
        )

    def test_input_budget_covers_all_content_fields(self):
        row = {field: field * 10000 for field in ("definition", "description", "en_definition", "en_description", "cleaning_context")}
        item = MODULE.input_item(row, 1, 2000)
        self.assertLessEqual(sum(len(item[f]) for f in ("definition", "en_definition", "description", "en_description", "context")), 2000)
        self.assertTrue(item["input_truncated"])

    def test_missing_field_contract_cannot_pass(self):
        result = MODULE.normalize_result({"name": "齿轮"}, {"name": "齿轮", "decision": "keep", "confidence": 0.95, "subject_relevant": True}, self.args())
        self.assertEqual(result["decision"], "review")
        self.assertIn("incomplete_review_contract", result["quality_flags"])

    def test_extract_json_from_fence(self):
        payload = MODULE.extract_json('```json\n{"items": []}\n```')
        self.assertEqual(payload, {"items": []})

    def test_layout_reference_cleanup_does_not_truncate_stable_words(self):
        self.assertEqual(MODULE.clean_model_text("Stable angina"), "Stable angina")
        self.assertEqual(
            MODULE.clean_model_text("Stable angina is a symptomatic presentation."),
            "Stable angina is a symptomatic presentation.",
        )
        self.assertEqual(MODULE.clean_model_text("See Table 2-1"), "See")

    def test_keep_is_downgraded_when_name_is_invented(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        item = {
            "decision": "keep",
            "confidence": 0.95,
            "subject_relevant": True,
            "name": "完全不同概念",
            "knowledge_point": "gear",
            "definition": "",
            "en_definition": "",
            "description": "",
            "en_description": "",
            "reason": "test",
            "quality_flags": [],
            "evidence": "齿轮",
        }
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "review")
        self.assertIn("standardized_name_not_grounded_in_source_titles", result["quality_flags"])

    def test_low_confidence_keep_is_review(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        item = {
            "decision": "keep",
            "confidence": 0.7,
            "subject_relevant": True,
            "name": "齿轮",
            "knowledge_point": "gear",
        }
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "review")
        self.assertIn("below_confidence_threshold", result["quality_flags"])

    def test_taxonomy_summary_includes_deep_scope_within_budget(self):
        payload = {
            "name": "机械工程",
            "children": [
                {
                    "name": "机械设计",
                    "acceptance_scope": "接纳机械设计知识",
                    "children": [
                        {
                            "name": "二级节点",
                            "children": [
                                {
                                    "name": "三级节点",
                                    "acceptance_scope": "接纳三级知识",
                                    "children": [
                                        {
                                            "name": "四级节点",
                                            "rejection_scope": "不接纳其他领域",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            summary, stats = MODULE.taxonomy_summary(path, 5000, depth=4)
        self.assertIn("机械工程", summary)
        self.assertIn("二级节点", summary)
        self.assertIn("三级节点", summary)
        self.assertIn("四级节点", summary)
        self.assertIn("不接纳其他领域", summary)
        self.assertEqual(stats["depth"], 4)
        self.assertFalse(stats["truncated"])

    def test_shallow_depth_excludes_deeper_nodes(self):
        payload = {
            "name": "机械工程",
            "children": [
                {
                    "name": "机械设计",
                    "acceptance_scope": "接纳机械设计知识",
                    "children": [{"name": "二级节点", "children": [{"name": "三级节点"}]}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            summary, stats = MODULE.taxonomy_summary(path, 5000, depth=2)
        self.assertIn("机械工程", summary)
        self.assertIn("二级节点", summary)
        self.assertIn("接纳：接纳机械设计知识", summary)
        self.assertNotIn("三级节点", summary)
        self.assertEqual(stats["depth"], 2)

    def test_taxonomy_summary_respects_budget(self):
        payload = {
            "name": "机械工程",
            "children": [
                {
                    "name": "机械设计",
                    "children": [{"name": "二级节点", "children": [{"name": "三级节点"}]}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            summary, stats = MODULE.taxonomy_summary(path, 12, depth=4)
        self.assertTrue(stats["truncated"])
        self.assertLessEqual(len(summary), 12)

    def test_flat_mounting_taxonomy_is_supported(self):
        payload = {
            "root_codes": ["M0"],
            "nodes": [
                {"node_code": "M0", "name": "管理学", "depth": 0, "full_path": "管理学", "path_names": ["管理学"]},
                {"node_code": "M1", "name": "管理科学", "depth": 1, "full_path": "管理学 > 管理科学", "path_names": ["管理学", "管理科学"]},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            index = MODULE.build_taxonomy_index(path, 3)
            summary, stats = MODULE.taxonomy_summary(index, 5000, depth=3)
        self.assertEqual(len(index["nodes"]), 2)
        self.assertIn("管理学 > 管理科学", index["nodes"])
        self.assertIn("管理科学", summary)
        self.assertEqual(stats["nodes_total"], 2)

    def test_finalize_writes_standard_and_review_outputs(self):
        rows = [
            {
                "id": 1,
                "cleaning_record_id": "k1",
                "name": "齿轮",
                "knowledge_point": "gear",
                "source": "book-a",
            },
            {
                "id": 2,
                "cleaning_record_id": "k2",
                "name": "边界概念",
                "knowledge_point": "",
                "source": "book-b",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            outputs = MODULE.known_output_paths(root / "out")
            outputs["db"].parent.mkdir(parents=True)
            connection = MODULE.init_db(outputs["db"])
            MODULE.save_results(
                connection,
                [
                    {
                        "key": "k1",
                        "result": {
                            "decision": "keep",
                            "confidence": 0.95,
                            "name": "齿轮",
                            "knowledge_point": "gear",
                            "definition": "齿轮是传递运动和动力的机械元件。",
                            "en_definition": "",
                            "description": "",
                            "en_description": "",
                        },
                    },
                    {
                        "key": "k2",
                        "result": {
                            "decision": "review",
                            "confidence": 0.6,
                            "reason": "学科边界不确定",
                        },
                    },
                ],
            )
            summary = MODULE.finalize(input_path, connection, outputs, 0, 0)
            connection.close()
            clean_rows = [json.loads(line) for line in outputs["clean"].read_text(encoding="utf-8").splitlines()]
            review_rows = [json.loads(line) for line in outputs["review"].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(summary["counts"], {"keep": 1, "review": 1})
        self.assertEqual(clean_rows[0]["name"], "齿轮")
        self.assertEqual(review_rows[0]["model_cleaning"]["decision"], "review")

    def test_process_batch_with_mock_openai_response(self):
        source = {"name": "齿轮", "knowledge_point": "gear"}
        batch = [
            {
                "key": "k1",
                "source_row": source,
                "api_item": {"key": "k1", "name": "齿轮", "knowledge_point": "gear"},
            }
        ]
        args = argparse.Namespace(
            model="test-model",
            temperature=0.0,
            max_tokens=1024,
            enable_thinking=False,
            response_format="auto",
            retries=0,
            single_retries=0,
            confidence_threshold=0.8,
        )
        payload = {
            "items": [
                {
                    "key": "k1",
                    "decision": "keep",
                    "confidence": 0.95,
                    "subject_relevant": True,
                    "field_results": {
                        "name": {"decision": "keep", "reason": "中文名称有效"},
                        "knowledge_point": {"decision": "keep", "reason": "英文名称有效"},
                    },
                    "pair_consistency": "consistent",
                    "name": "齿轮",
                    "knowledge_point": "gear",
                    "definition": "",
                    "en_definition": "",
                    "description": "",
                    "en_description": "",
                    "reason": "名称和内容有效",
                    "quality_flags": [],
                    "evidence": "齿轮",
                }
            ]
        }
        with mock.patch.object(MODULE, "http_request", return_value=(payload, json.dumps(payload), 42)):
            output = MODULE.process_batch(1, batch, "test prompt", args)
        self.assertEqual(output["tokens"], 42)
        self.assertEqual(output["results"][0]["result"]["decision"], "keep")
        self.assertEqual(output["results"][0]["key"], "k1")

    def test_inconsistent_pair_forces_drop(self):
        row = {"name": "齿轮", "knowledge_point": "bearing"}
        item = {
            "decision": "keep",
            "confidence": 0.98,
            "subject_relevant": True,
            "field_results": {
                "name": {"decision": "keep", "reason": "中文名称形式有效"},
                "knowledge_point": {"decision": "keep", "reason": "英文名称形式有效"},
            },
            "pair_consistency": "inconsistent",
            "name": "齿轮",
            "knowledge_point": "bearing",
        }
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "drop")
        self.assertEqual(result["pair_consistency"], "inconsistent")

    def test_parenthetical_repair_requires_description_transfer(self):
        row = {
            "name": "投射测验（通过模糊刺激了解人格特征的一类测验方法，常用于人格评估。）",
            "knowledge_point": "",
        }
        base_item = {
            "decision": "keep",
            "confidence": 0.95,
            "subject_relevant": True,
            "field_results": {
                "name": {"decision": "keep", "reason": "名称主体有效"},
                "knowledge_point": {"decision": "empty", "reason": "原字段为空"},
            },
            "pair_consistency": "not_applicable",
            "name": "投射测验",
            "knowledge_point": "",
            "description": "通过模糊刺激了解人格特征的一类测验方法，常用于人格评估。",
        }
        accepted = MODULE.normalize_result(row, base_item, self.args())
        self.assertEqual(accepted["repair_validation"]["titles"]["name"]["type"], "parenthetical_moved_to_description")
        without_transfer = dict(base_item)
        without_transfer["description"] = ""
        rejected = MODULE.normalize_result(row, without_transfer, self.args())
        self.assertEqual(rejected["decision"], "review")


    def test_field_result_invalid_uses_marker_not_review_enum(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        item = {
            "decision": "keep",
            "confidence": 0.95,
            "subject_relevant": True,
            "name": "齿轮",
            "knowledge_point": "gear",
        }
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "review")
        for field in ("name", "knowledge_point"):
            check = result["field_results"][field]
            self.assertEqual(check["decision"], "")
            self.assertTrue(check["invalid"])
        self.assertIn("incomplete_review_contract", result["quality_flags"])

    def test_empty_source_field_keeps_empty_marker(self):
        row = {"name": "齿轮", "knowledge_point": ""}
        item = {
            "decision": "keep",
            "confidence": 0.95,
            "subject_relevant": True,
            "field_results": {"name": {"decision": "keep", "reason": "有效"}},
            "name": "齿轮",
            "knowledge_point": "",
        }
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["field_results"]["knowledge_point"]["decision"], "empty")
        self.assertTrue(result["field_results"]["knowledge_point"]["invalid"])

    def test_conventional_language_assignment_is_kept(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        result = MODULE.normalize_result(row, self.keep_item(), self.args())
        self.assertEqual(result["decision"], "keep")
        self.assertNotIn(
            "language_assignment_swapped_by_model", result["quality_flags"]
        )

    def test_swapped_language_assignment_is_reviewed(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        item = self.keep_item(name="gear", knowledge_point="齿轮")
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "review")
        self.assertIn("language_assignment_swapped_by_model", result["quality_flags"])
        self.assertTrue(result["repair_validation"]["language"]["reversed"])

    def test_swapped_language_assignment_can_be_corrected(self):
        row = {"name": "齿轮", "knowledge_point": "gear"}
        item = self.keep_item(name="gear", knowledge_point="齿轮")
        result = MODULE.normalize_result(row, item, self.args(fix_swapped_languages=True))
        self.assertEqual(result["decision"], "keep")
        self.assertEqual(result["name"], "齿轮")
        self.assertEqual(result["knowledge_point"], "gear")
        self.assertTrue(result["repair_validation"]["language"]["corrected"])
        self.assertTrue(result["repair_validation"]["changed_any"])

    def test_mixed_script_term_does_not_trigger_language_guard(self):
        row = {"name": "PID控制", "knowledge_point": "PID control"}
        item = self.keep_item(name="PID控制", knowledge_point="PID control")
        result = MODULE.normalize_result(row, item, self.args())
        self.assertEqual(result["decision"], "keep")
        self.assertFalse(result["repair_validation"]["language"]["reversed"])


    def taxonomy_tree(self, directory):
        payload = {
            "name": "机械工程",
            "path": "机械工程",
            "children": [
                {
                    "name": "机械设计",
                    "path": "机械工程/机械设计",
                    "children": [
                        {
                            "name": "齿轮",
                            "path": "机械工程/机械设计/齿轮",
                            "acceptance_scope": "接纳齿轮强度与接触疲劳知识",
                            "rejection_scope": "不接纳轴承知识",
                        }
                    ],
                }
            ],
        }
        path = Path(directory) / "tree.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return MODULE.build_taxonomy_index(path, 4)

    def test_input_item_injects_candidate_node_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            index = self.taxonomy_tree(directory)
        row = {
            "id": 1,
            "name": "齿轮强度计算",
            "knowledge_point": "",
            "explanation": "齿轮强度计算用于确定齿轮承载能力。",
            "candidate_paths": [
                {"node_path": "机械工程/机械设计/齿轮"},
                {"node_path": "不存在的/节点"},
            ],
        }
        item = MODULE.input_item(row, 1, 3000, index, 3, 240)
        self.assertEqual(len(item["taxonomy_candidates"]), 1)
        self.assertIn("机械工程/机械设计/齿轮", item["taxonomy_candidates"][0])
        self.assertIn("不接纳轴承知识", item["taxonomy_candidates"][0])
        self.assertEqual(item["_taxonomy_candidates_unknown"], 1)

    def test_input_item_without_taxonomy_has_no_candidate_key(self):
        row = {"id": 1, "name": "齿轮", "explanation": "齿轮用于传递运动。"}
        item = MODULE.input_item(row, 1, 3000, None, 3, 240)
        self.assertNotIn("taxonomy_candidates", item)
        self.assertEqual(item["_taxonomy_candidates_unknown"], 0)

    def test_iter_batches_counts_taxonomy_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = self.taxonomy_tree(directory)
            input_path = root / "input.jsonl"
            rows = [
                {
                    "id": 1,
                    "name": "齿轮",
                    "explanation": "齿轮知识",
                    "candidate_paths": [{"node_path": "机械工程/机械设计/齿轮"}],
                },
                {"id": 2, "name": "轴承", "explanation": "轴承知识"},
            ]
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats: dict[str, int] = {}
            batches = list(
                MODULE.iter_batches(input_path, set(), 8, 3000, 0, 0, index, 3, 240, stats)
            )
        self.assertEqual(len(batches), 1)
        self.assertEqual(stats["api_items"], 2)
        self.assertEqual(stats["records_with_candidate_nodes"], 1)
        self.assertEqual(stats["records_without_candidate_nodes"], 1)
        self.assertEqual(stats["candidate_paths_unknown"], 0)

    def test_audit_counters_are_not_sent_to_the_model(self):
        captured = {}

        def fake_http(body, args, use_response_format):
            captured["body"] = body
            return {"items": []}, "", 0

        batch = [
            {
                "key": "k1",
                "source_row": {},
                "api_item": {
                    "key": "k1",
                    "name": "齿轮",
                    "_taxonomy_candidates_unknown": 2,
                },
            }
        ]
        with mock.patch.object(MODULE, "http_request", side_effect=fake_http):
            MODULE.request_model("batch-1", batch, "prompt", self.request_args())
        payload = json.loads(captured["body"]["messages"][1]["content"])
        self.assertNotIn("_taxonomy_candidates_unknown", payload["items"][0])


    def save_checkpoint(self, connection, decision, key, policy_version=None):
        MODULE.save_results(
            connection,
            [
                {
                    "key": key,
                    "result": {
                        "decision": decision,
                        "policy_version": policy_version or MODULE.MODEL_POLICY_VERSION,
                    },
                }
            ],
        )

    def test_done_keys_filters_other_policy_version(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = MODULE.init_db(Path(directory) / "checkpoint.sqlite")
            self.save_checkpoint(connection, "keep", "k1")
            self.save_checkpoint(connection, "review", "k2", "old_policy")
            done, ignored = MODULE.done_keys(connection, MODULE.MODEL_POLICY_VERSION)
            connection.close()
        self.assertEqual(done, {"k1"})
        self.assertEqual(ignored, 1)

    def test_retry_decisions_reselects_review(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = MODULE.init_db(Path(directory) / "checkpoint.sqlite")
            self.save_checkpoint(connection, "keep", "k1")
            self.save_checkpoint(connection, "review", "k2")
            self.save_checkpoint(connection, "error", "k3")
            done, ignored = MODULE.done_keys(
                connection, MODULE.MODEL_POLICY_VERSION, {"review", "error"}
            )
            connection.close()
        self.assertEqual(done, {"k1"})
        self.assertEqual(ignored, 0)

    def test_retry_decision_set_validates_values(self):
        self.assertEqual(
            MODULE.retry_decision_set(
                argparse.Namespace(retry_decisions="review, error", retry_errors=True)
            ),
            {"review", "error"},
        )
        with self.assertRaises(ValueError):
            MODULE.retry_decision_set(
                argparse.Namespace(retry_decisions="unknown", retry_errors=False)
            )

    def test_legacy_checkpoint_rows_are_migrated_and_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.sqlite"
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE results (record_key TEXT PRIMARY KEY, decision TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            legacy.execute("INSERT INTO results VALUES ('k1', 'keep', '{}', 'now')")
            legacy.commit()
            legacy.close()
            connection = MODULE.init_db(path)
            stored = connection.execute(
                "SELECT policy_version FROM results WHERE record_key = 'k1'"
            ).fetchone()[0]
            done, ignored = MODULE.done_keys(connection, MODULE.MODEL_POLICY_VERSION)
            connection.close()
        self.assertEqual(stored, MODULE.LEGACY_POLICY_VERSION)
        self.assertEqual(done, set())
        self.assertEqual(ignored, 1)


    def test_source_id_prefers_explicit_id_then_raw_collection_id(self):
        self.assertEqual(
            MODULE.source_id({"id": 7, "raw_collection_id": "x"}, "k"), 7
        )
        self.assertEqual(
            MODULE.source_id({"raw_collection_id": "ENG-1"}, "k"), "ENG-1"
        )
        self.assertEqual(MODULE.source_id({}, "k"), "k")

    def test_report_aggregates_review_causes_and_flags(self):
        rows = [
            {"id": 1, "cleaning_record_id": "k1", "name": "齿轮", "knowledge_point": "gear"},
            {"id": 2, "cleaning_record_id": "k2", "name": "边界概念", "knowledge_point": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            outputs = MODULE.known_output_paths(root / "out")
            outputs["db"].parent.mkdir(parents=True)
            connection = MODULE.init_db(outputs["db"])
            MODULE.save_results(
                connection,
                [
                    {
                        "key": "k1",
                        "result": {
                            "decision": "keep",
                            "confidence": 0.95,
                            "policy_version": MODULE.MODEL_POLICY_VERSION,
                            "name": "齿轮",
                            "knowledge_point": "gear",
                            "field_results": {
                                "name": {"decision": "keep", "reason": "有效"},
                                "knowledge_point": {"decision": "keep", "reason": "有效"},
                            },
                            "repair_validation": {
                                "titles": {
                                    "name": {"type": "empty"},
                                    "knowledge_point": {"type": "empty"},
                                },
                                "content": {},
                            },
                            "quality_flags": [],
                        },
                    },
                    {
                        "key": "k2",
                        "result": {
                            "decision": "review",
                            "confidence": 0.6,
                            "policy_version": MODULE.MODEL_POLICY_VERSION,
                            "quality_flags": [
                                "below_confidence_threshold",
                                "incomplete_review_contract",
                            ],
                        },
                    },
                ],
            )
            summary = MODULE.finalize(input_path, connection, outputs, 0, 0)
            connection.close()
        self.assertEqual(summary["review_causes"]["local_low_confidence"], 1)
        self.assertEqual(summary["quality_flags"]["below_confidence_threshold"], 1)
        self.assertIn("name:keep", summary["field_result_counts"])
        self.assertEqual(summary["missing_checkpoint_result"], 0)

    def test_strict_checkpoint_fails_and_reports_missing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            input_path.write_text(
                json.dumps(
                    {"id": 1, "cleaning_record_id": "k1", "name": "齿轮"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            outputs = MODULE.known_output_paths(root / "out")
            outputs["db"].parent.mkdir(parents=True)
            connection = MODULE.init_db(outputs["db"])
            with self.assertRaises(RuntimeError):
                MODULE.finalize(input_path, connection, outputs, 0, 0, True)
            summary = MODULE.finalize(input_path, connection, outputs, 0, 0)
            connection.close()
        self.assertEqual(summary["missing_checkpoint_result"], 1)
        self.assertEqual(summary["review_causes"]["api_error_after_retries"], 1)


if __name__ == "__main__":
    unittest.main()
