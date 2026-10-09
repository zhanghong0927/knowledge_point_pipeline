import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_qualitative_policy import qualitative_result
from test_dictionary_md_audit import AUDIT


def source_samples():
    return [
        {
            "sample_no": 1,
            "text": "## Social capital\nSocial capital refers to resources embedded in social networks.",
            "context_before": "",
            "context_after": "",
            "preceding_entry": "",
        },
        {
            "sample_no": 2,
            "text": "## Field theory\nField theory explains relations between positions and forms of capital.",
            "context_before": "",
            "context_after": "",
            "preceding_entry": "",
        },
        {
            "sample_no": 3,
            "text": "## Jane Addams\nHer settlement work shaped methods for studying urban poverty.",
            "context_before": "",
            "context_after": "",
            "preceding_entry": "",
        },
        {
            "sample_no": 4,
            "text": "## A hit song\nIt reached number one in the Billboard chart in 1982.",
            "context_before": "",
            "context_after": "",
            "preceding_entry": "",
        },
    ]


def challenge_response(recommendation="PASS"):
    return {
        "recommendation": recommendation,
        "dominant_payload": "conceptual_explanation",
        "summary": "Contains independent entries with substantive explanations.",
        "evidence": [
            {
                "sample_no": 1,
                "headword": "Social capital",
                "anchor": "resources embedded in social networks",
                "claim_type": "concept_definition",
                "knowledge_claim": "Defines social capital through network resources.",
            },
            {
                "sample_no": 2,
                "headword": "Field theory",
                "anchor": "relations between positions and forms of capital",
                "claim_type": "feature_or_relation_analysis",
                "knowledge_claim": "Explains relations among positions and capital.",
            },
            {
                "sample_no": 3,
                "headword": "Jane Addams",
                "anchor": "shaped methods for studying urban poverty",
                "claim_type": "entity_with_analysis",
                "knowledge_claim": "Connects a scholar to a disciplinary method.",
            },
        ],
    }


