"""Literal V5 anchors, conservative name evidence, and repair identity.

The two entry APIs retain V4 signatures and the V1 output schema. All offsets
are half-open decoded-original character positions. ``occurrence`` indexes ALL
literal matches (including overlaps), before scope filtering. No translation,
OCR correction, PDF-text substitution, or first/last-match fallback is used.
Ambiguous or unsupported entries raise ValueError; callers must quarantine them.
"""
import hashlib
import re
import unicodedata


_FORMAT = frozenset(' \t\r\n#*_`~()[]{}:;,/\\|-"\''
                    '\uFF08\uFF09\u3010\u3011\uFF1A\uFF0C\u3001'
                    '\u2018\u2019\u201c\u201d')
_NON_HEAD_KINDS = {'gap', 'fence', 'code_block', 'table', 'html_block',
                   'math_block', 'bullet_list', 'ordered_list'}
_INTERNAL_ROLES = {'caption', 'secondary_style', 'internal_after_bilingual_main'}
_PREFIX = re.compile(r'[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*|__|`)?[ \t]*')
_NUMBERED_PREFIX = re.compile(_PREFIX.pattern + r'\d+(?:\.\d+)+[ \t]+(?:\*\*|__|`)?[ \t]*')
_NUMBERED_HEAD = re.compile(_PREFIX.pattern + r'(\d+(?:\.\d+)+)[ \t]+([^\r\n]+)')
_NUMBERED_PREDICATE = re.compile(r'(?<![\u3400-\u9fff])\u662f(?=[\u3400-\u9fff])')
_GUIDE = r'(?:See(?:[ \t]+also)?|\u53c2\u89c1|\u89c1)'
_DATES = re.compile(r'\([^()\n]*(?:\b\d{3,4}\b|\bb\.|\bd\.)[^()\n]*\)')
_PROSE = re.compile(
    r'\b(?:is|are|was|were)\s+(?:an?\s+|the\s+|known\b|used\b)|'
    r'\b(?:known for|refers to|born in|used for|defined as)\b|'
    r'\u662f(?:\u4e00[\u79cd\u4f4d\u540d\u4e2a]|\u6307)|'
    r'(?:\u6307\u7684\u662f|\u7528\u4e8e|\u51fa\u751f\u4e8e|\u751f\u4e8e)', re.I)
_PROFESSION = re.compile(
    r'\b(?:pianist|composer|conductor|theorist|philologist|assyriologist|'
    r'scholar|physician|scientist|writer|poet|politician|artist|historian|'
    r'philosopher|mathematician|engineer|professor)\b', re.I)
_NATIONALITY = re.compile(
    r'\b(?:American|Swiss|British|English|French|German|Italian|Russian|'
    r'Chinese|Japanese|Semitic|classical)\b', re.I)
_SECTION = re.compile(r'^(?:section|chapter|part|history|introduction|overview|'
                      r'definition|references|bibliography|further reading|'
                      r'see also|conceptual overview|intellectual work)\b|'
                      r'^(?:\u6982\u8ff0|\u5386\u53f2|\u53c2\u8003\u6587\u732e|\u7b2c.+[\u7ae0\u8282])', re.I)


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


def _script(char):
    if '\u3400' <= char <= '\u9fff':
        return 'cjk'
    if 'LATIN' in unicodedata.name(char, ''):
        return 'latin'
    return 'other'


def _scripts(value):
    return {_script(c) for c in value if _script(c) != 'other'}


def _lexical(chars):
    return {pos: char for pos, char in chars.items() if char not in _FORMAT}


def _word_cut(left, right):
    return (left.isalnum() and (right.isalnum() or right == '_')
            and not {_script(left), _script(right)} == {'latin', 'cjk'})


