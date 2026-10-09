"""无损解析 Markdown，并组装携带来源引用且受 token 预算约束的核心块。

行号对应输入 MD 而非 PDF 页码；所有token预算使用本次模型的原生分词器。
"""

import re
from dataclasses import dataclass

from chonkie import RecursiveChunker, RecursiveLevel, RecursiveRules
from markdown_it import MarkdownIt

from .tokenization import get_tokenizer


@dataclass
class Unit:
    """不重叠的原文单元：保留文本、类型、标题路径和从 1 开始的闭区间行号。"""

    id: str
    text: str
    start_line: int
    end_line: int
    headings: list[str]
    kind: str


@dataclass
class Chunk:
    """核心文本及其来源引用；headings 是有序去重标题列表，不是单一章节路径。

    超长单元拆分后 ID 可重复出现；准确章节路径仍由 Unit 保存。
    """

    id: str
    unit_ids: list[str]
    text: str
    headings: list[str]


def token_count(text: str) -> int:
    """用本次模型分词器计算text的token数，不添加聊天模板前后缀。"""
    return get_tokenizer().count_text(text)


def parse_markdown(text: str) -> list[Unit]:
    """将 text 的全部字符分配到稳定的来源单元并返回有序列表。

    按 Markdown 顶层 map 回取原文，保留空白和引用定义；
    列表、代码和闭合展示公式尽量完整。
    空输入返回空列表，无法精确回拼原文时抛出 ValueError。
    """
    if not text:
        return []
    # splitlines() also treats Unicode paragraph separators as newlines, whereas
    # CommonMark's map counts CR/LF only. Normalize solely for locating lines;
    # all emitted text is sliced from the original line strings.
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)
    if lines and lines[-1] == "":
        lines.pop()
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    spans = [token for token in tokens if token.level == 0 and token.map]
    inline_titles = {
        tuple(token.map): token.content
        for token in tokens
        if token.type == "inline" and token.map
    }
    structural_starts = {
        token.map[0]
        for token in spans
        if token.map and token.type in {"heading_open", "fence", "code_block"}
    }
    units: list[Unit] = []
    heading_stack: list[tuple[int, str]] = []
    cursor = 0

    def append(start: int, end: int, kind: str) -> None:
        """将原行数组的 [start,end) 切片追加为 kind 单元，并记录当前标题路径。"""
        if start >= end:
            return
        value = "".join(lines[start:end])
        units.append(
            Unit(
                f"u{len(units) + 1:06d}",
                value,
                start + 1,
                end,
                [name for _, name in heading_stack],
                kind,
            )
        )

    for token in spans:
        assert token.map is not None
        start, end = token.map
        if start < cursor:
            continue
        if start > cursor:
            gap = "".join(lines[cursor:start])
            append(cursor, start, "whitespace" if gap.isspace() else "raw")
        kind = token.type.removesuffix("_open")
        if kind == "heading":
            level = int(token.tag[1:])
            # Inline content is used only for metadata; evidence retains the
            # original Markdown, including Setext underline and source syntax.
            title = inline_titles.get(tuple(token.map), lines[start].strip())
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        elif kind in {"fence", "code_block"}:
            kind = "code"
        elif kind == "html_block" and "<table" in lines[start].lower():
            kind = "table"
        elif kind == "paragraph":
            first = lines[start].strip()
            if first in {"$$", "\\["}:
                closing = "$$" if first == "$$" else "\\]"
                close = None
                for index in range(start + 1, len(lines)):
                    # 未闭合公式不能吞入下一个章节或代码块的 $$ 字面量。
                    if index in structural_starts:
                        break
                    if lines[index].strip() == closing:
                        close = index
                        break
                if close is not None:
                    end, kind = close + 1, "math"
            elif first.startswith("$$") and first.endswith("$$") and len(first) > 4:
                kind = "math"
        append(start, end, kind)
        cursor = end
    if cursor < len(lines):
        tail = "".join(lines[cursor:])
        append(cursor, len(lines), "whitespace" if tail.isspace() else "raw")
    if "".join(unit.text for unit in units) != text:
        raise ValueError("Markdown source coverage check failed")
    return units


