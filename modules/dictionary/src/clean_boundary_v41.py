"""Bounded v4.1 boundary regression over frozen v3.1 cohorts."""
import argparse
import copy
import json
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

import clean_boundary_v3 as boundary_v3
import clean_boundary_v4 as v4
import clean_compare1000 as c


ABBREVIATIONS = frozenset(v4.ABBREVIATIONS)
LOWERCASE_CONTINUATION_ABBREVIATIONS = frozenset({'sk', 'bsm'})
POLICY = v4.POLICY


def prose(raw):
    ends = []
    for match in re.finditer(r'[。！？]+[”’」』）]*|[.!?]+["\u201d\u2019\')\]]*(?=\s|$)', raw):
        end = match.end()
        prefix = raw[:end].rstrip("\"\u201d\u2019')]")
        if prefix.endswith('.'):
            punctuation = match.group().rstrip("\"\u201d\u2019')]")
            if (re.search(r'\.\s*\.$', prefix) or re.match(r'\s*\.', raw[end:])
                    or len(punctuation) > 1):
                continue
            token = re.search(r'([A-Za-z.]+)\.$', prefix)
            if token:
                abbreviation = token.group(1).lower()
                if abbreviation in ABBREVIATIONS:
                    continue
                if (abbreviation in LOWERCASE_CONTINUATION_ABBREVIATIONS
                        and re.match(r'\s+[a-z]', raw[end:])):
                    continue
            if re.search(r'(?:\b[A-Za-z]\.){2,}$', prefix) or re.search(r'\b[A-Z]\.$', prefix):
                continue
        if raw[:end].count('\u201c') > raw[:end].count('\u201d') or raw[:end].count('(') > raw[:end].count(')'):
            continue
        ends.append(end)
    if not ends or ends[-1] != len(raw):
        ends.append(len(raw))
    start = 0
    for end in ends:
        if raw[start:end].strip():
            yield {'start': start, 'end': end, 'text': raw[start:end]}
        start = end


def units(raw, layout_texts=frozenset()):
    pieces = []
    start = 0
    for match in re.finditer(r'(?m)^[^\n]+$', raw):
        text = match.group().strip()
        if (text in layout_texts or re.match(r'^#{1,6}\s+\S', text)
                or re.fullmatch(r'[（(](?:[\u3400-\u9fff][ \t]*){2,4}[）)]', text)):
            pieces.extend([(start, match.start(), False), (match.start(), match.end(), True)])
            start = match.end()
    pieces.append((start, len(raw), False))
    result = []
    for first, last, layout in pieces:
        if not raw[first:last].strip():
            continue
        parts = ([{'start': 0, 'end': last-first, 'text': raw[first:last]}]
                 if layout else prose(raw[first:last]))
        for unit in parts:
            result.append({
                **unit,
                'unit': len(result),
                'start': first + unit['start'],
                'end': first + unit['end'],
                'layout': layout,
            })
    return result


def project(mapped, vote, layout_texts=frozenset()):
    sentence_units = units(mapped.text, layout_texts)
    if (not isinstance(vote, dict) or not isinstance(vote.get('ranges'), list)
            or vote.get('exclude_units') != []):
        raise ValueError('Expected complete ranges and empty exclusions')
    intervals, edits = {}, []
    previous_last = -1
    for selected in vote['ranges']:
        first = selected.get('first')
        last = selected.get('last')
        language = selected.get('language')
        if (type(first) is not int or type(last) is not int
                or not 0 <= first <= last < len(sentence_units)
                or first <= previous_last or language not in {'zh', 'en'}):
            raise ValueError('Invalid, overlapping or unordered range')
        previous_last = last
        blocked_by_incomplete = False
        for unit in sentence_units[first:last+1]:
            if blocked_by_incomplete:
                edits.append({
                    'unit': unit['unit'],
                    'reason': 'avoid_bridge_after_incomplete',
                    'text': unit['text'],
                })
                continue
            if unit['layout']:
                edits.append({'unit': unit['unit'], 'reason': 'layout', 'text': unit['text']})
                continue
            if not boundary_v3.complete_sentence(unit['text']):
                edits.append({'unit': unit['unit'], 'reason': 'incomplete_sentence', 'text': unit['text']})
                blocked_by_incomplete = True
                continue
            start, end = unit['start'], unit['end']
            while start < end and mapped.text[start].isspace():
                start += 1
            while end > start and mapped.text[end-1].isspace():
                end -= 1
            ranges = intervals.setdefault(language, [])
            if ranges and not mapped.text[ranges[-1][1]:start].strip():
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
    return {language: mapped.select(ranges) for language, ranges in intervals.items()}, edits


def load_compatible_config(bases, workers):
    configs = [c.read(base/'CONFIG.json') for base in bases]
    if len(configs) != 2 or configs[0] != configs[1]:
        raise ValueError('Base CONFIG mismatch')
    hashes = {str(base/'CONFIG.json'): c.digest(base/'CONFIG.json') for base in bases}
    effective = copy.deepcopy(configs[0])
    effective.update(workers=workers, max_attempts=2)
    return effective, hashes


