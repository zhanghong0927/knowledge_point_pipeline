"""Exact, position-preserving anchors and conservative entry repair identity.

Selections retain the v1 schema and optionally add a zero-based ``occurrence``.
Occurrences count ALL literal matches in a unit, including overlapping matches,
before head/name/body scope checks. No source spelling or whitespace is repaired.
"""
import hashlib
import re
import unicodedata


_FORMAT = frozenset(' \t\r\n#*_`~()[]{}:;,./\\|-"\''
                    '\uFF08\uFF09\u3010\u3011\uFF1A\uFF0C\u3001\u3002')
_NON_HEAD_KINDS = {'gap', 'fence', 'code_block', 'table', 'html_block',
                   'math_block', 'bullet_list', 'ordered_list'}


def _same_book(*objects):
    identities = {key: set() for key in ('identifier', 'md_sha256', 'md_path')}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for metadata in (obj, obj.get('source'), obj.get('book')):
            if not isinstance(metadata, dict):
                continue
            for key, values in identities.items():
                if key in metadata:
                    value = metadata[key]
                    if not isinstance(value, str) or not value:
                        return False
                    values.add(value)
    return all(len(values) <= 1 for values in identities.values())


class _Resolver:
    def __init__(self, part, text=None):
        if not isinstance(part, dict) or not isinstance(part.get('units'), list):
            raise ValueError('Expected a source packet with units')
        self.rows = {}
        self.original_text = text
        for row in part['units']:
            if not isinstance(row, dict):
                raise ValueError('Invalid source unit')
            unit, offset, value = row.get('unit'), row.get('offset'), row.get('text')
            if (type(unit) is not int or unit < 0 or unit in self.rows
                    or type(offset) is not int or offset < 0 or not isinstance(value, str)):
                raise ValueError('Invalid or duplicate source unit/offset')
            if text is not None and (offset > len(text) or text[offset:offset+len(value)] != value):
                raise ValueError('Source unit does not match original text at its offset')
            self.rows[unit] = row

    def head_position(self, row, start, end=None):
        if (row.get('kind') in _NON_HEAD_KINDS or row.get('excluded_zone')
                or row.get('head_role') == 'caption'):
            return False
        if end is not None:
            value = row['text'] if self.original_text is None else self.original_text
            boundary = end if self.original_text is None else row['offset']+end
            if (boundary < len(value) and value[boundary-1].isalnum()
                    and (value[boundary].isalnum() or value[boundary] == '_')):
                return False
        if self.original_text is not None:
            absolute = row['offset']+start
            line_start = self.original_text.rfind('\n', 0, absolute)+1
            prefix = self.original_text[line_start:absolute]
        else:
            line_start = row['text'].rfind('\n', 0, start) + 1
            prefix = row['text'][line_start:start]
        if self.original_text is None and line_start == 0:
            previous = self.rows.get(row['unit'] - 1)
            if (previous and previous['offset'] + len(previous['text']) == row['offset']
                    and previous['text'] and not previous['text'].endswith(('\n', '\r'))):
                return False
        return re.fullmatch(r'[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*|__|`)?[ \t]*', prefix) is not None

    def resolve(self, item, *, role='head', head_spans=(), after=None):
        if not isinstance(item, dict):
            raise ValueError('Expected a literal source selection')
        unit, quote = item.get('unit'), item.get('quote')
        if type(unit) is not int or unit not in self.rows:
            raise ValueError('Selection unit is missing or invalid')
        if not isinstance(quote, str) or not quote:
            raise ValueError('Selection quote must be a nonempty literal string')
        if role not in ('head', 'name', 'body_start', 'body_end'):
            raise ValueError('Unknown selection role')
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError('Invalid selection lower bound')
        row = self.rows[unit]
        if row.get('excluded_zone'):
            raise ValueError('Selection lies in an excluded source region')
        matches = []
        cursor = 0
        while True:
            start = row['text'].find(quote, cursor)
            if start < 0:
                break
            matches.append((row['offset'] + start, row['offset'] + start + len(quote)))
            cursor = start + 1
        if not matches:
            raise ValueError('Quote is absent from its source unit')

        def eligible(span):
            if role == 'name' and not any(a <= span[0] and span[1] <= b for a, b in head_spans):
                return False
            return after is None or span[0] >= after

        if 'occurrence' in item:
            occurrence = item['occurrence']
            if type(occurrence) is not int or not 0 <= occurrence < len(matches):
                raise ValueError('occurrence must index a literal match (zero-based)')
            chosen = matches[occurrence]
        else:
            # Names use established head scope; starts cannot use text before the head.
            # End order is checked AFTER selection and never resolves duplicate ends.
            candidates = ([m for m in matches if eligible(m)]
                          if role in ('name', 'body_start') else matches)
            if role == 'head' and len(candidates) > 1:
                candidates = [m for m in candidates
                              if self.head_position(row, m[0] - row['offset'], m[1] - row['offset'])]
            if len(candidates) != 1:
                raise ValueError('Ambiguous or out-of-scope quote; supply occurrence')
            chosen = candidates[0]
        if not eligible(chosen):
            raise ValueError('Selection lies outside its head/body scope')
        return chosen

    def parts(self, items, **scope):
        if not isinstance(items, list):
            raise ValueError('Expected list of source selections')
        spans = [self.resolve(item, **scope) for item in items]
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise ValueError('Unordered or overlapping source selections')
        return spans

    def chars(self, spans):
        result = {}
        for a, b in spans:
            row = next((r for r in self.rows.values()
                        if r['offset'] <= a and b <= r['offset'] + len(r['text'])), None)
            if row is None:
                raise ValueError('Selection is not covered by a source unit')
            result.update((a+i, char) for i, char in
                          enumerate(row['text'][a-row['offset']:b-row['offset']]))
        return result


