"""Evidence-bounded content guards for the v4.1 cleaning pipeline."""
import argparse
import copy
import json
import re
from pathlib import Path

import clean_boundary_v41 as v41
import clean_boundary_v4 as v4
import clean_compare1000 as c


DATE_RANGE = re.compile(
    r'^(?:\d{1,2}\s+)?[A-Z][a-z]+\s+\d{4}\s*[–-]\s*'
    r'(?:\d{1,2}\s+)?[A-Z][a-z]+\s+\d{4}$'
)
IMAGE = re.compile(r'^!\[[^]]*\]\([^)]*\)$')
VISUAL_PARAGRAPH = re.compile(
    r'^(?:The|This|An?)\s+(?:illustration|figure|diagram|image|drawing)\s+'
    r'(?:shows|depicts|illustrates|presents)\b', re.I
)
DEPENDENT_DEFINITION = re.compile(
    r'^(?:This|That|These|Those|The (?:first|second|third|former|latter))\s+'
    r'(?:act|law|use|approach|method|process|system|concept|theory|result|case|one)\b', re.I
)
BYLINE = re.compile(r'^[A-Z][a-z]+(?:\s+(?:[A-Z]\.?)?\s*[A-Z][a-z]+){1,3}$')


def layout_evidence(row, raw):
    """Find non-prose lines using local MD evidence, not inferred semantics."""
    layout = set(v4.layout_evidence(row))
    edges = row['source_context'].get('body_edges', [])
    by_line = {item['line']: item['text'].strip() for item in edges}
    for location in row['source'].get('body_locations', []):
        line = location.get('start_line')
        text = by_line.get(line, '')
        if not text or text not in raw:
            continue
        previous = [by_line.get(line - offset, '') for offset in range(1, 5)]
        following = [by_line.get(line + offset, '') for offset in range(1, 5)]
        if IMAGE.fullmatch(next((part for part in previous if part), '')):
            layout.add(text)
        if BYLINE.fullmatch(text) and any(part.lower().startswith('see also') for part in following):
            layout.add(text)
    for match in re.finditer(r'(?m)^[^\n]+$', raw):
        text = match.group().strip()
        before, after = raw[:match.start()], raw[match.end():]
        previous = next((line.strip() for line in reversed(before.splitlines()) if line.strip()), '')
        if DATE_RANGE.fullmatch(text) and (previous in layout or previous.startswith('#')):
            layout.add(text)
        if (previous.startswith('#') and re.fullmatch(r'[a-z][a-z\s-]{2,55}', text)
                and after.startswith('\n\n') and re.match(r'\n\n[A-Z]', after)):
            layout.add(text)
        if IMAGE.fullmatch(text):
            layout.add(text)
    for paragraph in re.split(r'\n\s*\n', raw):
        text = paragraph.strip()
        if VISUAL_PARAGRAPH.match(text):
            for line in text.splitlines():
                if line.strip():
                    layout.add(line.strip())
    return layout


class Runner(v41.Runner):
    def select(self, row, stage, mapped, prompt):
        if not mapped.text.strip():
            return {}
        layouts = layout_evidence(row, mapped.text)
        instruction = (
            '选属于当前词条的解释性正文；layout=true的图注、署名、日期和图示依赖段落不能选择。'
            if stage == 'explanation' else
            '选直接界定词条的独立完整句；若定义首句的指代对象只在未选中的前文，空ranges。'
        )
        packet = {'subject': row['head'], 'units': v41.units(mapped.text, layouts)}
        if stage == 'explanation':
            packet['source_context'] = row['source_context']
        response = self.call(
            row['id'], stage, packet, v41.POLICY + instruction + c.SELECT_SCHEMA,
            lambda value: v41.project(mapped, value, layouts),
        )
        if response['status'] == 'failed':
            raise RuntimeError(stage + ' failed after two attempts')
        result, edits = v41.project(mapped, response['vote'], layouts)
        c.write(self.out/'sentence_guards'/stage/(row['id']+'.json'), {'edits': edits})
        return result

    def call(self, sid, stage, packet, prompt, validator):
        response = super().call(sid, stage, packet, prompt, validator)
        if stage != 'alignment' or response['status'] == 'failed':
            return response
        vote = copy.deepcopy(response['vote'])
        edits = []
        for field in ('definition', 'en_definition'):
            text = packet['text_fields'].get(field, '').strip()
            if text and DEPENDENT_DEFINITION.match(text) and vote['fields'][field] == 'keep':
                vote['fields'][field] = 'drop'
                edits.append({'field': field, 'reason': 'definition_begins_with_unresolved_reference'})
        c.write(self.out/'definition_guards_v42'/(sid+'.json'), {'edits': edits})
        return {**response, 'vote': vote}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bases', nargs=2, type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1024)
    args = parser.parse_args()
    raise SystemExit('Use run_boundary_v42.py for frozen cohort execution')
