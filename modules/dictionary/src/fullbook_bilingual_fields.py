"""Reassign literal translations only in repeated bilingual glossary layouts."""
import re

_CJK = re.compile(r'[\u3400-\u9fff]')
_EN = re.compile(r"[A-Za-z][A-Za-z0-9\s.,()\[\]_/+'\u2019=:-]{0,110}")
_INLINE = re.compile(r'^(?P<en>[A-Za-z][^\u3400-\u9fff]{0,110}?)\s+'
                     r'(?P<zh>[\uff08(\[]*[0-9]*[)\uff09\]]*[\u3400-\u9fff].*)$')
_PROSE = re.compile(r'^(?:\u6307|\u662f|\u4e00\u79cd|\u5c06|\u80fd|\u7531|\u5f53|\u5728|\u7528\u4e8e|\u7528\u6765)'
                    r'|(?:\u7528\u4e8e|\u7528\u6765|\u53ef\u4ee5|\u80fd\u591f|\u79f0\u4e3a|\u8868\u793a\u4e86)')


def _translation(value):
    return (0 < len(value) <= 80 and _CJK.search(value) is not None
            and not re.search(r'[\n\r\u3002\uff01\uff1f!?:\uff1a=]', value)
            and not _PROSE.search(value)
            and all(token.isupper() for token in re.findall(r'[A-Za-z]+', value)))


def _pairs(text, start, end):
    lines = []
    offset = start
    for line in text[start:end].splitlines(keepends=True):
        value = line.strip()
        if value:
            lines.append((offset + len(line) - len(line.lstrip()), value))
        offset += len(line)
    pairs = []
    for i, (pos, value) in enumerate(lines):
        match = _INLINE.fullmatch(value)
        if match and _translation(match['zh']):
            pairs.append((pos, match['en'].strip(), match['zh'].strip()))
        elif (_EN.fullmatch(value) and i + 1 < len(lines)
              and _translation(lines[i + 1][1])):
            pairs.append((pos, value, lines[i + 1][1]))
    return pairs


def repair_bilingual_fields(entry, text):
    """Keep genuine definitions and ambiguous/source-conflicting names unchanged."""
    en = entry.get('knowledge_point', '').strip()
    body = entry.get('raw_content', '')
    zh = body.strip()
    if entry.get('name') or not _EN.fullmatch(en) or not _translation(zh):
        return False
    source = entry['source']
    heads, spans = source.get('head_spans', []), source.get('body_spans', [])
    if len(heads) != 1 or len(spans) != 1:
        return False
    h0, h1 = heads[0]
    a, b = spans[0]
    if not (0 <= h0 < h1 <= a < b <= len(text)):
        return False
    if text[h0:h1].strip() != en or text[a:b].strip() != zh:
        return False
    gap = text[h1:a]
    if gap.strip() or len(gap) > 8:
        return False
    line_end = text.find('\n', b)
    if text[b:line_end if line_end >= 0 else len(text)].strip():
        return False
    if text[text.rfind('\n', 0, h0) + 1:h0].strip():
        return False
    lo = max(0, text.rfind('\n', 0, max(0, h0 - 2200)) + 1)
    hi = text.find('\n', min(len(text), b + 2200))
    pairs = _pairs(text, lo, len(text) if hi < 0 else hi)
    before = {head for pos, head, value in pairs if pos < h0}
    after = {head for pos, head, value in pairs if pos > h0}
    if len(before) < 2 or len(after) < 2:
        return False
    if any(head == en and value != zh for pos, head, value in pairs):
        return False
    # Only transfer the selected original text; never translate or complete it.
    c = a + len(text[a:b]) - len(text[a:b].lstrip())
    d = b - (len(text[a:b]) - len(text[a:b].rstrip()))
    source.setdefault('original_head_spans', [list(s) for s in heads])
    source.setdefault('original_body_spans', [list(s) for s in spans])
    source['head_spans'] = [list(s) for s in heads] + [[c, d]]
    source['body_spans'] = []
    source['name_spans'] = [[c, d]]
    entry['name'] = zh
    entry['head'] = en + ' ' + zh
    entry['raw_content'] = ''
    entry['name_evidence'] = [list(s) for s in source['head_spans']]
    entry['body_evidence'] = []
    entry['bilingual_field_repair'] = {
        'reason': 'repeated_bilingual_layout_literal_translation',
        'moved_span': [c, d], 'moved_text': zh,
        'previous_name': '', 'previous_body': body,
        'supporting_neighbors_before': len(before),
        'supporting_neighbors_after': len(after)}
    return True