class _Resolver:
    def __init__(self, part, text=None):
        if not isinstance(part, dict) or not isinstance(part.get('units'), list):
            raise ValueError('Expected a source packet with units')
        self.rows, self.original_text = {}, text
        for row in part['units']:
            if not isinstance(row, dict):
                raise ValueError('Invalid source unit')
            unit, offset, value = row.get('unit'), row.get('offset'), row.get('text')
            if (type(unit) is not int or unit < 0 or unit in self.rows
                    or type(offset) is not int or offset < 0 or not isinstance(value, str)):
                raise ValueError('Invalid or duplicate source unit/offset')
            if text is not None and (offset > len(text) or text[offset:offset + len(value)] != value):
                raise ValueError('Source unit does not match original text at its offset')
            self.rows[unit] = row
        self.ordered = sorted(self.rows.values(), key=lambda r: r['offset'])
        if any(a['offset'] + len(a['text']) > b['offset']
               for a, b in zip(self.ordered, self.ordered[1:])):
            raise ValueError('Overlapping source units')

    def slice(self, a, b):
        if self.original_text is not None:
            return self.original_text[a:b]
        pieces, cursor = [], a
        for row in self.ordered:
            lo, hi = row['offset'], row['offset'] + len(row['text'])
            if hi <= cursor or lo >= b:
                continue
            if lo > cursor:
                raise ValueError('Source gap has no literal evidence')
            end = min(hi, b)
            pieces.append(row['text'][cursor - lo:end - lo])
            cursor = end
        if cursor != b:
            raise ValueError('Selection is not covered by source units')
        return ''.join(pieces)

    def row_at(self, pos):
        row = next((r for r in self.ordered
                    if r['offset'] <= pos < r['offset'] + len(r['text'])), None)
        if row is None:
            raise ValueError('Position is not covered by a source unit')
        return row

    def head_position(self, row, start, end=None):
        if (row.get('kind') in _NON_HEAD_KINDS or row.get('excluded_zone')
                or row.get('head_role') == 'caption'):
            return False
        absolute = row['offset'] + start
        if self.original_text is not None:
            value, local = self.original_text, absolute
        else:
            # Recover an actual line prefix across packet units, never treat a
            # split mid-line unit as a new heading.
            first = self.ordered.index(row)
            while first and (self.ordered[first - 1]['offset']
                             + len(self.ordered[first - 1]['text']) == self.ordered[first]['offset']):
                if self.ordered[first - 1]['text'].endswith(('\n', '\r')):
                    break
                first -= 1
            base = self.ordered[first]['offset']
            value = self.slice(base, row['offset'] + len(row['text']))
            local = absolute - base
        prefix = value[value.rfind('\n', 0, local) + 1:local]
        # Missing Markdown markers need bilingual source text and a numbered peer.
        numbered=(_NUMBERED_PREFIX.fullmatch(prefix) and self.numbered_head(row))
        if not (_PREFIX.fullmatch(prefix) or numbered
                or re.fullmatch(_PREFIX.pattern + _GUIDE + r'[ \t]+', prefix, re.I)):
            return False
        if end is not None:
            boundary = local + end - start
            if boundary < len(value) and _word_cut(value[boundary - 1], value[boundary]):
                return False
        return True

    def numbered_head(self, row):
        if row.get('head_role') in _INTERNAL_ROLES or row.get('excluded_zone'):
            return False
        if row.get('kind') == 'heading':
            return True
        if row.get('kind') != 'paragraph':
            return False
        current = _NUMBERED_HEAD.fullmatch(row['text'].strip())
        # Inline definitions may share this line; selected heads are checked separately.
        if not current or _scripts(current[2]) != {'cjk', 'latin'}:
            return False
        number = current[1].split('.')
        for peer in self.ordered:
            if (abs(peer['unit'] - row['unit']) > 12 or peer.get('kind') != 'heading'
                    or peer.get('head_role') in _INTERNAL_ROLES or peer.get('excluded_zone')):
                continue
            match = _NUMBERED_HEAD.fullmatch(peer['text'].strip())
            if not match or _scripts(match[2]) != {'cjk', 'latin'} or _prose(match[2]):
                continue
            other = match[1].split('.')
            if (other[:-1] == number[:-1] and 0 < abs(int(other[-1]) - int(number[-1])) <= 3):
                return True
        return False

    def matches(self, item):
        if not isinstance(item, dict):
            raise ValueError('Expected a literal source selection')
        unit, quote = item.get('unit'), item.get('quote')
        if type(unit) is not int or unit not in self.rows:
            raise ValueError('Selection unit is missing or invalid')
        if not isinstance(quote, str) or not quote:
            raise ValueError('Selection quote must be a nonempty literal string')
        row = self.rows[unit]
        if row.get('excluded_zone'):
            raise ValueError('Selection lies in an excluded source region')
        matches, cursor = [], 0
        while True:
            start = row['text'].find(quote, cursor)
            if start < 0:
                break
            matches.append((row['offset'] + start, row['offset'] + start + len(quote)))
            cursor = start + 1
        if not matches:
            raise ValueError('Quote is absent from its source unit')
        if 'occurrence' in item:
            occurrence = item['occurrence']
            if type(occurrence) is not int or not 0 <= occurrence < len(matches):
                raise ValueError('occurrence must index a literal match (zero-based)')
            return [matches[occurrence]]
        return matches

    def resolve(self, item, *, role='head', head_spans=(), after=None, before=None):
        if role not in ('head', 'name', 'body_start', 'body_end'):
            raise ValueError('Unknown selection role')
        if any(bound is not None and (type(bound) is not int or bound < 0)
               for bound in (after, before)):
            raise ValueError('Invalid selection bound')
        candidates = [span for span in self.matches(item)
                      if (after is None or span[0] >= after)
                      and (before is None or span[1] <= before)
                      and (role != 'name' or any(a <= span[0] and span[1] <= b for a, b in head_spans))]
        if role == 'head' and len(candidates) > 1 and 'occurrence' not in item:
            row = self.rows[item['unit']]
            candidates = [m for m in candidates
                          if self.head_position(row, m[0] - row['offset'], m[1] - row['offset'])]
        if len(candidates) != 1:
            raise ValueError('Ambiguous or out-of-scope quote; supply occurrence')
        return candidates[0]

    def parts(self, items, **scope):
        if not isinstance(items, list):
            raise ValueError('Expected list of source selections')
        spans = [self.resolve(item, **scope) for item in items]
        _ordered(spans)
        return spans

    def chars(self, spans):
        result = {}
        for a, b in spans:
            result.update((a + i, char) for i, char in enumerate(self.slice(a, b)))
        return result


