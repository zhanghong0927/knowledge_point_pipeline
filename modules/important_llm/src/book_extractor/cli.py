"""提供单本与批量MD提取入口，限制并发并保存每本书的执行状态。"""

import argparse
import json
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any

from .llm import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    ExtractionCancelledError,
    LLMClient,
    fatal_service_error,
    load_service,
    merge_thinking_config,
)
from .pipeline import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_CHUNK_WORKERS,
    extract_book,
    write_json,
)
from .telemetry import TelemetryWriteError

DEFAULT_OUTPUT_DIRECTORY = Path("outputs/extraction")
DEFAULT_SERVICES_FILE = Path(".llm_services.json")
DEFAULT_BOOK_WORKERS = 1
DEFAULT_ADDITIONAL_MAX_CONNECTIONS = 16
BOOK_ADMISSION_INTERVAL_SECONDS = 1.0
SUMMARY_FIELDS = ("run_id", "status", "chunks", "candidates", "records")


def create_parser() -> argparse.ArgumentParser:
    """返回命令行解析器；运行参数在此集中声明，服务与模型默认读取配置文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path, help="单本UTF-8 Markdown文件")
    inputs.add_argument("--manifest", type=Path, help="含books数组的JSON清单")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--services", type=Path, default=DEFAULT_SERVICES_FILE)
    parser.add_argument("--service", help="覆盖配置中的默认服务")
    parser.add_argument(
        "--additional-service",
        action="append",
        default=[],
        help="按书目service启用额外服务",
    )
    parser.add_argument(
        "--additional-max-connections",
        type=int,
        default=DEFAULT_ADDITIONAL_MAX_CONNECTIONS,
    )
    parser.add_argument("--model", help="覆盖服务配置中的模型")
    parser.add_argument(
        "--resume-existing", action="store_true", help="校验后在现有逐书目录恢复"
    )
    parser.add_argument(
        "--compatible-implementation",
        action="append",
        default=[],
        help="允许原目录恢复的旧源码SHA256，可重复指定；其他配置必须相同",
    )
    parser.add_argument(
        "--transport-failure-limit",
        type=int,
        help="单次逻辑调用的连接及5xx故障上限，达到后交由后续补做",
    )
    parser.add_argument("--title")
    parser.add_argument("--subject", default="")
    parser.add_argument("--workers", type=int, default=DEFAULT_CHUNK_WORKERS)
    parser.add_argument("--book-workers", type=int, default=DEFAULT_BOOK_WORKERS)
    parser.add_argument(
        "--book-prefetch",
        type=int,
        default=0,
        help="全局请求队列不足时可额外加载的书数，限制驻留书籍内存",
    )
    parser.add_argument("--chunk-tokens", type=int, default=DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP读写阶段等待秒数，不是整个任务的墙钟上限",
    )
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument(
        "--thinking", action="store_true", default=None, help="覆盖配置并启用思考模式"
    )
    thinking.add_argument(
        "--no-thinking",
        dest="thinking",
        action="store_false",
        help="覆盖配置并禁用思考模式",
    )
    return parser


def _resume_runs(
    books: list[dict[str, Any]], base: Path, output: Path
) -> dict[int, dict[str, Any]]:
    """按书籍身份或源路径唯一查找原运行，返回输入序号到manifest的映射。

    这里只发现目录，不信任其complete状态；正文、配置、结果的完整校验由
    extract_book执行。重复目录及同一路径的身份冲突必须在发送请求前拒绝。
    """
    manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in output.glob("*/manifest.json")
    ]
    result = {}
    claimed = set()
    for index, book in enumerate(books):
        source = (base / book["local_name"]).resolve()
        matches = [
            row
            for row in manifests
            if (
                book.get("book_id") is not None
                and row.get("book_id") == book["book_id"]
            )
            or (row.get("source") and Path(row["source"]).resolve() == source)
        ]
        if len(matches) > 1:
            raise ValueError("Multiple existing runs match one book")
        if not matches:
            continue
        previous = matches[0]
        run_id = previous.get("run_id")
        if not isinstance(run_id, str) or run_id in claimed:
            raise ValueError("Existing run identity is missing or duplicated")
        if book.get("book_id") not in {None, previous.get("book_id")}:
            raise ValueError("Existing source belongs to a different book")
        claimed.add(run_id)
        result[index] = previous
    return result


def run_books(
    books: list[dict[str, Any]],
    base: Path,
    args: argparse.Namespace,
    client: LLMClient,
    *,
    clients: dict[str, LLMClient] | None = None,
) -> list[dict[str, Any]]:
    """按有界并发处理书目，返回保持输入顺序的逐书紧凑摘要。

    参数：
        books：包含local_name及可选书名、学科、book_id的书目列表。
        base：解析书目相对文件路径的目录。
        args：已验证的CLI选项，提供输出目录和并发、分块参数。
        client：由调用者创建并负责关闭的共享模型客户端。
        clients：显式服务名到客户端的映射；书目未指定service时使用client。

    批次只保留状态、计数及完整 manifest 的路径，不长期持有每本完整统计。
    每本失败都保存异常类型而不暴露异常正文；主线程更新batch.json。
    输出目录或批次摘要写入失败时向调用者抛错，不伪装为提取成功。
    """
    summaries: dict[int, dict[str, Any]] = {}
    args.output.mkdir(parents=True, exist_ok=True)
    stopped = Event()
    named_clients = clients or {}
    all_clients = list(
        {id(item): item for item in [client, *named_clients.values()]}.values()
    )
    selected_clients = []
    for book in books:
        name = book.get("service")
        if name is not None and name not in named_clients:
            raise ValueError("Book selects an unavailable service")
        selected_clients.append(client if name is None else named_clients[name])
    previous_runs = (
        _resume_runs(books, base, args.output)
        if getattr(args, "resume_existing", False)
        else {}
    )
    for index, previous in previous_runs.items():
        selected = selected_clients[index]
        previous_config = previous.get("config", {})
        if (
            previous_config.get("model") != selected.model
            or previous_config.get("service") != selected.identity
        ):
            raise ValueError("Existing book cannot change model or service")
        # 保留旧计数和目录，但在完整验证完成前不把旧complete声明为当前成功。
        summaries[index] = {
            **{key: previous.get(key) for key in SUMMARY_FIELDS},
            "book_id": previous.get("book_id"),
            "source": previous.get("source"),
            "manifest": str(
                (args.output / previous["run_id"] / "manifest.json").resolve()
            ),
            "status": "pending_validation",
            "previous_status": previous.get("status"),
        }

    def cancel_all() -> None:
        """向全部服务传播致命取消，避免另一个客户端继续发送新请求。"""
        stopped.set()
        for active_client in all_clients:
            if hasattr(active_client, "cancel"):
                active_client.cancel()

    def unstarted(book: dict[str, Any]) -> dict[str, Any]:
        """记录因批次致命错误未开始的书目，不将其算成已执行失败或成功。"""
        return {
            "source": str((base / book["local_name"]).resolve()),
            "status": "not_started",
            "error": "batch_fatal_error",
        }

    def run_one(item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        """处理item中的序号与书目，返回摘要；单本异常转为失败状态以继续其他书。"""
        index, book = item
        if stopped.is_set():
            return {**summaries.get(index, {}), **unstarted(book)}
        source = base / book["local_name"]
        if book.get("language") not in {None, "zh", "en", "zh-en"}:
            # 语言筛查由书目准备阶段完成；明确跳过不能混入提取成功或失败计数。
            return {
                "source": str(source.resolve()),
                "book_id": book.get("book_id"),
                "status": "skipped",
                "reason": "language_not_supported",
                "language": book["language"],
                "records": 0,
            }
        title = book.get("title") or source.stem
        print(f"[{index + 1}/{len(books)}] {title}", flush=True)
        try:
            resume_options = {}
            if index in previous_runs:
                resume_options = {
                    "resume_run_id": previous_runs[index]["run_id"],
                    "compatible_implementations": tuple(
                        getattr(args, "compatible_implementation", [])
                    ),
                }
            result = extract_book(
                source,
                args.output,
                selected_clients[index],
                title=book.get("title"),
                subject=book.get("subject", ""),
                book_id=book.get("book_id"),
                expected_sha256=book.get("sha256"),
                chunk_tokens=args.chunk_tokens,
                workers=args.workers,
                **resume_options,
            )
        except Exception as error:
            if isinstance(
                error, (TelemetryWriteError, ExtractionCancelledError, OSError)
            ) or (fatal_service_error(error)):
                cancel_all()
            failure = {
                "source": str(source),
                "status": "interrupted"
                if isinstance(error, ExtractionCancelledError)
                else "failed",
                "error": type(error).__name__,
            }
            print(json.dumps(failure), flush=True)
            return failure
        print(json.dumps({key: result[key] for key in SUMMARY_FIELDS}), flush=True)
        directory = Path(result.get("directory", args.output / result["run_id"]))
        summary = {
            **{key: result[key] for key in SUMMARY_FIELDS},
            "source": str(source.resolve()),
            "manifest": str((directory / "manifest.json").resolve()),
            "error_count": len(result.get("errors", [])),
        }
        if "book_id" in result:
            summary["book_id"] = result["book_id"]
        return summary

    prefetch = getattr(args, "book_prefetch", 0)
    # 同步流水线保留有界线程；驻留书数限定内存，每书就绪工作由workers限定。
    # 真正HTTP并发统一由客户端轮转队列控制，不再按书籍分割固定份额。
    with ThreadPoolExecutor(max_workers=args.book_workers + prefetch) as pool:
        # complete先验证，partial最后补做；同一优先级按服务交错入场，
        # 避免所有驻留书都占用主服务而让额外服务一直空闲。导出保留输入顺序。
        scheduled = []
        for priority in range(3):
            service_queues: dict[int, deque[tuple[int, dict[str, Any]]]] = {}
            for index, book in enumerate(books):
                book_priority = {"complete": 0, "partial": 2, "failed": 2}.get(
                    previous_runs.get(index, {}).get("status"), 1
                )
                if book_priority == priority:
                    service_queues.setdefault(
                        id(selected_clients[index]), deque()
                    ).append((index, book))
            while any(service_queues.values()):
                for queue in service_queues.values():
                    if queue:
                        scheduled.append(queue.popleft())
        items = list(scheduled)
        pending = {}

        def next_book() -> tuple[int, dict[str, Any]] | None:
            """优先给当前驻留书少的服务补位，同服务仍保持恢复优先级和原顺序。

            静态交错只能保证首发公平；快服务完成后必须重算，否则慢服务会
            逐渐占满全部驻留名额。只比较仍有待办书的服务，空闲名额可被借用。
            """
            if not items:
                return None
            resident = Counter(
                id(selected_clients[index]) for index in pending.values()
            )
            position = min(
                range(len(items)),
                key=lambda position: resident[id(selected_clients[items[position][0]])],
            )
            return items.pop(position)

        try:
            for _ in range(min(args.book_workers, len(books))):
                item = next_book()
                pending[pool.submit(run_one, item)] = item[0]
            while pending:
                finished, _ = wait(
                    pending,
                    return_when=FIRST_COMPLETED,
                    timeout=BOOK_ADMISSION_INTERVAL_SECONDS if prefetch else None,
                )
                for future in finished:
                    summaries[pending[future]] = future.result()
                    pending.pop(future)
                target = args.book_workers
                if prefetch:
                    states = [item.request_queue_state for item in all_clients]
                    if any(
                        state["active"] + state["waiting"] < state["limit"]
                        for state in states
                    ):
                        # 一次只额外加载一本；队列足够时不为预取继续增加驻留内存。
                        target = min(
                            args.book_workers + prefetch,
                            max(args.book_workers, len(pending) + 1),
                        )
                # 同轮先收齐结果，再补位，避免已有致命失败仍启动下一本。
                while len(pending) < target and not stopped.is_set():
                    item = next_book()
                    if item is None:
                        break
                    pending[pool.submit(run_one, item)] = item[0]
                if stopped.is_set():
                    for index, book in items:
                        summaries[index] = {
                            **summaries.get(index, {}),
                            **unstarted(book),
                        }
                    items.clear()
                if not finished:
                    continue
                write_json(
                    args.output / "batch.json",
                    {
                        "books": [summaries[index] for index in sorted(summaries)],
                        "statistics": (
                            "Per-book execution events and summaries; "
                            "resumed runs retain earlier execution logs."
                        ),
                    },
                )
        except BaseException:
            # 必须在线程池 __exit__ 等待前通知所有书及其SDK重试停止发新请求。
            cancel_all()
            for future, index in pending.items():
                try:
                    summaries[index] = future.result()
                except BaseException as error:
                    summaries[index] = {
                        "source": str(base / books[index]["local_name"]),
                        "status": "interrupted",
                        "error": type(error).__name__,
                    }
            for index, book in items:
                summaries[index] = {**summaries.get(index, {}), **unstarted(book)}
            write_json(
                args.output / "batch.json",
                {"books": [summaries[index] for index in sorted(summaries)]},
            )
            raise
    return [summaries[index] for index in sorted(summaries)]


def main() -> None:
    """读取命令行与服务配置并启动提取，保证结束时关闭模型客户端。

    单本输入或书目清单二选一；不完整批次退出码为1，参数错误由argparse报告。
    不接收入参、不返回业务结果，提取结果和运行摘要写入指定输出目录。
    """
    parser = create_parser()
    args = parser.parse_args()
    if args.workers < 1 or args.book_workers < 1 or args.book_prefetch < 0:
        parser.error("workers must be positive and book-prefetch nonnegative")
    if args.additional_max_connections < 1 or (
        args.transport_failure_limit is not None and args.transport_failure_limit < 1
    ):
        parser.error(
            "additional connections and transport failure limit must be positive"
        )
    if args.manifest:
        books = json.loads(args.manifest.read_text(encoding="utf-8"))["books"]
        base = args.manifest.parent
    else:
        books = [
            {
                "local_name": str(args.input.resolve()),
                "title": args.title,
                "subject": args.subject,
            },
        ]
        base = Path.cwd()
    # 清单格式必须整体有效后才创建客户端，避免后半清单损坏时前半已产生费用。
    if not isinstance(books, list) or not books:
        parser.error("books must be a nonempty list")
    if any(
        not isinstance(book, dict)
        or not isinstance(book.get("local_name"), str)
        or not book["local_name"].strip()
        for book in books
    ):
        parser.error("every book must have a nonempty local_name")
    if any(
        book.get("service") is not None
        and (not isinstance(book["service"], str) or not book["service"].strip())
        for book in books
    ):
        parser.error("book service must be a nonempty configured name")
    primary_name = args.service
    if primary_name is None and (
        args.additional_service or any(book.get("service") for book in books)
    ):
        configured = json.loads(args.services.read_text(encoding="utf-8-sig"))
        primary_name = configured.get("default")
        if primary_name is None and len(configured["services"]) == 1:
            primary_name = next(iter(configured["services"]))
    if len(set(args.additional_service)) != len(args.additional_service) or (
        primary_name is not None and primary_name in args.additional_service
    ):
        parser.error("additional services must be distinct from each other and primary")
    available = {primary_name, *args.additional_service}
    if any(
        book.get("service") not in available for book in books if book.get("service")
    ):
        parser.error("book selects a service not enabled for this run")
    services = [
        (primary_name, load_service(args.services, args.service, args.model)),
        *[
            (name, load_service(args.services, name))
            for name in args.additional_service
        ],
    ]
    created: list[LLMClient] = []
    named_clients: dict[str, LLMClient] = {}
    try:
        for index, (name, configuration) in enumerate(services):
            configuration = dict(configuration)
            extra_body = merge_thinking_config(
                configuration.pop("extra_body", None), args.thinking
            )
            client = LLMClient(
                **configuration,
                max_output_tokens=args.max_output_tokens,
                timeout=args.timeout,
                max_connections=args.max_connections
                if index == 0
                else args.additional_max_connections,
                extra_body=extra_body,
                transport_failure_limit=args.transport_failure_limit,
            )
            created.append(client)
            if name is not None:
                named_clients[name] = client
        summaries = run_books(books, base, args, created[0], clients=named_clients)
    finally:
        for client in created:
            client.close()
    if any(book["status"] not in {"complete", "skipped"} for book in summaries):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
