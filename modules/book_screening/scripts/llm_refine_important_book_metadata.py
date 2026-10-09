from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import heapq
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


DEFAULT_API_URL = (
    "http://jb-aionlineinferenceservice-160662987482878464-8000-nhss-job."
    "v5000-prod.nhss.zhejianglab.com/v1/chat/completions"
)
DEFAULT_MODEL = "/mnt/si002991n0no/default/model/Qwen/Qwen3.8-27B"
DECISIONS = {"KEEP", "REVIEW", "DROP"}
PROMPT_FIELDS = (
    "record_id",
    "title",
    "subject1",
    "subject2",
    "subject3",
    "content_category",
    "content_genre",
    "usage_type",
    "education_stage",
    "audience_level",
    "abstract",
    "description",
    "keywords",
)
FIELD_LIMITS = {
    "record_id": 40,
    "title": 300,
    "subject1": 160,
    "subject2": 160,
    "subject3": 160,
    "content_category": 120,
    "content_genre": 120,
    "usage_type": 160,
    "education_stage": 120,
    "audience_level": 120,
    "abstract": 1200,
    "description": 1200,
    "keywords": 800,
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())


def _list_text(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(_text(item) for item in value if _text(item))
    return _text(value)


def _boundary_line(boundary: dict[str, Any], *keys: str) -> str:
    parts = [_list_text(boundary.get(key)) for key in keys]
    return "；".join(part for part in parts if part) or "未提供"


def build_system_prompt(config: dict[str, Any]) -> str:
    subject = _text(config.get("subject_name"))
    boundary = config.get("boundary")
    if not subject or not isinstance(boundary, dict):
        raise ValueError("学科附件必须包含 subject_name 和 boundary 对象")
    core = _boundary_line(
        boundary,
        "core_scope",
        "aliases",
        "direct_subject_labels",
        "strong_terms",
        "weak_terms",
        "cooccurrence_groups",
    )
    adjacent = _boundary_line(
        boundary, "accepted_adjacent", "adjacent_subject_labels"
    )
    excluded = _boundary_line(boundary, "excluded_scope", "exclude_phrases")
    return f"""你是学术书源元数据审核员。请逐本判断书籍是否值得进入“{subject}”知识点抽取的正文审核阶段。

学科边界附件：
- 核心范围：{core}
- 可接受相邻范围：{adjacent}
- 明确排除范围：{excluded}

只依据每本书自身的 title、subject1/2/3、content_category、content_genre、usage_type、education_stage、audience_level、abstract、description、keywords 判断，不得把同批其他书的信息串用。

判断维度：
1. subject_fit：core、acceptable_adjacent、weak_or_incidental、outside、unclear。
2. knowledge_extraction_fit：high、medium、low、unclear。重点看是否系统讲解概念、理论、方法、机制、分类或专业知识，而非只有叙事、观点或操作步骤。
3. book_type：textbook、scholarly_monograph、standard、handbook、reference、edited_collection、popular、fiction、biography、exam、product_manual、proceedings、other、unclear。

决策规则：
- KEEP：属于核心或可接受相邻范围，并且知识点抽取适用性为 high 或 medium。
- DROP：明显越界、仅偶然提到本学科，或明显属于小说、泛人物传记、应试资料、营销材料、单一产品操作手册、期刊/会议论文集等低价值书源。
- REVIEW：仅限元数据不足、相互矛盾，无法可靠判断的情况；不要把明显可判定的书放入 REVIEW。
- 不评价年份、文件格式和 OCR，这些由其他阶段处理。
- 摘要与题名冲突时，以更具体的摘要、简介和关键词证据为准。

只输出一个 JSON 对象，不要输出解释文字：
{{"results":[{{"record_id":"原值","decision":"KEEP|REVIEW|DROP","subject_fit":"枚举值","knowledge_extraction_fit":"枚举值","book_type":"枚举值","evidence_fields":["字段名"],"reason":"不超过60字的具体理由"}}]}}
必须为输入中的每个 record_id 返回且只返回一条结果。"""


def _prompt_record(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: _text(row.get(field))[: FIELD_LIMITS[field]]
        for field in PROMPT_FIELDS
    }


def build_user_prompt(rows: list[dict[str, Any]]) -> str:
    payload = [_prompt_record(row) for row in rows]
    return "请审核以下书目：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_objects(text: str) -> Iterable[Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        yield value


def parse_batch_response(text: str, expected_record_ids: list[str]) -> list[dict[str, Any]]:
    payload = None
    for candidate in _json_objects(text):
        if isinstance(candidate, dict) and isinstance(candidate.get("results"), list):
            payload = candidate
    if payload is None:
        raise ValueError("模型返回中没有可解析的 results JSON 对象")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in payload["results"]:
        if not isinstance(raw, dict):
            raise ValueError("results 中存在非对象元素")
        record_id = _text(raw.get("record_id"))
        if not record_id or record_id in by_id:
            raise ValueError("record_id 为空或重复")
        decision = _text(raw.get("decision")).upper()
        if decision not in DECISIONS:
            raise ValueError(f"{record_id} 的 decision 无效: {decision}")
        result = dict(raw)
        result["record_id"] = record_id
        result["decision"] = decision
        evidence = result.get("evidence_fields")
        result["evidence_fields"] = evidence if isinstance(evidence, list) else []
        by_id[record_id] = result

    expected = list(dict.fromkeys(expected_record_ids))
    missing = [record_id for record_id in expected if record_id not in by_id]
    extra = [record_id for record_id in by_id if record_id not in set(expected)]
    if missing:
        raise ValueError("模型结果缺少 record_id: " + ", ".join(missing))
    if extra:
        raise ValueError("模型结果包含额外 record_id: " + ", ".join(extra))
    return [by_id[record_id] for record_id in expected]


def _iter_csv(path: Path) -> Iterable[tuple[int, dict[str, str]]]:
    csv.field_size_limit(2_147_483_647)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader((line.replace("\x00", "") for line in handle))
        for index, row in enumerate(reader, 1):
            yield index, row


def select_deterministic_sample(path: Path, size: int, seed: int) -> list[tuple[int, dict[str, str]]]:
    if size <= 0:
        return list(_iter_csv(path))
    heap: list[tuple[int, int, dict[str, str]]] = []
    for index, row in _iter_csv(path):
        identity = _text(row.get("identifier")) or _text(row.get("title")) or str(index)
        digest = hashlib.sha256(f"{seed}|{identity}|{index}".encode("utf-8")).digest()
        score = int.from_bytes(digest[:8], "big")
        item = (-score, -index, row)
        if len(heap) < size:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    return sorted(((-neg_index, row) for _, neg_index, row in heap), key=lambda item: item[0])


def _chat_url(url: str) -> str:
    return url.rstrip("/") if url.rstrip("/").endswith("/v1/chat/completions") else url.rstrip("/") + "/v1/chat/completions"


def build_request_body(
    model: str,
    system_prompt: str,
    rows: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "stream": False,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_prompt(rows)},
        ],
    }


