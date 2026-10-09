"""验证来源充分性协议与高置信度抽检，不依赖某本书或特定实体。"""

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from test_verification import VerificationClient

from book_extractor.markdown import Unit
from book_extractor.models import Finding, Record
from book_extractor.synthesis import _verify_records, verification_reason


class EvidenceSupportTests(unittest.TestCase):
    """矛盾响应必须拒绝；模型自报高分仍有独立回源检查机会。"""

    def test_support_contract(self) -> None:
        """不足必须null且无条件；支持必须有释义；正确保留假说可以high。"""
        value = dict(
            name="原对象",
            aliases=[],
            category="概念",
            support_reason="来源仅点名",
            definition_supported=False,
            definition=None,
            confidence="high",
            conditions=[],
            issues=[],
            evidence_ids=["u1"],
        )
        self.assertIsNone(Finding.model_validate(value).definition)
        for change in (
            {"definition": "文中提及的一个对象"},
            {"conditions": ["条件"]},
            {"definition_supported": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Finding.model_validate({**value, **change})
        result = Finding.model_validate(
            {
                **value,
                "support_reason": "作者明确提出该假说",
                "definition_supported": True,
                "definition": "作者提出该对象可能影响传播。",
            }
        )
        self.assertEqual(result.confidence, "high")
        fields = list(Finding.model_fields)
        self.assertLess(
            fields.index("support_reason"), fields.index("definition_supported")
        )
        self.assertLess(
            fields.index("definition_supported"), fields.index("definition")
        )

    def test_high_sample_is_stable_and_can_remove_unsupported_definition(self) -> None:
        """抽样与run无关；高分样本复核可置空，不能只降低评分却交付无依据释义。"""
        base = dict(
            name="原对象",
            aliases=[],
            category="概念",
            support_reason="错误的初次判断",
            definition_supported=True,
            definition="无依据的释义",
            confidence="high",
            conditions=[],
            issues=[],
            evidence_ids=["u1"],
            book_id="book",
            run_id="first",
            record_id="record",
            scope=[],
        )
        population = [Record(**base, candidate_ids=[str(i)]) for i in range(100)]
        selected = [r for r in population if verification_reason(r)]
        self.assertTrue(selected)
        self.assertLess(len(selected), len(population))
        record = selected[0]
        self.assertEqual(verification_reason(record), "high_confidence_sample")
        self.assertEqual(
            verification_reason(record.model_copy(update={"run_id": "second"})),
            verification_reason(record),
        )
        answer = Finding(
            **{k: getattr(record, k) for k in Finding.model_fields}
        ).model_copy(
            update={
                "definition_supported": False,
                "support_reason": "只有名称提及",
                "definition": None,
            }
        )
        client = VerificationClient(answer)
        with tempfile.TemporaryDirectory() as folder:
            revised, errors = _verify_records(
                [record],
                {"u1": Unit("u1", "原对象", 1, 1, [], "paragraph")},
                client,
                Path(folder),
            )
        self.assertFalse(errors)
        self.assertIsNone(revised[0].definition)
        self.assertEqual(len(client.payloads), 1)
        self.assertTrue(
            next(e for e in client.events if e["event"] == "verification_result")[
                "definition_removed"
            ]
        )


if __name__ == "__main__":
    unittest.main()
