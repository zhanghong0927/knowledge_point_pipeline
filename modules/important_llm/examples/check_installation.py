"""离线检查依赖、原生分词、MD回拼、示例引用与Schema，不调用模型服务。"""

import hashlib
import json
from pathlib import Path

from book_extractor.markdown import Unit, chunk_units, parse_markdown
from book_extractor.models import Candidate, Finding, Record
from book_extractor.tokenization import get_tokenizer, use_tokenizer


def main() -> None:
    """使用包内分词文件检查中英文示例；失败抛异常，成功打印有界摘要。"""
    root = Path(__file__).resolve().parents[1]
    tokenizer = get_tokenizer(root / "data/tokenizers/qwen3.8-27b")
    rows = []
    with use_tokenizer(tokenizer):
        for path in sorted((root / "examples/books").glob("*.md")):
            text = path.read_bytes().decode("utf-8-sig")
            units = parse_markdown(text)
            chunks = chunk_units(units, chunk_tokens=4000)
            assert "".join(unit.text for unit in units) == text
            assert "".join(chunk.text for chunk in chunks) == text
            assert all(tokenizer.count_text(chunk.text) <= 4000 for chunk in chunks)
            rows.append({"file": path.name, "units": len(units), "chunks": len(chunks)})
    provenance = json.loads(
        (root / "examples/provenance.json").read_text(encoding="utf-8")
    )
    for sample in provenance["samples"]:
        tag = sample["tag"]
        units = json.loads(
            (root / f"examples/evidence/{tag}.units.json").read_text(encoding="utf-8")
        )
        for unit in units:
            Unit(**unit)
        context = {
            "evidence_ids": {u["id"] for u in units},
            "evidence_texts": {u["id"]: u["text"] for u in units},
        }
        record = Record.model_validate_json(
            (root / f"examples/records/{tag}.json").read_text(encoding="utf-8"),
            context=context,
        )
        # 当前示例必须实际包含新协议字段，不能靠Record的历史读取默认值通过。
        Finding.model_validate(
            {name: getattr(record, name) for name in Finding.model_fields},
            context=context,
        )
        assert record.definition_supported is True
        assert record.book_id == sample["original_book_id"]
        assert record.run_id == sample["original_run_id"]
        assert record.record_id == sample["record_id"]
        assert record.name == sample["name"]
        candidates = [
            Candidate.model_validate_json(line, context=context)
            for line in (root / f"examples/candidates/{tag}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert set(record.candidate_ids) == {c.candidate_id for c in candidates}
        excerpt = (root / f"examples/books/{tag}.md").read_bytes()
        assert hashlib.sha256(excerpt).hexdigest() == sample["excerpt_sha256"]
        assert record.name in excerpt.decode("utf-8") and record.definition
    print(
        json.dumps(
            {
                "offline_check": "passed",
                "books": rows,
                "tokenizer_sha256": tokenizer.fingerprint,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