class PassChallengeTests(unittest.TestCase):
    def test_clean_concept_dictionary_does_not_trigger_challenge(self):
        result = qualitative_result()
        result["sample_structure_counts"] = {
            "compact_entries": 14, "long_entry": 0, "long_article": 0,
            "frontmatter_index": 2, "unclear": 0,
        }
        result["problems"] = []
        self.assertEqual(AUDIT.pass_challenge_reasons(result, "Dictionary of Social Concepts"), [])

    def test_risky_title_and_structure_trigger_challenge(self):
        result = qualitative_result()
        result["sample_structure_counts"] = {
            "compact_entries": 10, "long_entry": 0, "long_article": 2,
            "frontmatter_index": 4, "unclear": 0,
        }
        reasons = AUDIT.pass_challenge_reasons(result, "Amplified Encyclopedia of Music Trivia")
        self.assertIn("risky_title", reasons)
        self.assertIn("long_article_samples", reasons)

    def test_ocr_problem_or_frontmatter_alone_does_not_trigger_content_challenge(self):
        result = qualitative_result()
        result["sample_structure_counts"] = {
            "compact_entries": 8, "long_entry": 0, "long_article": 0,
            "frontmatter_index": 8, "unclear": 0,
        }
        result["problems"] = [{"sample_no": 3, "problem_type": "格式噪声"}]
        self.assertEqual(
            AUDIT.pass_challenge_reasons(result, "Encyclopedia of Social Theory"), []
        )

    def test_model_recovered_title_can_trigger_challenge(self):
        result = qualitative_result()
        result["title_guess"] = "The New Biographical Dictionary of Film"
        result["sample_structure_counts"] = {"compact_entries": 12}
        result["problems"] = []
        self.assertIn(
            "risky_title", AUDIT.pass_challenge_reasons(result, "THE NEW BIOGRAPHICAL")
        )

    def test_three_verified_strong_entries_keep_pass(self):
        challenge = AUDIT.normalize_pass_challenge(challenge_response(), source_samples())
        result = AUDIT.apply_pass_challenge(qualitative_result(), challenge, ["risky_title"])
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["pass_challenge_strong_evidence_count"], 3)

    def test_entity_analysis_counts_as_strong_evidence(self):
        challenge = AUDIT.normalize_pass_challenge(challenge_response(), source_samples())
        claim_types = {item["claim_type"] for item in challenge["verified_evidence"]}
        self.assertIn("entity_with_analysis", claim_types)
        self.assertEqual(challenge["strong_evidence_count"], 3)

    def test_trivia_evidence_moves_pass_to_review_not_drop(self):
        response = challenge_response("DROP")
        response["dominant_payload"] = "trivia_rankings"
        response["evidence"] = [
            {
                "sample_no": 4,
                "headword": "A hit song",
                "anchor": "reached number one in the Billboard chart",
                "claim_type": "ranking_or_trivia",
                "knowledge_claim": "Reports a chart rank.",
            }
        ]
        challenge = AUDIT.normalize_pass_challenge(response, source_samples())
        result = AUDIT.apply_pass_challenge(qualitative_result(), challenge, ["risky_title"])
        self.assertEqual(result["decision"], "REVIEW")
        self.assertTrue(result["decision_adjusted_by_pass_challenge"])

    def test_invalid_quote_is_not_counted_and_requires_review(self):
        response = challenge_response()
        response["evidence"][2]["anchor"] = "text not present in the sample"
        challenge = AUDIT.normalize_pass_challenge(response, source_samples())
        self.assertEqual(challenge["strong_evidence_count"], 2)
        self.assertEqual(len(challenge["unverified_evidence"]), 1)
        result = AUDIT.apply_pass_challenge(copy.deepcopy(qualitative_result()), challenge, ["reported_problem"])
        self.assertEqual(result["decision"], "REVIEW")

    def test_ellipsis_anchor_is_verified_when_all_parts_appear_in_order(self):
        response = challenge_response()
        response["evidence"][0]["anchor"] = (
            "resources embedded...social networks"
        )
        challenge = AUDIT.normalize_pass_challenge(response, source_samples())
        self.assertEqual(challenge["strong_evidence_count"], 3)
        self.assertEqual(challenge["unverified_evidence"], [])

    def test_challenge_sampler_prioritizes_reported_and_long_article_samples(self):
        item = {"samples": [
            {"sample_no": number, "text": f"sample {number}"}
            for number in range(1, 13)
        ]}
        result = qualitative_result()
        result["sample_structure_labels"][3]["structure"] = "long_article"
        result["problems"] = [{"sample_no": 7, "anchor": "sample 7"}]
        selected = AUDIT.select_pass_challenge_samples(item, result, max_samples=8)
        numbers = [sample["sample_no"] for sample in selected]
        self.assertEqual(len(numbers), 8)
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertIn(4, numbers)
        self.assertIn(7, numbers)

    def test_overlay_requires_every_triggered_challenge(self):
        primary = {
            "ok": True,
            "source_path": "/books/trivia.md",
            "input_title": "Music Trivia",
            "result": qualitative_result(),
        }
        manifest = {"source_path": "/books/trivia.md", "title": "Music Trivia", "samples": source_samples()}
        with self.assertRaisesRegex(ValueError, "missing pass challenge"):
            AUDIT.overlay_pass_challenges(
                {primary["source_path"]: primary}, {}, {manifest["source_path"]: manifest}
            )

    def test_overlay_demotes_only_the_challenged_pass(self):
        primary = {
            "ok": True,
            "source_path": "/books/trivia.md",
            "input_title": "Music Trivia",
            "result": qualitative_result(),
        }
        manifest = {"source_path": "/books/trivia.md", "title": "Music Trivia", "samples": source_samples()}
        weak = challenge_response("DROP")
        weak["dominant_payload"] = "trivia_rankings"
        weak["evidence"] = [{
            "sample_no": 4,
            "headword": "A hit song",
            "anchor": "reached number one in the Billboard chart",
            "claim_type": "ranking_or_trivia",
            "knowledge_claim": "Reports a chart rank.",
        }]
        challenge = {
            "ok": True,
            "source_path": primary["source_path"],
            "challenge": AUDIT.normalize_pass_challenge(weak, source_samples()),
        }
        merged = AUDIT.overlay_pass_challenges(
            {primary["source_path"]: primary},
            {challenge["source_path"]: challenge},
            {manifest["source_path"]: manifest},
        )
        self.assertEqual(merged[primary["source_path"]]["result"]["decision"], "REVIEW")
        self.assertEqual(primary["result"]["decision"], "PASS")

    def test_challenge_stage_calls_only_triggered_pass_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            subject = "测试学科"
            manifest_dir = output / "输入清单"
            primary_dir = output / subject / "模型原始结果"
            manifest_dir.mkdir(parents=True)
            primary_dir.mkdir(parents=True)
            records = [
                {
                    "short_id": "T001", "subject": subject,
                    "source_path": "/books/trivia.md", "relative_path": "trivia.md",
                    "file_name": "trivia.md", "title": "Music Trivia",
                    "samples": source_samples(),
                },
                {
                    "short_id": "T002", "subject": subject,
                    "source_path": "/books/concepts.md", "relative_path": "concepts.md",
                    "file_name": "concepts.md", "title": "Dictionary of Social Concepts",
                    "samples": source_samples(),
                },
            ]
            (manifest_dir / f"{subject}.json").write_text(
                json.dumps({"records": records}), encoding="utf-8"
            )
            primary_rows = []
            for record in records:
                result = qualitative_result()
                result["sample_structure_counts"] = {
                    "compact_entries": 12, "long_entry": 0, "long_article": 0,
                    "frontmatter_index": 0, "unclear": 0,
                }
                result["problems"] = []
                primary_rows.append({
                    "ok": True, "source_path": record["source_path"],
                    "input_title": record["title"], "result": result,
                })
            (primary_dir / "progress.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in primary_rows), encoding="utf-8"
            )

            calls = []

            def fake_request(*args, **kwargs):
                item = args[2]
                calls.append(item["source_path"])
                return {
                    "ok": True,
                    "short_id": item["short_id"],
                    "subject": item["subject"],
                    "source_path": item["source_path"],
                    "challenge": AUDIT.normalize_pass_challenge(
                        challenge_response(), item["challenge_samples"]
                    ),
                }

            with mock.patch.object(AUDIT, "request_pass_challenge", side_effect=fake_request):
                summary = AUDIT.run_pass_challenge_subject(
                    output, {"name": subject}, "http://example/v1/chat/completions",
                    "model", workers=2, timeout=10, retries=0, max_tokens=1000,
                )
            self.assertEqual(calls, ["/books/trivia.md"])
            self.assertEqual(summary["triggered"], 1)
            progress = AUDIT.load_progress(
                output / subject / "模型反证复核" / "challenge_progress.jsonl"
            )
            self.assertEqual(set(progress), {"/books/trivia.md"})

    def test_materialize_uses_challenge_overlay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            subject = "测试学科"
            source = output / "trivia.md"
            source.write_text("sample", encoding="utf-8")
            record = {
                "short_id": "T001", "subject": subject,
                "source_path": str(source), "relative_path": source.name,
                "file_name": source.name, "title": "Music Trivia",
                "samples": source_samples(),
            }
            manifest_dir = output / "输入清单"
            primary_dir = output / subject / "模型原始结果"
            challenge_dir = output / subject / "模型反证复核"
            manifest_dir.mkdir(parents=True)
            primary_dir.mkdir(parents=True)
            challenge_dir.mkdir(parents=True)
            (manifest_dir / f"{subject}.json").write_text(
                json.dumps({"records": [record]}), encoding="utf-8"
            )
            primary_result = qualitative_result()
            primary_result["sample_structure_counts"] = {"compact_entries": 12}
            primary_result["problems"] = []
            primary = {
                "ok": True, "short_id": "T001", "subject": subject,
                "source_path": str(source), "relative_path": source.name,
                "file_name": source.name, "input_title": record["title"],
                "result": primary_result,
            }
            (primary_dir / "progress.jsonl").write_text(
                json.dumps(primary) + "\n", encoding="utf-8"
            )
            weak = challenge_response("REVIEW")
            weak["dominant_payload"] = "trivia_rankings"
            weak["evidence"] = []
            challenge = {
                "ok": True, "source_path": str(source),
                "challenge": AUDIT.normalize_pass_challenge(weak, source_samples()),
            }
            (challenge_dir / "challenge_progress.jsonl").write_text(
                json.dumps(challenge) + "\n", encoding="utf-8"
            )
            rows, _ = AUDIT.materialize_subject(
                output, {"name": subject}, generate_priority_workbook=False,
                pass_challenge=True,
            )
            self.assertEqual(rows[0]["decision"], "REVIEW")
            self.assertEqual(rows[0]["pass_challenge_recommendation"], "REVIEW")
            self.assertEqual(rows[0]["pass_challenge_reasons"], "risky_title")


if __name__ == "__main__":
    unittest.main()
