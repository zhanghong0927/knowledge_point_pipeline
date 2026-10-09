"""Evidence-bounded structure guards and literal-span body cleanup for V5.

No OCR correction, sentence completion, or context-to-body promotion is allowed.
The V4 packet/name APIs are retained; overlap alone is not ownership evidence.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
import re
import threading

import fullbook_v4_structure as v4
from fullbook_bilingual_fields import repair_bilingual_fields
from fullbook_v4_structure import packet, preceding_same_heading

v3 = v4.v3
__all__ = ['annotate', 'packet', 'preceding_same_heading', 'repair_split_name',
           'guard_entries', 'cleanup_body']

_IMAGE = re.compile(r'!\[[^\]]*\]\([^\n]+\)')
_CREDIT = re.compile(r'^(?:Photo(?:graph)?(?:s)?(?:\s+by)?\b|Courtesy\b|'
                     r'(?:Figure|Fig\.)\s*\d|\u56fe\s*\d|\u6444\u5f71\s*[:\uff1a])', re.I)
_NUMBERED = re.compile(r'^\d+(?:\.\d+)+\s+[\u3400-\u9fff\[\uff3b].*[A-Za-z]')
_EQUIVALENCE = re.compile(r'^[A-Za-z][A-Za-z0-9_./-]*\s*\(\s*=')
_DICT = re.compile(r"^(?P<head>[\w\u3400-\u9fff'\u2019\-/ ,]{1,100}?)\s+"
                   r'(?:\([^\n)]{1,80}\)\s+)?(?:\d+\.\s+)?'
                   r'(?P<tag>abbr|adj|adv|n|v|vt|vi|vb|pl)\.\s', re.I)
_ABBREVIATIONS = {'n', 'abbr', 'adj', 'adv', 'v', 'vt', 'vi', 'vb', 'pl',
                  'mr', 'mrs', 'ms', 'dr', 'prof', 'fig', 'vol', 'pp', 'p',
                  'ed', 'eds', 'etc', 'cf', 'vs', 'st', 'no', 'b', 'd'}
_RECHECKED_RISKS = {'other_entry_head_in_body', 'systematic_interleaving_review',
                    'internal_heading_as_entry', 'excluded_section_entry',
                    'possible_fragmented_bilingual_head', 'body_mention_as_head',
                    'conflicting_selected_heads', 'overlap', 'span_overlap', 'coverage_gap'}
_OFFSET_CACHE = {}
_OFFSET_LOCK = threading.Lock()


def _offsets(units):
    # A book's immutable source positions are shared by per-entry cleanup calls.
    key = id(units)
    signature = (len(units), units[0]['offset'], units[-1]['offset']) if units else (0,)
    with _OFFSET_LOCK:
        cached = _OFFSET_CACHE.get(key)
        if cached is None or cached[0] is not units or cached[1] != signature:
            if len(_OFFSET_CACHE) >= 64:
                _OFFSET_CACHE.pop(next(iter(_OFFSET_CACHE)), None)
            cached = (units, signature, [row['offset'] for row in units])
            _OFFSET_CACHE[key] = cached
        return cached[2]


def _lexical_parent(row):
    value = v4.heading(row)
    if re.search(r'\b(?:dictionary|encyclopedia|glossary|chapter|section|category|'
                 r'categories|part|contents|index)\b|\u8bcd\u5178|\u8f9e\u5178|'
                 r'\u767e\u79d1|\u5206\u7c7b|\u7c7b\u76ee|\u7b2c.+[\u7ae0\u7bc7\u90e8]', value, re.I):
        return False
    return not row.get('subtitle_evidence') and (
        v4.bilingual(value) or row.get('head_role') == 'main_style')


def _level(row):
    match = re.match(r'^\s*(#+)\s', row['text'])
    return len(match[1]) if match else (1 if re.search(r'\n=+', row['text']) else 2)


def annotate(units):
    """Only hierarchy or a repeated *same* subtitle establishes a subtitle hint."""
    for row in units:
        if row.get('head_role') == 'internal_after_bilingual_main':
            row.pop('head_role', None)
            row.pop('parent_head_unit', None)
        row.pop('document_layout_risk', None)
        row.pop('source_interruption', None)
        row.pop('subtitle_evidence', None)
    v3.annotate_units(units)
    zone = False
    previous = None
    stack = []
    adjacent = defaultdict(list)
    for row in units:
        value = v4.heading(row)
        if row['kind'] == 'heading':
            if re.search(r'(?:\u672f\u8bed\u7d22\u5f15|\u8bcd\u76ee\u7d22\u5f15|'
                         r'\u6c49\u82f1\u7d22\u5f15|\u82f1\u6c49\u7d22\u5f15|'
                         r'\u6c49\u8bed\u62fc\u97f3\u7d22\u5f15|alphabetical index|index of terms)', value, re.I):
                zone = True
            elif zone and not re.fullmatch(r'[A-Za-z]|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]|[\u3400-\u9fff]?\s*[A-Z]', value):
                zone = False
            level = _level(row)
            while stack and _level(stack[-1]) >= level:
                stack.pop()
            if stack and _lexical_parent(stack[-1]) and row.get('head_role') != 'caption':
                row['head_role'] = 'internal_after_bilingual_main'
                row['parent_head_unit'] = stack[-1]['unit']
                row['subtitle_evidence'] = 'explicit_heading_hierarchy'
            if (previous and previous['kind'] == 'heading'
                    and _lexical_parent(previous) and v4.bilingual(v4.heading(previous))
                    and not v4.bilingual(value)
                    and row.get('head_role') != 'caption'):
                adjacent[v3.normalize(value)].append((previous, row))
            stack.append(row)
        if zone:
            row['excluded_zone'] = 'term_index'
        if row['text'].strip():
            previous = row
    for pairs in adjacent.values():
        if len({v3.normalize(v4.heading(parent)) for parent, _ in pairs}) >= 3:
            for parent, row in pairs:
                row['head_role'] = 'internal_after_bilingual_main'
                row['parent_head_unit'] = parent['unit']
                row['subtitle_evidence'] = 'same_subtitle_under_distinct_bilingual_heads'
    nonempty = [row for row in units if row['text'].strip()]
    interruptions = []
    for prev, row, nxt in zip(nonempty, nonempty[1:], nonempty[2:]):
        if (row['kind'] == 'heading' and prev['kind'] == nxt['kind'] == 'paragraph'
                and re.search(r'[A-Za-z]-\s*$', prev['text'])
                and re.match(r'^[a-z]', nxt['text'].strip())):
            row['source_interruption'] = [prev['offset'], nxt['offset'] + len(nxt['text'])]
            interruptions.append(row)
    if len(interruptions) >= 3:
        # Packet-wide warning is context only. Guards use localized selected evidence.
        for row in units:
            row['document_layout_risk'] = True
    report = False
    main = None
    for index,row in enumerate(units):
        if row['kind']=='heading':
            value=v4.heading(row)
            if re.match(r'^Appendix\b',value,re.I):
                upcoming=[]
                for candidate in units[index+1:index+41]:
                    if candidate['kind']=='heading' and re.match(r'^Appendix\b',v4.heading(candidate),re.I):
                        break
                    upcoming.append(candidate)
                headings={v4.heading(r).casefold().strip(' .:') for r in upcoming if r['kind']=='heading'}
                evidence='\n'.join(r['text'] for r in upcoming)
                report=bool({'introduction','summary'}.issubset(headings) and re.search(
                    r'\bThis report (?:evaluates|examines|analyzes|analyses|describes|presents|assesses)\b',evidence,re.I))
            elif re.fullmatch(r'(?:general )?(?:bibliography|index|glossary|main entries)',value.strip(' .:'),re.I):
                report=False
            if row.get('head_role')=='main_style':
                main=row
            elif row.get('head_role')=='secondary_style' and main:
                first=re.match(r'([A-Za-z]+)(.*)',value)
                parent=re.match(r'[A-Za-z]+',v4.heading(main))
                if first and parent and first[1].casefold()==parent[0].casefold():
                    synonyms=bool(re.match(r'\s+or\b',first[2],re.I) or
                        (first[2].startswith(',') and value.count(',')>=3))
                    if synonyms:
                        row['head_role']='internal_after_bilingual_main'
                        row['parent_head_unit']=main['unit']
                        row['subtitle_evidence']='same_topic_secondary_style'
        if report:
            row['excluded_zone']='appended_report'
    return units


def _sync(entry, text):
    spans = entry['source']['body_spans']
    entry['raw_content'] = '\n\n'.join(text[a:b] for a, b in spans)
    if 'body_evidence' in entry:
        entry['body_evidence'] = [list(span) for span in spans]


def _context(entry, text):
    source = entry['source']
    spans = source.get('original_body_spans', source['body_spans'])
    heads = source.get('head_spans', [])
    if not spans and not heads:
        return
    start = spans[0][0] if spans else heads[0][0]
    end = spans[-1][1] if spans else heads[-1][1]
    for key, a, b in [('body_context_before', max(0, start - 1200), start),
                      ('body_context_after', end, min(len(text), end + 1200))]:
        entry.setdefault(key, {'spans': [[a, b]] if a < b else [], 'text': text[a:b]})


def _remove(entry, text, removals):
    """Log only intersections with selected spans, including exact removed bytes."""
    spans = entry['source']['body_spans']
    actual = []
    for removal in removals:
        for a, b in spans:
            c, d = max(a, removal['start']), min(b, removal['end'])
            if c < d:
                actual.append({**removal, 'start': c, 'end': d, 'text': text[c:d]})
    if actual:
        entry['source'].setdefault('original_body_spans', [span[:] for span in spans])
        entry['source']['body_spans'] = v3.subtract(spans, [(r['start'], r['end']) for r in actual])
        entry.setdefault('body_cleanup_removed', []).extend(actual)
    _sync(entry, text)


def repair_split_name(entry, units, text):
    before = [span[:] for span in entry['source']['body_spans']]
    v4.repair_split_name(entry, units, text)
    if entry['source']['body_spans'] != before:
        removed = v3.subtract(before, entry['source']['body_spans'])
        entry.setdefault('body_cleanup_removed', []).extend(
            {'start': a, 'end': b, 'reason': 'adjacent_chinese_head_continuation', 'text': text[a:b]}
            for a, b in removed)
    repair_bilingual_fields(entry, text)
    _sync(entry, text)
    _context(entry, text)


def _row_at(position, units, offsets):
    if not units:
        return None
    row = units[max(0, bisect_right(offsets, position) - 1)]
    return row if row['offset'] <= position < row['offset'] + len(row['text']) else None


def _inline_head(row):
    value = row['text'].lstrip()
    start = row['offset'] + len(row['text']) - len(value)
    if _NUMBERED.match(value):
        return start, start + len(value.rstrip())
    match = _DICT.match(value)
    if match:
        return start, start + match.start('tag')
    return None


def _independent(entry, units, offsets, text):
    spans = entry['source'].get('head_spans', [])
    if not spans:
        return False
    a, b = spans[0]
    row = _row_at(a, units, offsets)
    if (not row or row.get('excluded_zone') or row.get('head_role') == 'caption'
            or row.get('subtitle_evidence')):
        return False
    for c, _ in spans[1:]:
        later = _row_at(c, units, offsets)
        if (later and later is not row
                and (_inline_head(later) or _EQUIVALENCE.match(later['text'].strip()))):
            return False
    if row['kind'] == 'heading':
        return True
    inline = _inline_head(row)
    if inline and a <= inline[0] + 1 and b <= inline[1]:
        return True
    prefix = text[row['offset']:a].strip()
    suffix = text[spans[-1][1]:row['offset'] + len(row['text'])].strip()
    quote = '\n'.join(text[c:d] for c, d in spans)
    if not prefix and not suffix and not re.search(r'[.!?\u3002\uff01\uff1f]\s*$', quote):
        return True
    # A distinct, emphasized opening PDF fragment can support an inline head.
    if not prefix:
        for annotation in row.get('pdf_format', []):
            fragments = [f for f in annotation.get('spans', []) if f.get('text', '').strip()]
            if not fragments:
                continue
            first = fragments[0]
            if (first.get('bold') or first.get('size_ratio', 1) >= 1.15):
                if v3.normalize(first['text']) == v3.normalize(text[a:b]):
                    return True
    return False


def _flag(entry, risks):
    if risks:
        entry['extraction_risks'] = sorted(risks)
        entry['structural_review_required'] = True
        entry['eligible_for_name_screening'] = False
    else:
        entry.pop('extraction_risks', None)
        entry['structural_review_required'] = False
        entry['eligible_for_name_screening'] = True


def guard_entries(entries, units, text):
    """Recheck ownership without mark_conflicts or book-wide overlap quarantine."""
    offsets = _offsets(units)
    independent = {id(e): _independent(e, units, offsets, text) for e in entries}
    candidates = []
    for entry in entries:
        if independent[id(entry)]:
            candidates.extend((a, b, entry) for a, b in entry['source']['head_spans'])
    candidates.sort(key=lambda item: (item[0], item[1]))
    positions = [a for a, _, _ in candidates]
    max_head_length = max((b-a for a,b,_ in candidates), default=0)

    def intersecting(a, b):
        lo = bisect_right(positions, a - max_head_length)
        hi = bisect_left(positions, b)
        return (other for c,d,other in candidates[lo:hi] if a < d)
    for entry in entries:
        _sync(entry, text)
        risks = set(entry.get('extraction_risks', [])) - _RECHECKED_RISKS
        heads = entry['source'].get('head_spans', [])
        selected = entry['source']['body_spans']
        if not heads:
            risks.add('missing_source_head')
            _flag(entry, risks)
            continue
        row = _row_at(heads[0][0], units, offsets)
        if row:
            if row.get('subtitle_evidence'):
                risks.add('internal_heading_as_entry')
            if row.get('excluded_zone'):
                risks.add('excluded_section_entry')
            # Weak evidence is not proof of a wrong head; later name filters own it.
            for c, _ in heads[1:]:
                later = _row_at(c, units, offsets)
                if (later and later is not row and
                        (_inline_head(later) or _EQUIVALENCE.match(later['text'].strip()))):
                    risks.add('multiple_independent_heads_selected')
        head_key = [tuple(span) for span in heads]
        for a, b in selected:
            if any(other is not entry and [tuple(span) for span in other['source']['head_spans']] != head_key
                   for other in intersecting(a, b)):
                risks.add('other_entry_head_in_body')
        if independent[id(entry)]:
            for a, b in heads:
                if any(other is not entry and [tuple(span) for span in other['source']['head_spans']] != head_key
                       for other in intersecting(a, b)):
                    risks.add('conflicting_selected_heads')
        # Source-pattern evidence also catches an omitted neighboring candidate.
        for a, b in selected:
            first = max(0, bisect_right(offsets, a) - 1)
            last = bisect_right(offsets, b - 1)
            for body_row in units[first:last]:
                if any(body_row['offset'] <= c < body_row['offset'] + len(body_row['text'])
                       for c, _ in heads):
                    continue
                inline = _inline_head(body_row)
                if inline and a <= inline[0] < b:
                    risks.add('other_entry_head_in_body')
                interrupted = body_row.get('source_interruption')
                if interrupted and a < body_row['offset'] + len(body_row['text']) and body_row['offset'] < b:
                    risks.add('systematic_interleaving_review')
        first_line = entry['raw_content'].strip().split('\n', 1)[0]
        if (entry.get('knowledge_point') and re.fullmatch(r'[\u3400-\u9fff]', entry.get('name', ''))
                and re.fullmatch(r'[\u3400-\u9fff]{2,24}', first_line)):
            risks.add('possible_fragmented_bilingual_head')
        _flag(entry, risks)
    return entries


def _caption_style(row):
    fragments = [s for f in row.get('pdf_format', []) for s in f.get('spans', [])
                 if s.get('text', '').strip()]
    if not fragments:
        return False
    total = sum(len(s['text'].strip()) for s in fragments)
    marked = sum(len(s['text'].strip()) for s in fragments
                 if (s.get('italic') or 'italic' in s.get('font', '').casefold())
                 and abs(s.get('size_ratio', 1) - 1) >= .1)
    return marked / total >= .6


def _sentence_end(value):
    """Last terminal, excluding lexical abbreviations, initials, and decimals."""
    last = 0
    for match in re.finditer(r'[.!?\u3002\uff01\uff1f]', value):
        i = match.start()
        if value[i] == '.':
            left = value[:i]
            right = value[i + 1:]
            token = re.search(r'([A-Za-z]+)$', left)
            if (i and value[i - 1].isdigit() and right[:1].isdigit()):
                continue
            if token:
                word = token[1]
                final_upper = word.isupper() and not right.strip(' \t\r\n\"\'\u201d\u2019)\uff09]')
                if word.casefold() in _ABBREVIATIONS and not final_upper:
                    continue
                preceded_by_digit = token.start() > 0 and left[token.start() - 1].isdigit()
                if len(word) == 1 and not final_upper and not preceded_by_digit:
                    continue
                if re.search(r'(?:[A-Za-z]\.)+[A-Za-z]$', left) and not final_upper:
                    continue
            if re.search(r'(?:^|\n)\s*\d+$', left):
                continue
            if right and not re.match(r'[\s\"\u201d\u2019\')\]\uFF09]', right):
                continue
        end = match.end()
        while end < len(value) and value[end] in '\"\'\u201d\u2019)\uff09]':
            end += 1
        last = end
    return last


def _trim_tail(entry, units, text):
    spans = entry['source']['body_spans']
    if not spans:
        return
    last_end = spans[-1][1]
    offsets = _offsets(units)
    index = bisect_right(offsets, last_end - 1) - 1
    tail = None
    while index >= 0:
        if units[index]['text'].strip():
            tail = units[index]
            break
        index -= 1
    if (tail and tail['kind'] in ('table', 'math_block', 'fence', 'code_block')
            and last_end >= tail['offset'] + len(tail['text'])):
        return
    value = entry['raw_content']
    stripped = value.rstrip()
    # Omitting only the source's sentence mark is coverage, not an unsafe phrase.
    if text[last_end:last_end + 1] in ('.', '!', '?', '\u3002', '\uff01', '\uff1f'):
        return
    split_word = (0 < last_end < len(text) and text[last_end - 1].isalpha()
                  and text[last_end].isalpha())
    broken_tail = bool(re.search(r'[A-Za-z]-$|[:\uff1a]$', stripped))
    continuation = False
    if entry.get('body_complete') is False:
        names = {v3.normalize(entry.get(k, '')) for k in ('head', 'knowledge_point', 'name')}
        for row in units[max(0, bisect_right(offsets, last_end) - 1):
                         min(len(units), bisect_right(offsets, last_end) + 12)]:
            a, b = row['offset'], row['offset'] + len(row['text'])
            if b <= last_end:
                continue
            following = text[max(a, last_end):b].strip()
            if not following or not re.search(r'[\w\u3400-\u9fff]', following):
                continue
            if row.get('excluded_zone'):
                break
            if row['kind'] == 'heading':
                if v3.normalize(v4.heading(row)) in names or row.get('head_role') == 'caption':
                    continue
                break
            if _inline_head(row) or _EQUIVALENCE.match(following):
                break
            if _IMAGE.fullmatch(following) or row.get('layout_caption_for') or _caption_style(row):
                continue
            if re.match(r'^(?:\[\s*(?:ed\.|editor)|BIBLIOGRAPHY:|\(?\s*See also\b|Compare\b|\u53c2\u89c1)', following, re.I):
                break
            continuation = row['kind'] in ('paragraph', 'list', 'bullet_list', 'ordered_list')
            break
    if not (split_word or broken_tail or continuation):
        return
    end = _sentence_end(value)
    if not value[end:].strip():
        return
    cursor = 0
    removals = []
    for index, (a, b) in enumerate(spans):
        length = b - a
        if cursor + length > end:
            removals.append({'start': a + max(0, end - cursor), 'end': b,
                             'reason': 'incomplete_sentence_tail'})
        cursor += length + (2 if index < len(spans) - 1 else 0)
    _remove(entry, text, removals)
    entry['body_complete'] = False
    entry['body_coverage_warning'] = 'trimmed_to_complete_sentence'


def _font_interruption(before, caption, after):
    """An image-adjacent font change interrupts the same unfinished sentence."""
    if any(row['kind']!='paragraph' for row in (before,caption,after)):
        return False
    left,middle,right=(row['text'].strip() for row in (before,caption,after))
    if not left or not right or not 0<len(middle)<=300:
        return False
    if not (left[-1].isalnum() or left.endswith('-')) or not right[0].islower():
        return False
    def fonts(row):
        return {span['font'] for ann in row.get('pdf_format',[]) for span in ann.get('spans',[])
                if span.get('font') and span.get('text','').strip()}
    body_fonts=fonts(before)&fonts(after)
    caption_fonts=fonts(caption)
    return bool(body_fonts and caption_fonts and not body_fonts&caption_fonts)


def cleanup_body(entry, units, text):
    """Remove proven source noise and trim unsafe tails; never extend selections."""
    _sync(entry, text)
    _context(entry, text)
    spans = entry['source']['body_spans']
    if not spans:
        return entry
    names = {v3.normalize(entry.get(k, '')) for k in ('head', 'name', 'knowledge_point')
             if entry.get(k)}
    start, end = spans[0][0], spans[-1][1]
    offsets = _offsets(units)
    first = max(0, bisect_right(offsets, start) - 1)
    last = bisect_right(offsets, end - 1)
    # Include a removed image immediately preceding a retained caption.
    nearby = units[max(0, first - 8):last]
    nonempty = [row for row in nearby if row['text'].strip()]
    image_units = {row['unit'] for row in nonempty if _IMAGE.fullmatch(row['text'].strip())}
    caption_units = set()
    for index, row in enumerate(nonempty):
        if row['unit'] not in image_units:
            continue
        if index>0 and index+2<len(nonempty):
            caption,after=nonempty[index+1:index+3]
            if _font_interruption(nonempty[index-1],caption,after):
                caption_units.add(caption['unit'])
        for following in nonempty[index + 1:index + 9]:
            if following['kind'] != 'paragraph' or _IMAGE.fullmatch(following['text'].strip()):
                break
            if _caption_style(following) or _CREDIT.match(following['text'].strip()):
                caption_units.add(following['unit'])
            else:
                break
    removals = []
    risks = set(entry.get('extraction_risks', []))
    for row in units[first:last]:
        a, b = row['offset'], row['offset'] + len(row['text'])
        if not any(c <= a and b <= d for c, d in spans):
            continue
        value = row['text'].strip()
        reason = None
        if _IMAGE.fullmatch(value):
            reason = 'image_markup'
        elif row['unit'] in caption_units:
            reason = 'evidenced_inline_caption'
        elif row['kind'] == 'heading' and v3.normalize(v4.heading(row)) in names:
            reason = 'repeated_own_head'
        elif row.get('layout_caption_for'):
            obj = next((r for r in units if r['unit'] == row.get('layout_object_unit')), None)
            if obj and (_IMAGE.fullmatch(obj['text'].strip()) or obj['kind'] == 'table'
                        or obj['text'].lstrip().startswith('<table')):
                reason = 'layout_caption'
        elif _CREDIT.match(value):
            # Lexical suspicion is not evidence permitting deletion.
            risks.add('ambiguous_inline_caption')
        if reason:
            removals.append({'start': a, 'end': b, 'reason': reason})
    _remove(entry, text, removals)
    _trim_tail(entry, units, text)
    if 'ambiguous_inline_caption' in risks:
        _flag(entry, risks)
    return entry
