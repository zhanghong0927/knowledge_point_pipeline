"""验证清洗交付筛选不会把失败记录作为完整成果。"""

import importlib.util
from pathlib import Path


def test_completed_records_survive_unrelated_book_errors() -> None:
    """保留partial书中的独立成功项，排除有释义但失败或延后的关联项。"""
    path = Path(__file__).parents[1] / "scripts/export_cleaning_delivery.py"
    spec = importlib.util.spec_from_file_location("cleaning_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = {
        "record_id": "r1",
        "definition": "释义",
        "candidate_ids": ["c001-a"],
        "issues": [],
    }
    errors = [{"chunk_id": "c002", "error": "DeferredCallError"}]
    assert module.exclusion_reasons(row, errors) == []
    assert module.exclusion_reasons({**row, "definition": " "}, []) == [
        "definition_missing"
    ]
    assert module.exclusion_reasons(
        {**row, "issues": ["verification_failed:APIStatusError"]}, []
    )
    assert module.exclusion_reasons(
        {**row, "issues": ["cross_batch_grouping_deferred"]}, []
    )
    assert module.exclusion_reasons(
        row,
        [
            {
                "candidate_ids": ["c001-a"],
                "issue": "synthesis_failed:MemberValidation",
                "severity": "error",
            }
        ],
    )
    assert module.exclusion_reasons(
        row, [{"chunk_id": "c001", "error": "DeferredCallError"}]
    )


if __name__ == "__main__":
    test_completed_records_survive_unrelated_book_errors()