class Runner(v4.Runner):
    def select(self, row, stage, mapped, prompt):
        if not mapped.text.strip():
            return {}
        layouts = v4.layout_evidence(row)
        instruction = (
            '选属于当前词条的解释性正文，可保留完整且明确的独立参见句。'
            if stage == 'explanation' else
            '选直接界定当前词条是什么、核心属性或含义的完整句。地理分布、历史背景、评价、用途举例不单独作定义。'
            '需要前句才成立的指代或对比句不可单独选择，可连同必要完整界定句一起选；没有可靠定义就空ranges。'
        )
        packet = {'subject': row['head'], 'units': units(mapped.text, layouts)}
        if stage == 'explanation':
            packet['source_context'] = row['source_context']
        response = self.call(
            row['id'], stage, packet, POLICY+instruction+c.SELECT_SCHEMA,
            lambda value: project(mapped, value, layouts),
        )
        if response['status'] == 'failed':
            raise RuntimeError(stage+' failed after two attempts')
        result, edits = project(mapped, response['vote'], layouts)
        c.write(self.out/'sentence_guards'/stage/(row['id']+'.json'), {'edits': edits})
        return result


def run(bases, out, workers):
    out.mkdir(parents=True, exist_ok=True)
    rows, decisions, old_content = [], {}, {}
    for base in bases:
        rows.extend(c.read(base/'INPUT.json'))
        for path in (base/'01_structure').glob('*.json'):
            value = c.read(path)
            if value['id'] in decisions:
                raise ValueError('Duplicate source ID')
            decisions[value['id']] = value
        for path in (base/'02_content').glob('*.json'):
            value = c.read(path)
            old_content[value['id']] = value
    assert len(rows) == len({row['id'] for row in rows}) == len(decisions) == 270
    cfg, config_hashes = load_compatible_config(bases, workers)
    with urllib.request.urlopen(cfg['api_url'].rstrip('/')+'/v1/models', timeout=30) as response:
        models = json.load(response)
    assert any(model['id'] == cfg['model'] for model in models['data']), 'Configured model unavailable'
    files = ['clean_boundary_v41.py', 'clean_boundary_v4.py', 'clean_boundary_v3.py', 'clean_compare1000.py']
    source_hashes = dict(config_hashes)
    for base in bases:
        paths = [
            base/'INPUT.json',
            *sorted((base/'01_structure').glob('*.json')),
            *sorted((base/'02_content').glob('*.json')),
        ]
        source_hashes.update({str(path): c.digest(path) for path in paths})
    manifest = {
        'bases': [str(base) for base in bases],
        'source_hashes': source_hashes,
        'code_hashes': {name: c.digest(Path(__file__).parent/name) for name in files},
        'config': cfg,
    }
    if (out/'MANIFEST.json').exists():
        assert c.read(out/'MANIFEST.json') == manifest, 'Resume mismatch'
    else:
        c.write(out/'MANIFEST.json', manifest)
        c.write(out/'INPUT.json', rows)
        c.write(out/'CONFIG.json', cfg)
    c.write(out/'MODEL_PROBE.json', models)
    runner = Runner(out, cfg)
    allowed = [row for row in rows if decisions[row['id']]['status'] == 'keep']
    start = time.time()
    results = runner.batch(allowed, '02_content', runner.content)
    kept = [result['record'] for result in results if result['status'] == 'keep']
    c.write(out/'FINAL_RECORDS.json', kept)
    ledger = {
        sid: 'structure_'+decision['status']
        for sid, decision in decisions.items() if decision['status'] != 'keep'
    }
    ledger.update({
        result['id']: result['status'] if result['status'] == 'keep' else 'content_'+result['status']
        for result in results
    })
    c.write(out/'LEDGER.json', [
        {'id': sid, 'status': status} for sid, status in sorted(ledger.items())
    ])
    changes = []
    for result in results:
        old = old_content[result['id']]
        old_fields = {field: old.get('record', {}).get(field, '') for field in c.FIELDS}
        new_fields = {field: result.get('record', {}).get(field, '') for field in c.FIELDS}
        if old['status'] != result['status'] or old_fields != new_fields:
            changes.append({
                'id': result['id'],
                'before_status': old['status'],
                'after_status': result['status'],
                'before': old_fields,
                'after': new_fields,
            })
    c.write(out/'CHANGES.json', changes)
    summary = {
        'input': len(rows),
        'content_input': len(allowed),
        'statuses': dict(Counter(ledger.values())),
        'kept': len(kept),
        'with_content': sum(any(row[field] for field in c.FIELDS) for row in kept),
        'with_definition': sum(bool(row['definition'] or row['en_definition']) for row in kept),
        'changed': len(changes),
        'elapsed_seconds': round(time.time()-start, 2),
    }
    c.write(out/'SUMMARY.json', summary)
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bases', nargs=2, type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1024)
    args = parser.parse_args()
    if not 1 <= args.workers <= 1024:
        parser.error('workers must be between 1 and 1024')
    run(args.bases, args.out, args.workers)
