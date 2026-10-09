"""Conservative structural rejection and alignment-protocol repair for frozen inputs."""
import copy
import re

import clean_boundary_v42 as v42
import clean_compare1000 as c


ALIGNMENT_EXTENSION = '''
额外检查定义是否真正界定名称，而不只是相关背景。分布地点、历史事件、评价句不能仅因相关而作定义。
返回额外definition_checks对象：对每个保留的非空definition/en_definition字段给出
{"role":"definition/background/dependent/none","needs_prior":false,"evidence":"该字段中的逐字片段"}。
不能独立成立、依赖缺失前文的定义，标dependent和needs_prior=true。没有定义允许保留名称与可靠解释。
保留解释不要求它全文完整，但所选句必须完整、无串条、无署名混入、指代和语义未因剪裁改变。
整条drop或字段drop时无需为已删除的定义给出definition_checks；保留的定义证据必须逐字来自该字段。
'''


def alignment_vote(vote, packet):
    result, edits = copy.deepcopy(vote), []
    if result['record_decision'] != 'keep':
        return result, edits
    checks = result.get('definition_checks')
    for field in ('definition', 'en_definition'):
        text = packet['text_fields'].get(field, '').strip()
        if not text or result['fields'][field] != 'keep':
            continue
        check = checks.get(field, {}) if isinstance(checks, dict) else {}
        valid = (isinstance(check, dict) and check.get('role') == 'definition'
                 and check.get('needs_prior') is False
                 and isinstance(check.get('evidence'), str)
                 and bool(check['evidence'].strip()) and check['evidence'] in text)
        if not valid or v42.DEPENDENT_DEFINITION.match(text):
            result['fields'][field] = 'drop'
            edits.append({'field': field, 'reason': 'unverified_or_dependent_definition'})
    return result, edits


def structural_reject_reason(row):
    head = row['head'].strip()
    source = row['source']
    context = row['source_context']['head_context']
    heading = next((part['text'].strip() for part in context
                    if part['line'] == source['head_line']), '')
    if not heading or not head:
        return None
    if re.fullmatch(r'[IVXLCDM]{2,9}', head) and re.match(
            rf'^{re.escape(head)}[.)]\s+(?:Also|Whereas|It is)\b', heading, re.I):
        return 'numbered_source_passage'
    see_line = re.match(rf'^{re.escape(head)}[.]\s+(See\s+\S.+)$', heading, re.I)
    body_lines = [line.strip() for line in row['raw_content'].splitlines() if line.strip()]
    if (see_line and body_lines and body_lines[0] == see_line.group(1)
            and len(body_lines) > 1):
        return 'index_see_only'
    if ('\ufffd' in head and head.count('\ufffd') >= max(2, len(head) // 4)):
        return 'unreadable_head'
    return None


def structural_review_reason(row):
    head = row['head'].strip()
    source = row['source']
    context = row['source_context']['head_context']
    heading = next((part['text'].strip() for part in context
                    if part['line'] == source['head_line']), '')
    preceding = [part['text'].strip() for part in context
                 if part['line'] < source['head_line'] and part['text'].strip()]
    previous = preceding[-1] if preceding else ''
    body = row['raw_content'].strip()
    if (head and re.match(rf'^#{{2,6}}\s+{re.escape(head)}\s*$', heading, re.I)
            and len(previous) >= 120 and not previous.startswith('#')
            and body and not re.search(rf'\b{re.escape(head)}\b', body[:300], re.I)):
        return 'possible_dependent_subheading'
    return None


class Runner(v42.Runner):
    def call(self, sid, stage, packet, prompt, validator):
        if stage != 'alignment':
            return super().call(sid, stage, packet, prompt, validator)
        response = c.Runner.call(self, sid, stage, packet,
                                 prompt + ALIGNMENT_EXTENSION, validator)
        if response['status'] == 'failed':
            return response
        vote, edits = alignment_vote(response['vote'], packet)
        c.write(self.out/'definition_guards_v45'/(sid+'.json'), {
            'original_vote': response['vote'], 'effective_vote': vote, 'edits': edits,
        })
        return {**response, 'vote': vote}

    def content(self, row):
        reason = structural_reject_reason(row)
        if reason:
            return {'id': row['id'], 'status': 'drop', 'structural_reject': reason}
        return super().content(row)
