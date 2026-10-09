"""Mechanical errors versus semantic suspicions are reported separately."""
import re
import unicodedata
def check_name(value,field):
    if not isinstance(value,str) or not value: return []
    out=[]
    if value!=value.strip(): out.append(('error','edge_whitespace'))
    if re.search(r'\s{2,}',value): out.append(('error','repeated_whitespace'))
    if any(unicodedata.category(c) in ('Cc','Cf') for c in value): out.append(('error','control_character'))
    pairs={'(':')','（':'）','[':']','【':'】','{':'}','《':'》'}
    stack=[]; bad=False
    for c in value:
        if c in pairs: stack.append(pairs[c])
        elif c in pairs.values():
            if not stack or stack.pop()!=c: bad=True
    if stack or bad: out.append(('error','unbalanced_brackets'))
    if '\ufffd' in value: out.append(('error','replacement_character'))
    if re.match(r'^\s*(?:\d+[.、．]\s+|[（(]\d+[)）])',value): out.append(('suspect','number_prefix'))
    if re.search(r'[,，;；:：。!?！？]{2,}|[,，;；:：。!?！？]$',value): out.append(('suspect','punctuation'))
    han=bool(re.search(r'[\u3400-\u9fff]',value)); latin=bool(re.search('[A-Za-z]',value))
    if field=='knowledge_point' and han: out.append(('suspect','english_field_contains_han'))
    if field=='name' and latin: out.append(('suspect','chinese_field_contains_latin'))
    return out