def _ordered(spans):
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError('Unordered or overlapping source selections')


def resolve_selection(selection, part, *, role='head', head_spans=(), after=None):
    """V4-compatible helper; body roles use only unique in-scope literal matches.

    Entry-level next-head bounds are established only by ``validate_entry``.
    """
    return _Resolver(part).resolve(selection, role=role, head_spans=head_spans, after=after)


def _plain(value):
    value = re.sub(r'^\s*#{1,6}\s+', '', value)
    return re.sub(r'\*\*|__|`', '', value).strip()


def _biography_start(value):
    """A literal personal-name/date prefix, not a global occupation blacklist."""
    match = _DATES.search(value)
    if match and re.match(r'[^()\n,]+,[^()\n,]+$', value[:match.start()].strip()):
        return match.start(), match.end()
    return None


def _prose(value):
    value = _plain(value)
    biography = _biography_start(value)
    if biography and value[biography[1]:].strip(' \t\r\n.,;:'):
        return True
    return bool(_PROSE.search(value) or
                (_NATIONALITY.search(value) and _PROFESSION.search(value)
                 and (value.endswith('.') or ',' in value)))


def _gap_ok(resolver, left, right, *, explicit=False):
    gap = resolver.slice(left, right)
    return (all(c in _FORMAT for c in gap)
            and (explicit or not re.search(r'\r?\n[ \t\r]*\n', gap))
            and (explicit or '#' not in gap))