def resolve_selection(selection, part, *, role='head', head_spans=(), after=None):
    """Return an exact half-open original-character span or raise ValueError.

    ``role='name'`` restricts matches to ``head_spans``. ``body_start`` restricts
    matches to ``after`` (the preceding head/body end). ``body_end`` validates
    order using ``after`` but requires occurrence for ANY duplicate unit quote.
    """
    return _Resolver(part).resolve(selection, role=role, head_spans=head_spans, after=after)


def validate_entry(item, part, text, book):
    """Build exactly the v1 single-entry schema, without cross-entry policy."""
    if not isinstance(item, dict) or not isinstance(text, str) or not isinstance(book, dict):
        raise ValueError('Invalid entry, original text, or book schema')
    if not all(isinstance(book.get(k), str) and book[k] for k in ('identifier', 'md_sha256')):
        raise ValueError('Book identifier and MD hash are required')
    resolver = _Resolver(part, text)
    if not _same_book(item, part, book, *resolver.rows.values()):
        raise ValueError('Cross-book source identity mismatch')
    head = resolver.parts(item.get('head'))
    if head:
        first=resolver.rows[item['head'][0]['unit']]
        if not resolver.head_position(first,head[0][0]-first['offset'],head[0][1]-first['offset']):
            raise ValueError('First head selection is not at a supported source head position')
    lo, hi = part.get('lo'), part.get('hi')
    if (not head or type(lo) is not int or type(hi) is not int or lo < 0 or hi <= lo
            or not lo <= item['head'][0]['unit'] < hi):
        raise ValueError('Head outside ownership or empty head')
    fields = {}
    for key in ('knowledge_point', 'name'):
        spans = resolver.parts(item.get(key), role='name', head_spans=head)
        fields[key] = ' '.join(text[a:b] for a, b in spans)
    if not fields['knowledge_point'] and not fields['name']:
        raise ValueError('At least one original-language name required')
    if not isinstance(item.get('body'), list):
        raise ValueError('Expected body range list')
    body, end = [], head[-1][1]
    for span in item['body']:
        if not isinstance(span, dict):
            raise ValueError('Expected body start/end selections')
        a = resolver.resolve(span.get('start'), role='body_start', after=end)[0]
        b = resolver.resolve(span.get('end'), role='body_end', after=a)[1]
        if b <= a:
            raise ValueError('Body range is empty or reversed')
        body.append((a, b))
        end = b
    if type(item.get('body_complete')) is not bool:
        raise ValueError('body_complete must be boolean')
    identity = f"{book['identifier']}:{book['md_sha256']}:{head[0][0]}"
    return dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], **fields,
                head=' '.join(text[a:b] for a, b in head),
                raw_content='\n\n'.join(text[a:b] for a, b in body),
                body_complete=item['body_complete'],
                leading_context=text[max(0, head[0][0]-600):head[0][0]],
                trailing_context=text[end:end+1200],
                source={**book, 'head_spans': head, 'body_spans': body},
                ready_for_delivery=False)