def call_batch(
    api_url: str,
    model: str,
    system_prompt: str,
    rows: list[dict[str, Any]],
    timeout: int,
    retries: int,
    max_tokens: int,
) -> list[dict[str, Any]]:
    expected_ids = [_text(row.get("record_id")) for row in rows]
    body = json.dumps(
        build_request_body(model, system_prompt, rows, max_tokens),
        ensure_ascii=False,
    ).encode("utf-8")
    error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                _chat_url(api_url), body, {"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            return parse_batch_response(content, expected_ids)
        except Exception as exc:  # network and model-format failures use the same bounded retry path
            error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"批次调用失败: {error}") from error


def call_batch_resilient(
    api_url: str,
    model: str,
    system_prompt: str,
    rows: list[dict[str, Any]],
    timeout: int,
    retries: int,
    max_tokens: int,
) -> list[dict[str, Any]]:
    try:
        return call_batch(
            api_url,
            model,
            system_prompt,
            rows,
            timeout,
            retries,
            max_tokens,
        )
    except RuntimeError as exc:
        if len(rows) == 1 or not isinstance(exc.__cause__, ValueError):
            raise
        midpoint = len(rows) // 2
        print(json.dumps({
            "event": "split_malformed_batch",
            "batch_size": len(rows),
            "record_ids": [_text(row.get("record_id")) for row in rows],
            "error": str(exc.__cause__),
        }, ensure_ascii=False), flush=True)
        return (
            call_batch_resilient(
                api_url, model, system_prompt, rows[:midpoint], timeout, retries, max_tokens
            )
            + call_batch_resilient(
                api_url, model, system_prompt, rows[midpoint:], timeout, retries, max_tokens
            )
        )


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return results
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                result = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"检查点第 {line_number} 行不是有效 JSON") from exc
            record_id = _text(result.get("record_id")) if isinstance(result, dict) else ""
            decision = _text(result.get("decision")).upper() if isinstance(result, dict) else ""
            if not record_id or record_id in results or decision not in DECISIONS:
                raise ValueError(f"检查点第 {line_number} 行的 record_id 或 decision 无效")
            result["record_id"] = record_id
            result["decision"] = decision
            evidence = result.get("evidence_fields")
            result["evidence_fields"] = evidence if isinstance(evidence, list) else []
            results[record_id] = result
    return results


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.subject_config.read_text(encoding="utf-8-sig"))
    system_prompt = build_system_prompt(config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.output_dir / "批次检查点.jsonl"
    sampled = select_deterministic_sample(args.input_csv, args.sample_size, args.seed)
    rows: list[dict[str, Any]] = []
    for source_index, source_row in sampled:
        row = dict(source_row)
        row["source_row_number"] = source_index
        row["record_id"] = f"r{source_index:09d}"
        rows.append(row)

    expected_ids = {row["record_id"] for row in rows}
    results = load_checkpoint(checkpoint_path)
    unexpected = sorted(set(results) - expected_ids)
    if unexpected:
        raise ValueError("检查点包含当前输入不存在的 record_id: " + ", ".join(unexpected[:10]))
    pending_rows = [row for row in rows if row["record_id"] not in results]
    batches = list(_batches(pending_rows, args.batch_size))
    if results:
        print(json.dumps({
            "event": "resume_from_checkpoint",
            "completed_records": len(results),
            "pending_records": len(pending_rows),
        }, ensure_ascii=False), flush=True)
    with checkpoint_path.open("a", encoding="utf-8") as checkpoint:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    call_batch_resilient,
                    args.api_url,
                    args.model,
                    system_prompt,
                    batch,
                    args.timeout,
                    args.retries,
                    args.max_tokens,
                ): batch
                for batch in batches
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                batch_results = future.result()
                for result in batch_results:
                    results[result["record_id"]] = result
                    checkpoint.write(json.dumps(result, ensure_ascii=False) + "\n")
                checkpoint.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(batches):
                    print(json.dumps({
                        "completed_batches": completed,
                        "total_batches": len(batches),
                        "completed_records": len(results),
                    }), flush=True)

    output_rows: list[dict[str, Any]] = []
    for row in rows:
        result = results[row["record_id"]]
        output_rows.append({
            "source_row_number": row["source_row_number"],
            "record_id": row["record_id"],
            "identifier": _text(row.get("identifier")),
            "title": _text(row.get("title")),
            "subject1": _text(row.get("subject1")),
            "subject2": _text(row.get("subject2")),
            "subject3": _text(row.get("subject3")),
            "content_category": _text(row.get("content_category")),
            "content_genre": _text(row.get("content_genre")),
            "decision": result["decision"],
            "subject_fit": _text(result.get("subject_fit")),
            "knowledge_extraction_fit": _text(result.get("knowledge_extraction_fit")),
            "book_type": _text(result.get("book_type")),
            "evidence_fields": " | ".join(_text(item) for item in result["evidence_fields"]),
            "reason": _text(result.get("reason")),
        })

    fields = list(output_rows[0]) if output_rows else ["record_id", "decision"]
    csv_path = args.output_dir / "书目大模型精筛结果.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    jsonl_path = args.output_dir / "书目大模型精筛结果.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in output_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (args.output_dir / "实际提示词.txt").write_text(system_prompt, encoding="utf-8")

    counts = Counter(row["decision"] for row in output_rows)
    summary = {
        "subject_name": config["subject_name"],
        "input_csv": str(args.input_csv),
        "sample_size": len(rows),
        "batch_size": args.batch_size,
        "workers": args.workers,
        "model": args.model,
        "checkpoint": str(checkpoint_path),
        "counts": {decision: counts.get(decision, 0) for decision in ("KEEP", "REVIEW", "DROP")},
        "outputs": {"csv": str(csv_path), "jsonl": str(jsonl_path)},
    }
    (args.output_dir / "书目大模型精筛汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用可插拔学科边界对重要书籍元数据做大模型精筛")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--subject-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-size", type=int, default=1000, help="0 表示全量；测试建议 1000")
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2400)
    args = parser.parse_args()
    if args.sample_size < 0 or args.batch_size < 1 or args.workers < 1:
        parser.error("sample-size 必须不小于0，batch-size 和 workers 必须大于0")
    return args


def main() -> int:
    args = parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