def _head_cluster(resolver, head, *, check_position=True, explicit=False):
    if not head:
        raise ValueError('Empty head')
    _ordered(head)
    for a, b in head:
        row = resolver.row_at(a)
        if (row.get('kind') in _NON_HEAD_KINDS or row.get('excluded_zone')
                or row.get('head_role') == 'caption'):
            raise ValueError('Head uses a non-head source region')
        # No subsequent Markdown heading is an automatic bilingual counterpart.
        if (not explicit and row['unit'] != resolver.row_at(head[0][0])['unit']
                and re.match(r'\s*#{1,6}\s+', row['text'])):
            raise ValueError('Head crosses a separate source heading')
        if not explicit and '\n\n' in resolver.slice(a, b).replace('\r\n', '\n'):
            raise ValueError('Head crosses a paragraph boundary')
    for previous, current in zip(head, head[1:]):
        if not _gap_ok(resolver, previous[1], current[0], explicit=explicit):
            raise ValueError('Head crosses prose, paragraph, or another heading')
        gap = resolver.slice(previous[1], current[0])
        if '\n' in gap and _scripts(resolver.slice(*previous)) != _scripts(resolver.slice(*current)):
            row = resolver.row_at(current[0])
            tail = row['text'][current[1] - row['offset']:].split('\n', 1)[0]
            if re.match(r'\s*(?:is|are|was|were|means|defines|refers)\b', tail, re.I):
                raise ValueError('Crossline counterpart is the prefix of a definition')
    first = resolver.row_at(head[0][0])
    if check_position and not resolver.head_position(first, head[0][0] - first['offset'], head[0][1] - first['offset']):
        raise ValueError('First head selection is not at a supported source head position')
    title = ' '.join(resolver.slice(a, b) for a, b in head)
    if (first.get('kind') == 'paragraph' and _NUMBERED_HEAD.fullmatch(first['text'].strip())):
        if _scripts(title) != {'cjk', 'latin'}:
            raise ValueError('Numbered paragraph head requires original bilingual head evidence')
        if _NUMBERED_PREDICATE.search(_plain(title)):
            raise ValueError('Numbered paragraph head includes a definition predicate')
    if _prose(title) or re.search(r'(?:^|\s)' + _GUIDE + r'(?:\s|$)', _plain(title), re.I):
        raise ValueError('Head includes an introduction or reference guide, not just a headword')


def _adjacent_translation(resolver, head, span):
    if any(a <= span[0] and span[1] <= b for a, b in head):
        return True
    title = ''.join(resolver.slice(a, b) for a, b in head)
    scripts = _scripts(title)
    added = resolver.slice(*span)
    if scripts not in ({'latin'}, {'cjk'}) or _scripts(added) != ({'latin', 'cjk'} - scripts):
        return False
    if _prose(added):
        return False
    trial = sorted([*head, span])
    try:
        _head_cluster(resolver, trial, check_position=False)
        # A quote must include the full contiguous name, not a prefix of a
        # Chinese introduction or a chopped English/OCR word.
        row = resolver.row_at(span[0])
        value = row['text']
        a, b = span[0] - row['offset'], span[1] - row['offset']
        if a and _word_cut(value[a - 1], value[a]):
            return False
        if b < len(value) and _word_cut(value[b - 1], value[b]):
            return False
        if '\n' in resolver.slice(trial[0][1], trial[-1][0]):
            tail = value[b:].split('\n', 1)[0]
            if any(c not in _FORMAT for c in tail):
                return False
        return True
    except ValueError:
        return False


def _fields_and_head(resolver, item, head):
    fields, expanded = {}, list(head)
    for key in ('knowledge_point', 'name'):
        selections = item.get(key)
        if not isinstance(selections, list):
            raise ValueError('Expected list of original-language name selections')
        fields[key] = []
        for selection in selections:
            candidates = [m for m in resolver.matches(selection)
                          if _adjacent_translation(resolver, expanded, m)]
            if len(candidates) != 1:
                raise ValueError('Name has no unique headword or adjacent original translation evidence')
            chosen = candidates[0]
            fields[key].append(chosen)
            if not any(a <= chosen[0] and chosen[1] <= b for a, b in expanded):
                expanded = sorted([*expanded, chosen])
        _ordered(fields[key])
    _head_cluster(resolver, expanded, explicit=True)
    if not any(fields.values()):
        raise ValueError('At least one original-language name required')

    for spans in fields.values():
        chars = _lexical(resolver.chars(spans))
        if not chars:
            if spans:
                raise ValueError('Name contains no headword characters')
            continue
        if _prose(' '.join(resolver.slice(a, b) for a, b in spans)):
            raise ValueError('Name is an introduction, not a headword')
        for a, b in spans:
            if ((a and _word_cut(resolver.slice(a - 1, a), resolver.slice(a, a + 1)))
                    or (b < len(resolver.original_text or '')
                        and _word_cut(resolver.slice(b - 1, b), resolver.slice(b, b + 1)))):
                raise ValueError('Name selection cuts inside an original word')
    return expanded, fields