def _lexical(chars):
    return {pos: char for pos, char in chars.items() if char not in _FORMAT}


def _script(char):
    if '\u3400' <= char <= '\u9fff':
        return 'cjk'
    if 'LATIN' in unicodedata.name(char, ''):
        return 'latin'
    return 'other'


def _title_selected(resolver, row, head):
    selected = _lexical(resolver.chars(head))
    literal = _lexical({row['offset']+i: char for i, char in enumerate(row['text'])})
    return bool(literal) and all(selected.get(pos) == char for pos, char in literal.items())


def _title_cluster(resolver, head):
    involved = [r for r in sorted(resolver.rows.values(), key=lambda r: r['unit'])
                if any(r['offset'] <= a and b <= r['offset']+len(r['text']) for a, b in head)]
    if not involved or involved[-1]['unit']-involved[0]['unit'] > 3:
        return False
    first = involved[0]
    if not resolver.head_position(first, head[0][0]-first['offset']):
        return False
    if any(not _title_selected(resolver, row, head) for row in involved):
        return False
    # A separate next heading is not evidence of a bilingual counterpart.
    if any(r.get('kind') != 'paragraph' for r in involved[1:]):
        return False
    selected_units = {r['unit'] for r in involved}
    for unit in range(first['unit'], involved[-1]['unit']+1):
        row = resolver.rows.get(unit)
        if row is None or (unit not in selected_units and row['text'].strip()):
            return False
    for previous, current in zip(involved, involved[1:]):
        between = [resolver.rows[u] for u in range(previous['unit'], current['unit']+1)]
        if any(a['offset']+len(a['text']) != b['offset'] for a, b in zip(between, between[1:])):
            return False
    return True


def _selection_name(item):
    value = ' '.join(h['quote'] for h in item['head'])
    value = re.sub(r'^\s*#{1,6}\s+', '', value)
    value = re.sub(r'\s+#+\s*$', '', value)
    value = re.sub(r'\*\*|__|`', '', value)
    return re.sub(r'\s+', ' ', value).strip().casefold()


def repair_matches(original, fixed, part):
    """Accept only same-position identity, evidenced bilingual expansion, or
    a body mention moved back <=3 units to its same-name genuine heading.

    This is an identity check, not entry validation. Call ``validate_entry`` on
    an accepted repair; in particular body and language fields remain untrusted.
    """
    try:
        if not isinstance(original, dict) or not isinstance(fixed, dict):
            return False
        resolver = _Resolver(part)
        if not _same_book(original, fixed, part, *resolver.rows.values()):
            return False
        old = resolver.parts(original.get('head'))
        new = resolver.parts(fixed.get('head'))
        if not old or not new:
            return False
        old_chars, new_chars = _lexical(resolver.chars(old)), _lexical(resolver.chars(new))
        if not old_chars or not new_chars:
            return False
        retained = all(new_chars.get(pos) == char for pos, char in old_chars.items())
        if retained:
            added = {pos: char for pos, char in new_chars.items() if pos not in old_chars}
            if not added:
                return True
            scripts = {_script(char) for char in old_chars.values()}
            if scripts not in ({'latin'}, {'cjk'}) or not _title_cluster(resolver, new):
                return False
            opposite, field = ('cjk', 'name') if scripts == {'latin'} else ('latin', 'knowledge_point')
            evidence = resolver.parts(fixed.get(field), role='name', head_spans=new)
            supported = resolver.chars(evidence)
            return all(_script(char) == opposite and supported.get(pos) == char
                       for pos, char in added.items())

        old_unit, new_unit = original['head'][0]['unit'], fixed['head'][0]['unit']
        old_row, new_row = resolver.rows[old_unit], resolver.rows[new_unit]
        if (len(old) != 1 or not 1 <= old_unit-new_unit <= 3
                or old_row.get('kind') != 'paragraph'
                or resolver.head_position(old_row, old[0][0]-old_row['offset'])
                or new_row.get('kind') != 'heading'
                or new_row.get('excluded_zone') or new_row.get('head_role') == 'caption'
                or not _title_cluster(resolver, new)
                or _selection_name(original) != _selection_name(fixed)):
            return False
        return not any(r.get('kind') == 'heading' and new_unit < r['unit'] < old_unit
                       for r in resolver.rows.values())
    except (KeyError, IndexError, TypeError, ValueError):
        return False
