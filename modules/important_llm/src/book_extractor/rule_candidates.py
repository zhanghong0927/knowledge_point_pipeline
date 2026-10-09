"""从明确目录、索引和词汇表提名，并按精确词面反查正文原文证据。"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from .markdown import Unit, token_count
from .models import Candidate, EvidenceSpan

Origin = Literal["toc", "index", "glossary"]
_REGION_NAMES: dict[str, Origin] = {
    "目录": "toc",
    "目次": "toc",
    "简要目录": "toc",
    "contents": "toc",
    "table of contents": "toc",
    "索引": "index",
    "主题索引": "index",
    "名词索引": "index",
    "index": "index",
    "subject index": "index",
    "index of names": "index",
    "index of shows": "index",
    "index of biographies": "index",
    "index of persons, organizations and geographical names": "index",
    "词汇表": "glossary",
    "术语表": "glossary",
    "术语": "glossary",
    "glossary": "glossary",
}
_PAGE_NUMBER = r"(?:\d+|[ivxlcdm]+)(?:\s*[-–—,，、]\s*(?:\d+|[ivxlcdm]+))*"
_PAGE_SUFFIX = re.compile(
    rf"(?:\s*[.．·…]{{2,}}\s*|\s+)(?:[（(]{_PAGE_NUMBER}[)）]|{_PAGE_NUMBER})\s*$",
    re.I,
)
_SECTION_PREFIX = re.compile(
    r"^(?:第[一二三四五六七八九十百零\d]+[章节篇部]\s*"
    r"|§\s*\d+(?:[-.]\d+)*\s+"
    r"|\d+(?:\.\d+)*[.)]?\s+|[一二三四五六七八九十]+、\s*)"
)
_STRUCTURAL_PREFIX = re.compile(
    r"^(?:附录[一二三四五六七八九十百零\dA-Z]+(?=[\s:：、.．])"
    r"|第[一二三四五六七八九十百零\d]+[章节篇部]"
    r"|Appendix\s+[A-Z\d]+(?=[\s:：.]))[\s:：、.．]*",
    re.I,
)
_MAX_HITS = 8
_MAX_NAME = 120
_MAX_WINDOW = 400
_CONTEXT = 140
_HEADING_CONTEXT_TOKENS = 512


@dataclass
class _Nomination:
    """积累同词面提名及证据，不生成或改写名称。"""

    name: str
    first: Unit
    origins: list[Origin] = field(default_factory=list)
    name_ids: list[str] = field(default_factory=list)
    groups: list[tuple[list[str], list[EvidenceSpan]]] = field(default_factory=list)
    hits: int = 0
    truncated: bool = False
    window_truncated: bool = False
    heading_truncated: bool = False
    glossary_hits: int = 0
    table_limited: bool = False
    body_windows: set[tuple[tuple[str, int, int], ...]] = field(default_factory=set)


@dataclass
class _Node:
    """存储词面前缀，避免为每个名称重新扫描全书。"""

    children: dict[str, "_Node"] = field(default_factory=dict)
    terminal: str | None = None


def _heading_origin(label: str) -> Origin | None:
    """识别审计中出现的明确区域标题，装饰破折号不属于名称。"""
    label = label.strip().strip("#* ：:—–-").strip().casefold()
    # 实际交付含页眉式“392 Index”“viii Contents”，仅移除这类整行标题页码。
    label = re.sub(r"^(?:\d+|[ivxlcdm]+)\s+(?=index$|contents$)", "", label)
    return _REGION_NAMES.get(label)


def _regions(units: list[Unit]) -> dict[str, Origin]:
    """沿连续条目识别区域，兼容被转换为同级标题的目录行和索引字母。

    遇到普通章节标题或正文句子立即结束，不因旧标题路径继续吸入正文。
    不猜跨栏顺序，不将含索引关键词的普通条目标题当成新索引区域。
    """
    result: dict[str, Origin] = {}
    active: Origin | None = None
    index_started = False
    for unit in units:
        if unit.kind == "whitespace":
            continue
        if unit.kind == "heading":
            label = unit.headings[-1] if unit.headings else unit.text.strip("# ")
            explicit = _heading_origin(label)
            if explicit:
                active = explicit
                index_started = False
                result[unit.id] = active
                continue
            if active == "index" and re.fullmatch(r"[A-Za-z]", label.strip()):
                result[unit.id] = active
                continue
            if active in {"toc", "index"} and _PAGE_SUFFIX.search(label):
                index_started = active == "index"
                result[unit.id] = active
                continue
            active = None
            continue
        if active in {"toc", "index"}:
            lines = [line.strip() for line in unit.text.splitlines() if line.strip()]
            # CommonMark会把缩进子项或换行页码解析成代码。缩进本身不能
            # 证明导航区域结束；保留该歧义单元给LLM，真正的围栏代码仍结束区域。
            if (
                unit.kind == "code"
                and lines
                and all(
                    (line.startswith("    ") or line.startswith("\t"))
                    for line in unit.text.splitlines()
                    if line.strip()
                )
            ):
                continue
            # 一旦进入长句、图像或复杂表格就终止连续目录，避免结构区域外溢。
            if (
                unit.kind in {"code", "table", "math"}
                or (
                    active == "toc"
                    and any(
                        len(line) > 180
                        or re.search(r"[。！？;；]|!\[", line)
                        or (
                            not _PAGE_SUFFIX.search(line)
                            and re.search(r"[.!?](?:\s|$)", line)
                        )
                        for line in lines
                    )
                )
                or (
                    active == "index"
                    and index_started
                    and any(
                        not _PAGE_SUFFIX.search(line)
                        and not re.search(r"[.,]\s+See(?: also)?\s+\S", line, re.I)
                        and (
                            len(line) > 180
                            or re.search(r"[。！？;；]|!\[|[.!?](?:\s|$)", line)
                        )
                        for line in lines
                    )
                )
            ):
                active = None
            if active == "index" and any(_PAGE_SUFFIX.search(line) for line in lines):
                index_started = True
        if active:
            result[unit.id] = active
    return result


def strip_structural_prefix(name: str) -> str:
    """去除名称开头明确的章篇或附录编号，返回仍非空的实体名称。

    不删除普通数字、名称内部的编号或无编号的“附录”一词；
    原文单元、候选身份及来源坐标由调用方原样保留。
    """
    return _STRUCTURAL_PREFIX.sub("", name).strip() or name


def rule_only_units(
    units: list[Unit], candidates: list[Candidate]
) -> dict[str, Origin]:
    """返回可只走规则提名的目录/索引单元及其区域类型。

    units 为完整原文，candidates 为已提名结果。仅当单元每个非空行都已
    解析为候选且名称依据已落在该单元时分流；父项、复杂行、词汇表仍送
    正文发现，避免把规则未覆盖的名称或解释静默丢掉。原文与候选均不修改。
    """
    regions = _regions(units)
    nominated = {
        (unit_id, item.name)
        for item in candidates
        for unit_id in item.name_evidence_ids
    }
    routed: dict[str, Origin] = {}
    for unit in units:
        origin = regions.get(unit.id)
        if origin not in {"toc", "index"}:
            continue
        entries = _entries(unit, origin)
        lines = [line for line in unit.text.splitlines() if line.strip()]
        if not entries or len(entries) != len(lines):
            continue
        if all((unit.id, name) in nominated for name, *_ in entries):
            routed[unit.id] = origin
    return routed


def _name(value: str, origin: Origin) -> str | None:
    """去除列表和页码呈现标记，拒绝页码及明显章节结构名称。"""
    value = re.sub(r"^\s*[-+*]\s+", "", value).strip()
    value = _PAGE_SUFFIX.sub("", value).strip().rstrip(",，").strip()
    link = re.fullmatch(r"\[([^\]]+)\]\([^)]*\)", value)
    if link:
        value = link.group(1)
    value = value.strip("*_` \t")
    if origin == "toc":
        value = strip_structural_prefix(_SECTION_PREFIX.sub("", value).strip())
    if not 1 <= len(value) <= _MAX_NAME:
        return None
    if re.fullmatch(r"[\d\W_]+|[IVXLCDM]+|[A-Za-z]", value):
        return None
    if re.match(
        r"^(?:第[一二三四五六七八九十百零\d]+[章节篇部]|chapter\s+\d+|part\s+[ivx\d]+)",
        value,
        re.I,
    ):
        return None
    if (
        value.casefold() in _REGION_NAMES
        or value.casefold()
        in {
            "preface",
            "foreword",
            "introduction",
            "bibliography",
            "references",
            "acknowledgments",
            "acknowledgements",
            "list of illustrations",
        }
        or value
        in {
            "绪论",
            "概述",
            "小结",
            "习题",
            "思考题",
            "前言",
            "序言",
            "参考文献",
            "附录",
            "后记",
            "致谢",
        }
    ):
        return None
    if any(char in value for char in "。！？;；\t"):
        return None
    return value


def _entries(unit: Unit, origin: Origin) -> list[tuple[str, int, int, bool]]:
    """返回名称、原行区间及是否含原解释；表格仅接收明确双列表头。"""
    result: list[tuple[str, int, int, bool]] = []
    offset = 0
    table_columns: tuple[int, int] | None = None
    for line in unit.text.splitlines(keepends=True):
        start, end = offset, offset + len(line)
        offset = end
        stripped = line.strip()
        if not stripped:
            continue
        if unit.kind == "heading":
            if origin == "glossary":
                continue
            stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        has_definition = False
        value = stripped
        if "|" in stripped:
            if origin != "glossary" or "\\|" in stripped:
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) != 2:
                continue
            labels = [cell.strip("* ").casefold() for cell in cells]
            names = {"名称", "术语", "词汇", "term", "name"}
            meanings = {"释义", "解释", "定义", "说明", "definition", "meaning"}
            if labels[0] in names and labels[1] in meanings:
                table_columns = (0, 1)
                continue
            if labels[1] in names and labels[0] in meanings:
                table_columns = (1, 0)
                continue
            if table_columns is None or all(
                re.fullmatch(r":?-+:?", cell) for cell in cells
            ):
                continue
            value = cells[table_columns[0]]
            has_definition = bool(cells[table_columns[1]])
        elif origin == "glossary":
            pair = re.split(r"[：:]", stripped, maxsplit=1)
            if len(pair) != 2 or not pair[1].strip():
                continue
            value, has_definition = pair[0], True
        elif not (_PAGE_SUFFIX.search(stripped) or re.match(r"^[-+*]\s+", stripped)):
            continue
        # 多栏压平成一行、索引的 and/see 子项不可拼成一个新名称。
        unnumbered = _SECTION_PREFIX.sub("", value) if origin == "toc" else value
        if re.search(r"\s\d+(?:[-–—,]\d+)*\s+\S", unnumbered):
            continue
        name = _name(value, origin)
        if name is not None:
            result.append((name, start, end, has_definition))
    return result


def _word_boundary(text: str, start: int, end: int, name: str) -> bool:
    """英文端点按字母数字词界匹配，避免把 art 命中到 cart。"""
    for edge, neighbor in (
        (name[0], text[start - 1] if start else ""),
        (name[-1], text[end] if end < len(text) else ""),
    ):
        if edge.isascii() and (edge.isalnum() or edge == "_"):
            if neighbor and (neighbor.isalnum() or neighbor == "_"):
                return False
    return True


def _table_spans(unit: Unit, position: int) -> list[EvidenceSpan]:
    """小表保留全表；大 Markdown 表只保留完整表头和完整命中行。

    超长行、HTML 大表或无明确表头返回空；不把半行列值交给后续猜配对。
    """
    if len(unit.text) <= _MAX_WINDOW:
        return [EvidenceSpan(unit_id=unit.id, start=0, end=len(unit.text))]
    lines = unit.text.splitlines(keepends=True)
    if len(lines) < 2 or "|" not in lines[0]:
        return []
    separators = lines[1].strip().strip("|").split("|")
    if not all(re.fullmatch(r"\s*:?-+:?\s*", value) for value in separators):
        return []
    header_end = len(lines[0]) + len(lines[1])
    row_start = unit.text.rfind("\n", 0, position) + 1
    newline = unit.text.find("\n", position)
    row_end = len(unit.text) if newline < 0 else newline + 1
    if header_end > _MAX_WINDOW or row_end - row_start > _MAX_WINDOW:
        return []
    return [
        EvidenceSpan(unit_id=unit.id, start=0, end=header_end),
        EvidenceSpan(unit_id=unit.id, start=row_start, end=row_end),
    ]


def _bounded_prose_end(text: str, budget: int) -> int:
    """返回预算内句末或词边界的前缀终点；无法完整保留一个词时返回零。

    先二分缩小字符前缀，再优先退到句末，否则退到空白边界。
    不解码被截断的 token；最后仍以原文字符坐标回取。
    """
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if token_count(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    prefix = text[:low]
    boundaries = [m.end() for m in re.finditer(r"[。！？；]|[.!?;](?=\s|$)", prefix)]
    if not boundaries:
        boundaries = [m.start() for m in re.finditer(r"\s+", prefix)]
    # 分词数量不保证随字符数严格单调，缩到边界后再次校验。
    for end in reversed(boundaries):
        if token_count(text[:end]) <= budget:
            return end
    return 0


def _heading_body(
    units: list[Unit], index: int, regions: dict[str, Origin]
) -> tuple[list[EvidenceSpan], bool]:
    """回取标题后 token 预算内的原文及标题；无正文返回空证据。

    遇任何下一标题即停，不借其他子节定义当前对象。表格、代码、公式不切半；
    优先完整段落，超长段退到句或词边界；返回是否因窗口限制省略原文。
    纯图片和空白不作定义证据。
    """
    heading = units[index]
    remaining = _HEADING_CONTEXT_TOKENS - token_count(heading.text)
    if remaining <= 0:
        return [], True
    spans: list[EvidenceSpan] = []
    truncated = False
    for position in range(index + 1, len(units)):
        unit = units[position]
        if unit.kind == "heading" or unit.id in regions:
            break
        if unit.kind == "whitespace" or re.fullmatch(
            r"\s*!\[[^\]]*\]\([^\n]*\)\s*", unit.text
        ):
            continue
        if remaining <= 0:
            truncated = True
            break
        size = token_count(unit.text)
        if unit.kind in {"table", "code", "math"} and size > remaining:
            truncated = True
            break
        end = (
            len(unit.text)
            if size <= remaining
            else _bounded_prose_end(unit.text, remaining)
        )
        if end:
            spans.append(EvidenceSpan(unit_id=unit.id, start=0, end=end))
            remaining -= token_count(unit.text[:end])
        if end < len(unit.text):
            truncated = True
            break
    if spans:
        spans.append(EvidenceSpan(unit_id=heading.id, start=0, end=len(heading.text)))
    return spans, truncated


def extract_rule_candidates(units: list[Unit]) -> list[Candidate]:
    """提名并返回有界原文证据；无正文证据的名称仍作为待核定候选保留。

    建树 O(名称总字符数)，正文扫描 O(正文字符数 × 最长名称长度)，
    最长名称上限 120；不执行名称数乘全文长度的重复扫描。
    每名称最多保留八个正文命中；词面窗口最多 400 字符，标题窗口最多 512 token。
    截断显式记入 issues；两类窗口不改变原文字符坐标。
    """
    regions = _regions(units)
    nominations: dict[str, _Nomination] = {}
    for unit in units:
        origin = regions.get(unit.id)
        if origin is None or unit.kind in {"code", "math"}:
            continue
        for name, start, end, has_definition in _entries(unit, origin):
            item = nominations.setdefault(name, _Nomination(name, unit))
            if origin not in item.origins:
                item.origins.append(origin)
            if unit.id not in item.name_ids:
                item.name_ids.append(unit.id)
            if has_definition:
                if item.glossary_hits >= _MAX_HITS:
                    item.truncated = True
                    continue
                item.glossary_hits += 1
                item.window_truncated |= end - start > _MAX_WINDOW
                spans = [
                    EvidenceSpan(
                        unit_id=unit.id, start=start, end=min(end, start + _MAX_WINDOW)
                    )
                ]
                if unit.kind == "table":
                    spans = _table_spans(unit, start)
                    item.table_limited |= len(unit.text) > _MAX_WINDOW
                    if not spans:
                        continue
                item.groups.append((unit.headings, spans))
    if not nominations:
        return []
    root = _Node()
    for name in nominations:
        node = root
        for char in name:
            node = node.children.setdefault(char, _Node())
        node.terminal = name
    heading: Unit | None = None
    for index, unit in enumerate(units):
        if unit.kind == "heading":
            heading = unit
            if unit.id not in regions and unit.headings:
                # 仅用原标题去除章节编号后的精确名称，不把相邻章节当同义词。
                name = _SECTION_PREFIX.sub("", unit.headings[-1]).strip()
                item = nominations.get(name)
                if item is not None:
                    if unit.id not in item.name_ids:
                        item.name_ids.append(unit.id)
                    spans, limited = _heading_body(units, index, regions)
                    item.heading_truncated |= limited
                    key = tuple((s.unit_id, s.start, s.end) for s in spans)
                    if spans and key not in item.body_windows:
                        if item.hits >= _MAX_HITS:
                            item.truncated = True
                        else:
                            item.body_windows.add(key)
                            item.hits += 1
                            item.groups.append((unit.headings, spans))
            continue
        if unit.id in regions or unit.kind in {"whitespace", "code"}:
            continue
        for start in range(len(unit.text)):
            node = root
            for end in range(start, min(len(unit.text), start + _MAX_NAME)):
                child = node.children.get(unit.text[end])
                if child is None:
                    break
                node = child
                if node.terminal is None:
                    continue
                item = nominations[node.terminal]
                if not _word_boundary(unit.text, start, end + 1, item.name):
                    continue
                left = max(0, start - _CONTEXT)
                right = min(len(unit.text), end + 1 + _CONTEXT)
                item.window_truncated |= left > 0 or right < len(unit.text)
                spans = [EvidenceSpan(unit_id=unit.id, start=left, end=right)]
                if unit.kind == "table":
                    spans = _table_spans(unit, start)
                    item.table_limited |= len(unit.text) > _MAX_WINDOW
                    if not spans:
                        continue
                if heading is not None and heading.id not in regions:
                    item.window_truncated |= len(heading.text) > _MAX_WINDOW
                    spans.append(
                        EvidenceSpan(
                            unit_id=heading.id,
                            start=0,
                            end=min(len(heading.text), _MAX_WINDOW),
                        )
                    )
                key = tuple((span.unit_id, span.start, span.end) for span in spans)
                if key in item.body_windows:
                    continue
                if item.hits >= _MAX_HITS:
                    item.truncated = True
                    continue
                item.body_windows.add(key)
                item.hits += 1
                item.groups.append((unit.headings, spans))
    unit_map = {unit.id: unit for unit in units}
    result: list[Candidate] = []
    for name, item in nominations.items():
        seen: set[tuple[tuple[str, int, int], ...]] = set()
        for scope, raw_spans in item.groups or [(item.first.headings, [])]:
            spans = list(
                {
                    (span.unit_id, span.start, span.end): span for span in raw_spans
                }.values()
            )
            key = tuple((span.unit_id, span.start, span.end) for span in spans)
            if key in seen:
                continue
            seen.add(key)
            # 按命中窗口分别提名，保留同名跨章节义项，交由综合阶段判断。
            for span in spans:
                if not 0 <= span.start < span.end <= len(unit_map[span.unit_id].text):
                    raise ValueError("rule evidence span outside source unit")
            issues = [] if spans else ["insufficient_body_evidence"]
            if item.truncated:
                issues.append(f"rule_hits_truncated:{_MAX_HITS}")
            if item.table_limited:
                issues.append("rule_large_table_row_only_or_unsupported")
            if item.window_truncated:
                issues.append(f"rule_context_truncated:{_MAX_WINDOW}_chars")
            if item.heading_truncated:
                issues.append(
                    f"rule_heading_context_truncated:{_HEADING_CONTEXT_TOKENS}_tokens"
                )
            digest = hashlib.sha256(
                f"{name}\0{item.first.id}\0{key}".encode()
            ).hexdigest()[:16]
            result.append(
                Candidate(
                    candidate_id=f"rule-{digest}",
                    chunk_id=f"rule:{spans[0].unit_id if spans else item.first.id}",
                    scope=scope,
                    name=name,
                    evidence_ids=list(dict.fromkeys(span.unit_id for span in spans)),
                    name_evidence_ids=item.name_ids,
                    evidence_spans=spans,
                    aliases=[],
                    origins=item.origins,
                    issues=issues,
                )
            )
    return result