def _next_head(resolver, head):
    """Only corroborated lexical heads bound bodies; Markdown alone does not.

    Flattened section headings are common. Bilingual source names, personal
    name/date syntax, or a matching established main/caps style supply the
    extra lexical evidence. Missing evidence leaves the quote ambiguous.
    """
    first = resolver.row_at(head[0][0])
    own = re.match(r'\s*(#{1,6})\s+', first['text'])
    level = len(own[1]) if own else None
    numbered_own = _NUMBERED_HEAD.fullmatch(first['text'].strip())
    for row in resolver.ordered:
        if (row.get('excluded_zone') or row.get('kind') in _NON_HEAD_KINDS
                or row.get('head_role') in _INTERNAL_ROLES):
            continue
        if (numbered_own and row['offset'] >= head[-1][1]
                and row.get('kind') == 'paragraph' and resolver.numbered_head(row)):
            numbered = _NUMBERED_HEAD.fullmatch(row['text'].strip())
            if len(numbered[1].split('.')) <= len(numbered_own[1].split('.')):
                return row['offset']
        for match in re.finditer(r'(?m)^[ \t]*(#{1,6})[ \t]+([^\r\n]+)', row['text']):
            pos = row['offset'] + match.start()
            if pos < head[-1][1] or (level is not None and len(match[1]) > level):
                continue
            value = _plain(match[2])
            if _SECTION.search(value):
                continue
            letters = ''.join(c for c in value if _script(c) == 'latin')
            current = ''.join(resolver.slice(a, b) for a, b in head)
            primary_caps = (first.get('head_role') == row.get('head_role') == 'main_style'
                            and letters.isupper()
                            and ''.join(c for c in current if _script(c) == 'latin').isupper())
            lexical = (_scripts(value) == {'cjk', 'latin'} or _biography_start(value)
                       or primary_caps)
            if lexical and not _prose(value) and value:
                return pos
    return None


def validate_entry(item, part, text, book):
    """Build the V4-compatible entry or raise ValueError for review/quarantine.

    A field can extend an omitted head only with unique adjacent literal
    bilingual evidence. Body quotes resolve after the head/prior range and
    before a clear next source head; remaining ambiguity is never guessed.
    Neither input nor source metadata is mutated. PDF format is only a hint.
    """
    if not isinstance(item, dict) or not isinstance(text, str) or not isinstance(book, dict):
        raise ValueError('Invalid entry, original text, or book schema')
    if not all(isinstance(book.get(k), str) and book[k] for k in ('identifier', 'md_sha256')):
        raise ValueError('Book identifier and MD hash are required')
    resolver = _Resolver(part, text)
    if not _same_book(item, part, book, *resolver.rows.values()):
        raise ValueError('Cross-book source identity mismatch')
    head, fields = _fields_and_head(resolver, item, resolver.parts(item.get('head')))
    lo, hi = part.get('lo'), part.get('hi')
    first_unit = resolver.row_at(head[0][0])['unit']
    if (type(lo) is not int or type(hi) is not int or lo < 0 or hi <= lo
            or not lo <= first_unit < hi):
        raise ValueError('Head outside ownership')
    if not isinstance(item.get('body'), list):
        raise ValueError('Expected body range list')
    body, end, boundary = [], head[-1][1], _next_head(resolver, head)
    for span in item['body']:
        if not isinstance(span, dict):
            raise ValueError('Expected body start/end selections')
        a = resolver.resolve(span.get('start'), role='body_start', after=end, before=boundary)[0]
        b = resolver.resolve(span.get('end'), role='body_end', after=a, before=boundary)[1]
        if b <= a:
            raise ValueError('Body range is empty or reversed')
        if ((a and _word_cut(text[a - 1], text[a]))
                or (b < len(text) and _word_cut(text[b - 1], text[b]))):
            raise ValueError('Body range cuts inside an original word')
        if any(r.get('excluded_zone') and a < r['offset'] + r.get('source_length', len(r['text']))
               and r['offset'] < b for r in resolver.ordered):
            raise ValueError('Body crosses an excluded source region')
        body.append((a, b))
        end = b
    if type(item.get('body_complete')) is not bool:
        raise ValueError('body_complete must be boolean')
    identity = f"{book['identifier']}:{book['md_sha256']}:{head[0][0]}"
    return dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24],
                **{key: ' '.join(text[a:b] for a, b in spans) for key, spans in fields.items()},
                head=' '.join(text[a:b] for a, b in head),
                raw_content='\n\n'.join(text[a:b] for a, b in body),
                body_complete=item['body_complete'],
                leading_context=text[max(0, head[0][0] - 600):head[0][0]],
                trailing_context=text[end:end + 1200],
                source={**book, 'head_spans': head, 'body_spans': body},
                ready_for_delivery=False)


