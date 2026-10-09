"""Conservative, offset-preserving prose units and literal mapped projection.

Layout is supplied by the caller, never inferred from names or headings.
Only isolated, high-confidence source parentheses are dependent units. They
must be explicitly selected with an actually retained parent in the same
language. Uncertain source forms remain ordinary prose; no text is rewritten.
"""
import re

import clean_boundary_v41 as v41


_BRACKETS = {'(': ')', '\uff08': '\uff09', '[': ']', '\u3010': '\u3011', '{': '}'}
_QUOTES = {'"': '"', "'": "'", '\u201c': '\u201d', '\u2018': '\u2019',
           '\u300c': '\u300d', '\u300e': '\u300f', '\u300a': '\u300b'}
_OPENERS = {**_BRACKETS, **_QUOTES}
_CLOSERS = ''.join(set(_OPENERS.values()))
_TERMINALS = re.compile(
    r'[\u3002\uff01\uff1f]+[' + re.escape(_CLOSERS) + r']*|'
    r'[.!?]+[' + re.escape(_CLOSERS) + r']*(?=\s|$)'
)
_PARENTHESES = re.compile(r'\([^()\uff08\uff09\r\n]*\)|\uff08[^()\uff08\uff09\r\n]*\uff09')
_PAGE = re.compile(r'pp?\.\s*\d+(?:\s*[-\u2013\u2014]\s*\d+)?\.?', re.I)
_DATED_SOURCE = re.compile(
    r'[\u3400-\u9fff]{2,16}(?:\u7f51|\u62a5|\u901a\u8baf\u793e|\u6742\u5fd7)'
    r'\s*[,\uff0c:\uff1a]?\s*'
    r'(?:\d{4}\u5e74\d{1,2}\u6708\d{1,2}\u65e5|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})'
)
_AUTHOR_BOOK = re.compile(r'([\u3400-\u9fff]{2,4})\s*[:\uff1a]?\s*\u300a[^\u300a\u300b\r\n]+\u300b')
_EXPLANATORY_PREFIX = re.compile(
    r'^(?:\u53c2\u89c1|\u8be6\u89c1|\u4f8b\u5982|\u6bd4\u5982|\u9605\u8bfb|'
    r'\u5173\u4e8e|\u5305\u62ec|\u7814\u7a76|\u4f7f\u7528|\u6240\u8c13|\u5373|\u89c1|\u5982)'
)


def _possessive_in_quote(raw, index, char):
    following = raw[index + 1:].lstrip()
    if raw[index - 1].lower() != 's' or not following or not following[0].islower():
        return False
    # Prefer a possessive only when the next non-word quote is a clear closer,
    # not the opening of a separate quoted term or sentence.
    for match in re.finditer(r"['\u2018\u2019]", raw[index + 1:]):
        position = index + 1 + match.start()
        previous = raw[position - 1]
        next_char = raw[position + 1] if position + 1 < len(raw) else ''
        if previous == '\\' or (previous.isalnum() and next_char.isalnum()):
            continue
        return match.group() == char and previous in '.!?\u3002\uff01\uff1f'
    return False


def _syntax(raw):
    """Track paired delimiters without treating word apostrophes as quotes."""
    stack, brackets = [], []
    depths, bracket_starts, quote_ends = [0], [], set()
    backslashes = 0
    for index, char in enumerate(raw):
        bracket_starts.append(brackets[0] if brackets else None)
        escaped = backslashes % 2 == 1
        backslashes = backslashes + 1 if char == '\\' else 0
        previous = raw[index - 1] if index else ''
        following = raw[index + 1] if index + 1 < len(raw) else ''
        apostrophe = char in {"'", '\u2018', '\u2019'} and previous.isalnum() and following.isalnum()
        if (not apostrophe and char in {"'", '\u2019'} and index
                and stack and char == stack[-1][1] and following.isspace()):
            apostrophe = _possessive_in_quote(raw, index, char)
        if not escaped and not apostrophe:
            if stack and char == stack[-1][1]:
                opener, _, _ = stack.pop()
                if opener in _BRACKETS:
                    brackets.pop()
                else:
                    quote_ends.add(index + 1)
            elif char in _OPENERS:
                # A trailing possessive apostrophe cannot open a quotation.
                if char not in {"'", '\u2018'} or not previous.isalnum():
                    stack.append((char, _OPENERS[char], index))
                    if char in _BRACKETS:
                        brackets.append(index)
        depths.append(len(stack))
    return depths, bracket_starts, quote_ends


def _source_note(text):
    text = text.strip()
    if not _PARENTHESES.fullmatch(text):
        return False
    body = text[1:-1].strip()
    if _PAGE.fullmatch(body) or _DATED_SOURCE.fullmatch(body):
        return True
    author = _AUTHOR_BOOK.fullmatch(body)
    return bool(author and not _EXPLANATORY_PREFIX.match(author.group(1)))


def _source_spans(raw):
    depths, _, _ = _syntax(raw)
    spans = []
    for match in _PARENTHESES.finditer(raw):
        start, end = match.span()
        if depths[start] or not _source_note(match.group()):
            continue
        line_start = raw.rfind('\n', 0, start) + 1
        line_end = raw.find('\n', end)
        if line_end < 0:
            line_end = len(raw)
        on_own_line = not (raw[line_start:start].strip() or raw[end:line_end].strip())
        after_sentence = re.search(r'[.!?\u3002\uff01\uff1f][' + re.escape(_CLOSERS) + r']*\s*$', raw[:start])
        after_note = bool(spans and not raw[spans[-1][1]:start].strip())
        if on_own_line or after_sentence or after_note:
            spans.append((start, end))
    return spans


