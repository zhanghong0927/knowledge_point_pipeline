"""N3 条列·步骤规则型规则抽取器：保留编号步骤、规则及其续行。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import csv
import jieba
import jieba.posseg as pseg
jieba.setLogLevel(40)
from pathlib import Path
from typing import Any


VERSION = "n3-rule-concept-extractor-2.0"
MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
CHAPTER_START = re.compile(r"^(?:第\s*[零〇一二三四五六七八九十百千万\d]+\s*[章节篇部卷编]|chapter\s+\d+|part\s+[ivx\d]+)", re.I)
NUMBERED_ITEM = re.compile(
    r"^\s*(?P<marker>(?:第\s*[零〇一二三四五六七八九十百千万\d]+\s*[条款]|"
    r"[（(][一二三四五六七八九十百\d]+[）)]|(?:\d+\.){1,5}\d*|\d+[、.)）．]|[一二三四五六七八九十百]+、))\s*"
    r"(?P<body>\S.*)$"
)
LEAD_IN = re.compile(r"^\s*(?:首先|其次|再次|最后|第一|第二|第三|first(?:ly)?|second(?:ly)?|third(?:ly)?|finally)[，,:：、\s]+", re.I)
RULE_LEAD = re.compile(r"^\s*(?:规则|要求|注意(?:事项)?|警告|禁止|例外|条件|必须|应当|不得|严禁|须知|rule|requirement|note|warning|exception|must|shall|should|do not|never)\s*[：:，,、]?", re.I)
IMAGE_ONLY = re.compile(r"(?:!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>)", re.I)
SKIP_LABELS = {"目录", "目次", "contents", "table of contents", "前言", "序言", "序", "preface", "foreword", "版权", "copyright", "参考文献", "参考书目", "references", "bibliography", "索引", "index", "附录", "appendix", "appendices"}

# Only literal names are accepted; uncertain items are retained for review.
GENERIC = {'注意', '注意事项', '要求', '规则', '条件', '例外', '方法', '内容', '其他', '概述', '基本要求', '相关要求', '步骤', '首先', '其次', '最后'}
ACTION = re.compile(r'^(?:应|须|要|可|能|必须|不得|禁止|严禁|检查|确认|了解|掌握|提高|加强|采取|建立|做好|进行|由于|为了|如果|当|在|对|从|把|将|使|这|它|其|我们|check\b|verify\b|stop\b|ensure\b|must\b|should\b|do\b)', re.I)
RULE_SIGNAL = re.compile(r'必须|应当|不得|禁止|严禁|应遵循|须|\b(?:must|shall|should|never)\b', re.I)
STEP_SIGNAL = re.compile(r'^(?:首先|其次|再次|最后|第\s*\d+\s*步|step\s*\d+|first(?:ly)?\b|second(?:ly)?\b|finally\b)', re.I)
TABLE = re.compile(r'^\s*(?:\||</?(?:table|tr|td|th)\b)', re.I)
TOC_ROW = re.compile(r'(?:\.{2,}|…|⋯).*\d\s*[）)]?\s*$')
NOUN_END = re.compile(r'[\u4e00-\u9fff]{1,14}(?:制度|原则|机制|模型|理论|方法|流程|条件|政策|管理|组织|结构|责任制|需求|需要|目标|职能|决策|评估|控制|计划)')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def clean(text):
    text = IMAGE_ONLY.sub('', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return re.sub(r'\s+', ' ', re.sub(r'[`*_~]', '', text)).strip()


def label(title):
    return re.sub(r'[\s:：.。—–-]+', '', title).casefold()


def get_heading(line):
    match = MD_HEADING.fullmatch(line)
    if match:
        return len(match[1]), match[2].strip()
    title = line.strip()
    if len(title) <= 60 and CHAPTER_START.match(title) and not TOC_ROW.search(title):
        return 2, title
    return None


def marker_depth(marker):
    if re.match(r'^第', marker) or re.match(r'^[一二三四五六七八九十百]+、', marker):
        return 1
    if marker.startswith(('(', '（')):
        return 3
    return 1 + len(re.findall(r'\d+', marker))


def item_kind(text, numbered=False):
    if STEP_SIGNAL.match(text):
        return 'step'
    if RULE_SIGNAL.search(text) or RULE_LEAD.match(text):
        return 'rule'
    return 'enumeration' if numbered else None


def is_noun_name(term):
    if not 2 <= len(term) <= 20 or term in GENERIC or ACTION.search(term):
        return False
    if re.search(r'[。！？；：:;!?\d]|是指|指的是|包括|应当|必须|不得|可以|能够', term):
        return False
    if re.search(r'[\u4e00-\u9fff]', term):
        tokens = list(pseg.cut(term))
        nominal = {'管理', '组织', '控制', '计划', '决策', '需要', '需求', '评估', '领导'}
        return bool(tokens) and all(t.flag.startswith('n') or t.flag in {'a', 'b', 'f', 'eng'} or t.word in nominal or t.word == '的' for t in tokens) and (tokens[-1].flag.startswith('n') or tokens[-1].word in nominal)
    return bool(re.fullmatch(r'[A-Za-z]+(?:[ -][A-Za-z]+){0,5}', term)) and not re.search(r'\b(is|are|has|to|and then|before|after)\b', term, re.I)


def name_item(body):
    # Name only from the opening clause; do not promote incidental later topics.
    head = body.split('\n', 1)[0].strip()
    first = re.split(r'[：:。；;（(]', head, maxsplit=1)[0].strip(' “”。"')
    if is_noun_name(first):
        return first, 'explicit_item_title'
    definition = re.match(r'^(?:所谓)?[“"]?(.{2,20}?)[”"]?(?:是指|指的是|是(?:一种|一类|一个)|，是|\s+(?:refers to|is a|is an)\b)', head, re.I)
    if definition and is_noun_name(definition[1]):
        return definition[1], 'definition_subject'
    opening = re.split(r'[，,。；;：:]', head, maxsplit=1)[0].strip()
    if NOUN_END.fullmatch(opening) and is_noun_name(opening):
        return opening, 'noun_phrase'
    return None, 'no_reliable_literal_name'


def constraints(text):
    conditions = re.findall(r'(?:如果|若|当)[^。；;\n]{1,60}?(?:时|则|，|,)', text)
    exceptions = re.findall(r'(?:除[^。；;\n]{1,50}?外|但[^。；;\n]{1,60}?除外|例外[：:][^。；;\n]{1,60})', text)
    return list(dict.fromkeys(conditions)), list(dict.fromkeys(exceptions))


def extract_document(md_path):
    text = md_path.read_text(encoding='utf-8-sig')
    lines = text.splitlines()
    headings = [(i, get_heading(line)) for i, line in enumerate(lines, 1)]
    contents = [i for i, h in headings if h and label(h[1]) in {'目录', '目次', 'contents', 'tableofcontents'}]
    body_start = next((i for i, h in headings if h and CHAPTER_START.match(h[1]) and not TOC_ROW.search(h[1]) and (not contents or i > contents[0])), None)
    section_stack, item_stack, items, warnings = [], [], [], []
    skip_level = None
    current = None
    sequences = {}
    skip_labels = {label(x) for x in SKIP_LABELS}

    def flush():
        nonlocal current
        if current is not None:
            while current['end'] >= current['start'] and not lines[current['end'] - 1].strip():
                current['end'] -= 1
            current['source_quote'] = '\n'.join(lines[current['start'] - 1:current['end']])
            items.append(current)
            current = None

    for i, line in enumerate(lines, 1):
        if body_start and i < body_start:
            continue
        h = get_heading(line)
        # Numbered Markdown headings are eligible item titles, not chapter headings.
        item_text = h[1] if h else line
        match = NUMBERED_ITEM.match(item_text)
        is_item_heading = bool(h and match and not CHAPTER_START.match(h[1]))
        if h and not is_item_heading:
            flush()
            level, title = h
            if skip_level is not None and level > skip_level and not CHAPTER_START.match(title):
                continue
            skip_level = None
            item_stack = []
            sequences = {}
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            if label(title) in skip_labels:
                skip_level = level
                continue
            section_stack.append((level, title))
            continue
        if skip_level is not None:
            continue
        if TABLE.match(line) or IMAGE_ONLY.fullmatch(line.strip()) or TOC_ROW.search(line):
            flush()
            continue
        body = match['body'] if match else line.strip()
        candidate = bool(match or LEAD_IN.match(body) or RULE_LEAD.match(body))
        if candidate:
            flush()
            marker = match['marker'].strip() if match else None
            # Whitespace nesting can override marker style within the same list.
            indent = len(line) - len(line.lstrip())
            depth = marker_depth(marker) if marker else 1
            if item_stack and indent > item_stack[-1]['indent']:
                depth = max(depth, item_stack[-1]['depth'] + 1)
            while item_stack and item_stack[-1]['depth'] >= depth:
                item_stack.pop()
            if marker:
                # Discontinuities are audit warnings, never silently repaired numbers.
                parts = re.findall(r"\d+", marker)
                if parts:
                    sequence_key = (tuple(x['start'] for x in item_stack), depth, re.sub(r"\d+", "#", marker), tuple(parts[:-1]))
                    number = int(parts[-1])
                    previous = sequences.get(sequence_key)
                    if previous is not None and number not in (1, previous + 1):
                        warnings.append({'code': 'numbering_discontinuity', 'line': i, 'previous': previous, 'current': number})
                    sequences[sequence_key] = number
            current = {'start': i, 'end': i, 'body_first': body, 'marker': marker,
                       'depth': depth, 'indent': indent, 'item_type': item_kind(clean(body), bool(match)),
                       'context_path': [title for _, title in section_stack],
                       'parent_item_lines': [x['start'] for x in item_stack]}
            if marker:
                item_stack.append({'start': i, 'depth': depth, 'indent': indent})
        elif current is not None:
            current['end'] = i
    flush()
    points, review, dedup = [], [], {}
    for item in items:
        raw = item['source_quote']
        term, method = name_item(item['body_first'])
        conditions, exceptions = constraints(raw)
        occurrence = {'source_quote': raw, 'source_line_start': item['start'], 'source_line_end': item['end'],
                      'context_path': item['context_path'], 'marker': item['marker'], 'depth': item['depth'],
                      'parent_item_lines': item['parent_item_lines'], 'item_type': item['item_type'],
                      'conditions': conditions, 'exceptions': exceptions}
        if term is None or term not in raw:
            review.append({'candidate': clean(item['body_first']), 'reason': method, **occurrence})
            continue
        # Group identical names only; each occurrence retains its independent constraints.
        key = term.casefold()
        if key in dedup:
            dedup[key]['occurrences'].append(occurrence)
            continue
        point = {'knowledge_point': term, **occurrence, 'method': method,
                 'source_char_count': len(raw), 'occurrences': [occurrence]}
        points.append(point)
        dedup[key] = point
    if body_start is None:
        warnings.append({'code': 'body_start_uncertain', 'message': '未识别正文起点，仅按章节标签过滤。'})
    if not points:
        warnings.append({'code': 'no_reliable_names', 'message': '未识别可靠名称，候选保留在待复核列表。'})
    return {'schema_version': VERSION,
            'source': {'path': str(md_path.resolve()), 'sha256': sha256(md_path), 'size_bytes': md_path.stat().st_size, 'line_count': len(lines)},
            'rule': {'id': 'N3', 'name': '条列·步骤规则型', 'unit': 'literal_concept_name', 'method': 'rules_only', 'body_start_line': body_start},
            'summary': {'knowledge_point_count': len(points), 'candidate_count': len(items), 'review_count': len(review),
                        'step_count': sum(p['item_type'] == 'step' for p in points), 'rule_count': sum(p['item_type'] == 'rule' for p in points),
                        'enumeration_count': sum(p['item_type'] == 'enumeration' for p in points), 'warning_count': len(warnings)},
            'knowledge_points': points, 'review_candidates': review, 'warnings': warnings}


def export_csv(result, path, review=False):
    fields = (['candidate', 'reason'] if review else ['knowledge_point', 'method']) + ['item_type', 'marker', 'depth', 'context_path', 'source_line_start', 'source_line_end', 'source_quote', 'conditions', 'exceptions', 'occurrences']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for point in result['review_candidates' if review else 'knowledge_points']:
            row = {k: point.get(k, '') for k in fields}
            for key in ('context_path', 'conditions', 'exceptions', 'occurrences'):
                row[key] = json.dumps(row[key], ensure_ascii=False)
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description='N3 纯规则概念抽取，不调用模型')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='JSON 输出文件，同时导出知识点及待复核 CSV')
    args = parser.parse_args()
    result = extract_document(args.input)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    export_csv(result, args.out.with_suffix('.csv'))
    export_csv(result, args.out.with_name(args.out.stem + '_review.csv'), review=True)
    print(json.dumps(result['summary'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