def _identity_core(resolver, head):
    chars = _lexical(resolver.chars(head))
    a, b = head[0][0], head[-1][1]
    value = resolver.slice(a, b)
    prefix = re.match(r'\s*' + _GUIDE + r'[ \t]+', value, re.I)
    if prefix:
        chars = {p: c for p, c in chars.items() if p >= a + prefix.end()}
    suffix = re.search(r'[ \t]+' + _GUIDE + r'\s*$', value, re.I)
    row = resolver.row_at(b - 1)
    tail = row['text'][b - row['offset']:]
    if suffix and re.match(r'[ \t]+\S', tail):
        chars = {p: c for p, c in chars.items() if p < a + suffix.start()}
    biography = _biography_start(_plain(value))
    if biography:
        match = _DATES.search(value)
        chars = {p: c for p, c in chars.items() if p < a + match.start()}
    return chars


def _same_heading_recovery(resolver, original, fixed, old, new):
    old_unit, new_unit = original['head'][0]['unit'], fixed['head'][0]['unit']
    old_row, new_row = resolver.rows[old_unit], resolver.rows[new_unit]
    if (len(old) != 1 or not 1 <= old_unit - new_unit <= 3
            or old_row.get('kind') != 'paragraph' or new_row.get('kind') != 'heading'
            or resolver.head_position(old_row, old[0][0] - old_row['offset'])
            or _plain(' '.join(resolver.slice(a, b) for a, b in old)).casefold()
            != _plain(' '.join(resolver.slice(a, b) for a, b in new)).casefold()):
        return False
    literal = _lexical({new_row['offset'] + i: c for i, c in enumerate(new_row['text'])})
    # Markdown markers are ignored, but a heading containing a definition is
    # not the same-name recovery promised by V4.
    selected = _lexical(resolver.chars(new))
    if any(selected.get(p) != c for p, c in literal.items()):
        return False
    return not any(r.get('kind') == 'heading' and new_unit < r['unit'] < old_unit
                   for r in resolver.ordered)


def check_repair_identity(original, fixed, part):
    """Return identity compatibility; raise source/structure failures separately.

    Same-position formatting, evidenced guide/biography cropping, and adjacent
    original bilingual extension are allowed. Arbitrary lexical cropping,
    replacement, translation, and relocation to another occurrence are not.
    V4's bounded same-name body-mention-to-real-heading recovery is retained.
    """
    if not isinstance(original, dict) or not isinstance(fixed, dict):
        raise ValueError('Expected original and repaired entry objects')
    resolver = _Resolver(part)
    if not _same_book(original, fixed, part, *resolver.rows.values()):
        return False
    old, new = resolver.parts(original.get('head')), resolver.parts(fixed.get('head'))
    if not old or not new:
        raise ValueError('Empty repair head')
    _head_cluster(resolver, new, explicit=True)
    old_chars, new_chars = _lexical(resolver.chars(old)), _lexical(resolver.chars(new))
    if not old_chars or not new_chars:
        raise ValueError('Repair head contains no headword characters')
    core = _identity_core(resolver, old)
    if not core or any(new_chars.get(p) != c for p, c in core.items()):
        return _same_heading_recovery(resolver, original, fixed, old, new)
    added = {p: c for p, c in new_chars.items() if p not in old_chars}
    if not added:
        return True

    scripts = _scripts(''.join(core.values()))
    if scripts not in ({'latin'}, {'cjk'}):
        return False
    opposite = {'latin', 'cjk'} - scripts
    field = 'name' if opposite == {'cjk'} else 'knowledge_point'
    evidence = resolver.parts(fixed.get(field), role='name', head_spans=new)
    supported = resolver.chars(evidence)
    added_spans = [(a, b) for a, b in evidence if any(a <= p < b for p in added)]
    core_spans = [(min(core), max(core) + 1)]
    return (all(_script(c) in opposite and supported.get(p) == c for p, c in added.items())
            and all(_adjacent_translation(resolver, core_spans, span) for span in added_spans))


def repair_matches(original, fixed, part):
    """Compatibility predicate; a True repair still requires ``validate_entry``."""
    try:
        return check_repair_identity(original, fixed, part)
    except (KeyError, IndexError, TypeError, ValueError):
        return False
