"""以同步Python API接入单书提取；真实执行会调用所配置的模型服务。"""

import argparse
import json
from pathlib import Path

from book_extractor.llm import LLMClient, load_service
from book_extractor.pipeline import extract_book


def main() -> None:
    """读取路径参数，调用单书提取并打印紧凑结果；任何情况下均关闭客户端。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", type=Path, default=Path(".llm_services.json"))
    parser.add_argument("--manifest", type=Path, default=Path("examples/books.json"))
    parser.add_argument("--book-index", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("work/runs"))
    args = parser.parse_args()
    book = json.loads(args.manifest.read_text(encoding="utf-8"))["books"][
        args.book_index
    ]
    client = LLMClient(
        **load_service(args.services),
        max_connections=4,
        max_output_tokens=8192,
        transport_failure_limit=2,
        context_limit=32768,
    )
    try:
        result = extract_book(
            args.manifest.parent / book["local_name"],
            args.output,
            client,
            book_id=book["book_id"],
            title=book["title"],
            subject=book.get("subject", ""),
            expected_sha256=book.get("sha256"),
            chunk_tokens=4000,
            workers=4,
        )
    finally:
        client.close()
    print(
        json.dumps(
            {k: result[k] for k in ("book_id", "run_id", "status", "records")},
            ensure_ascii=False,
        )
    )
    if result["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