def _prose(raw):
    depths, bracket_starts, quote_ends = _syntax(raw)
    start = 0
    for match in _TERMINALS.finditer(raw):
        end = match.end()
        if depths[end]:
            continue
        bracket_start = bracket_starts[match.start()]
        following = raw[end:].lstrip()
        if bracket_start is not None and raw[start:bracket_start].strip():
            continue
        if (bracket_start is not None
                or any(position in quote_ends for position in range(match.start() + 1, end + 1))):
            if (following and following[0].islower()) or re.match(r'(?:or|and)\b', following, re.I):
                continue
        prefix = raw[:end].rstrip(_CLOSERS)
        if prefix.endswith('.'):
            punctuation = match.group().rstrip(_CLOSERS)
            if (re.search(r'\.\s*\.$', prefix) or re.match(r'\s*\.', raw[end:])
                    or len(punctuation) > 1):
                continue
            token = re.search(r'([A-Za-z.]+)\.$', prefix)
            if token:
                abbreviation = token.group(1).lower()
                if abbreviation in v41.ABBREVIATIONS:
                    continue
                if (abbreviation in v41.LOWERCASE_CONTINUATION_ABBREVIATIONS
                        and re.match(r'\s+[a-z]', raw[end:])):
                    continue
            if re.search(r'(?:\b[A-Za-z]\.){2,}$', prefix) or re.search(r'\b[A-Z]\.$', prefix):
                continue
        if raw[start:end].strip():
            yield start, end
        start = end
    if raw[start:].strip():
        yield start, len(raw)


def units(raw, layout_texts=frozenset()):
    """Return v41-shaped unit/start/end/text/layout dictionaries over raw."""
    pieces, start = [], 0
    for match in re.finditer(r'(?m)^[^\n]+$', raw):
        if match.group().strip() in layout_texts:
            pieces.extend([(start, match.start(), False), (match.start(), match.end(), True)])
            start = match.end()
    pieces.append((start, len(raw), False))
    result = []

    def append(first, last, layout):
        if raw[first:last].strip():
            result.append({'unit': len(result), 'start': first, 'end': last,
                           'text': raw[first:last], 'layout': layout})

    for first, last, layout in pieces:
        if layout:
            append(first, last, True)
            continue
        text = raw[first:last]
        start = 0
        for note_start, note_end in _source_spans(text):
            for begin, end in _prose(text[start:note_start]):
                append(first + start + begin, first + start + end, False)
            append(first + note_start, first + note_end, False)
            start = note_end
        for begin, end in _prose(text[start:]):
            append(first + start + begin, first + start + end, False)
    return result


def project(mapped, vote, layout_texts=frozenset()):
    """Return (language -> mapped selection, edits), using this module's units.

    Preserve v41 range validation, incomplete-sentence bridge protection,
    whitespace trimming and mapped.select joiners without patching v41 globals.
    """
    sentence_units = units(mapped.text, layout_texts)
    if (not isinstance(vote, dict) or not isinstance(vote.get('ranges'), list)
            or vote.get('exclude_units') != []):
        raise ValueError('Expected complete ranges and empty exclusions')
    parents, previous_content = {}, None
    for unit in sentence_units:
        if unit['layout']:
            continue
        if _source_note(unit['text']):
            parents[unit['unit']] = previous_content
        else:
            previous_content = unit['unit']
    intervals, edits, retained = {}, [], {}
    previous_last = -1
    for selected in vote['ranges']:
        if not isinstance(selected, dict):
            raise ValueError('Invalid range')
        first, last, language = selected.get('first'), selected.get('last'), selected.get('language')
        if (type(first) is not int or type(last) is not int
                or not 0 <= first <= last < len(sentence_units)
                or first <= previous_last or language not in ('zh', 'en')):
            raise ValueError('Invalid, overlapping or unordered range')
        previous_last = last
        blocked_by_incomplete = False
        for unit in sentence_units[first:last + 1]:
            reason = None
            number = unit['unit']
            if blocked_by_incomplete:
                reason = 'avoid_bridge_after_incomplete'
            elif unit['layout']:
                reason = 'layout'
            elif number in parents:
                if retained.get(parents[number]) != language:
                    reason = 'orphan_source_note'
            elif not v41.boundary_v3.complete_sentence(unit['text']) or _syntax(unit['text'])[0][-1]:
                reason = 'incomplete_sentence'
                blocked_by_incomplete = True
            if reason:
                edits.append({'unit': number, 'reason': reason, 'text': unit['text']})
                continue
            start, end = unit['start'], unit['end']
            while start < end and mapped.text[start].isspace():
                start += 1
            while end > start and mapped.text[end - 1].isspace():
                end -= 1
            ranges = intervals.setdefault(language, [])
            if ranges and not mapped.text[ranges[-1][1]:start].strip():
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
            retained[number] = language
    return {language: mapped.select(ranges) for language, ranges in intervals.items()}, edits