def _safe_halves(text: str, budget: int) -> list[str]:
    """递归按字符边界拆分 text，返回每段不超过 budget 的原文片段。

    用于库分块无法保真时兜底；若单个字符已超预算则抛出 ValueError。
    """
    if token_count(text) <= budget:
        return [text]
    if len(text) <= 1:
        raise ValueError("chunk_tokens is too small to encode one source character")
    # 这是 Unicode 安全兜底，不解码被切断的 token 字节；保留两侧所有字符。
    middle = len(text) // 2
    return _safe_halves(text[:middle], budget) + _safe_halves(text[middle:], budget)


def chunk_units(units: list[Unit], chunk_tokens: int = 4000) -> list[Chunk]:
    """将相邻 units 打包为核心块，只拆分超过 chunk_tokens 的单元。

    正常尺寸表格、代码和公式保持完整；超长结构保留所有字符但子片段可能缺表头或围栏。
    标题元数据不计入正文预算；标题无法可靠区分论文，调用方须保留单元语境。
    预算非法、单元 ID 重复或字符回拼失败时抛出 ValueError。
    """
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if len({unit.id for unit in units}) != len(units):
        raise ValueError("Unit IDs must be unique")
    rules = RecursiveRules(
        levels=[
            RecursiveLevel(delimiters=["\n\n", "\n"]),
            RecursiveLevel(delimiters=["。", "！", "？", "；", ". ", "! ", "? "]),
            RecursiveLevel(whitespace=True),
            RecursiveLevel(),
        ]
    )
    splitter = RecursiveChunker(
        tokenizer=get_tokenizer().backend, chunk_size=chunk_tokens, rules=rules
    )
    # 先得到每段都能独立装入预算的原文片段，仍保留其所属单元。
    pieces: list[tuple[Unit, str]] = []
    for unit in units:
        if not unit.text:
            continue
        parts = [unit.text]
        if token_count(unit.text) > chunk_tokens:
            try:
                parts = [chunk.text for chunk in splitter.chunk(unit.text)]
            except (ValueError, UnicodeError):
                # 库 tokenizer 可能拒绝原文中的特殊 token 字面量；它们是
                # 文档内容，不是控制指令，按普通文本计量并从源文安全切分。
                parts = _safe_halves(unit.text, chunk_tokens)
            # Chonkie 的底层 token 切分可能破坏 Unicode 或忽略空白；一旦
            # 回拼或预算校验失败就回到原文，不在损坏的库输出上继续修补。
            if "".join(parts) != unit.text or any(
                token_count(p) > chunk_tokens for p in parts
            ):
                parts = _safe_halves(unit.text, chunk_tokens)
        pieces.extend((unit, part) for part in parts)
    chunks = _pack_parts(pieces, chunk_tokens)
    if "".join(chunk.text for chunk in chunks) != "".join(unit.text for unit in units):
        raise ValueError("Chunk source coverage check failed")
    return chunks


def _pack_parts(pieces: list[tuple[Unit, str]], budget: int) -> list[Chunk]:
    """将已满足单段预算的pieces组为块，返回逐字连续且原生计数不超budget的块。

    倍增探测后收窄边界，避免每加入一个短单元都重新分词整块。
    BPE计数不保证随字符数严格单调，所以只承诺选中边界已实际验证，
    不承诺取得所有可能边界中的最大块；任何失败探测都不会被直接输出。
    """
    chunks: list[Chunk] = []
    start = 0

    def fits(end: int) -> bool:
        """对当前start到end的完整拼接原文精确计数，返回是否符合预算。"""
        return token_count("".join(text for _, text in pieces[start:end])) <= budget

    while start < len(pieces):
        # 单段已由上游核验；无论后续探测结果如何，至少前进一段。
        end = start + 1
        probe = min(start + 2, len(pieces))
        while probe > end and fits(probe):
            end = probe
            probe = min(start + 2 * (probe - start), len(pieces))
        low, high = end + 1, probe - 1
        while low <= high:
            middle = (low + high) // 2
            if fits(middle):
                end, low = middle, middle + 1
            else:
                high = middle - 1
        chosen = pieces[start:end]
        chunks.append(
            Chunk(
                id=f"c{len(chunks) + 1:06d}",
                unit_ids=list(dict.fromkeys(unit.id for unit, _ in chosen)),
                text="".join(text for _, text in chosen),
                headings=list(
                    dict.fromkeys(
                        heading for unit, _ in chosen for heading in unit.headings
                    )
                ),
            )
        )
        start = end
    return chunks
