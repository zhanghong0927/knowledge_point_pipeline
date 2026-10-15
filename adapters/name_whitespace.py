"""Conservative, auditable name-only whitespace repair at the cleaning handoff."""
import hashlib
import json
from pathlib import Path
import re

VERSION='clean-export-name-whitespace-v1-20261015'
FIELDS=('name','knowledge_point')
RULES=Path(__file__).resolve().parents[1]/'modules/dedup/whitespace_rules.json'
HAN=r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af'
CJK_GAP=re.compile(r'(?<=['+HAN+r'])\s+(?=['+HAN+r'])')
SPACE=re.compile(r'\s+')
PROTECTED=re.compile(r'[$`=+*/^_<>∑∫≤≥≠≈±]|\\[([]|\\(?:frac|sqrt|begin)\b|\b(?:sin|cos|tan|log|ln|exp)\s*\(')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


RULES_SHA=sha(RULES)
IMPLEMENTATION_SHA=sha(__file__)
approved=set()
for rule in json.loads(RULES.read_text('utf-8'))['rules']:
    if rule['equivalent'] is not True:continue
    for variant in rule['variants']:
        # Only Han-to-Han gaps may be removed here. English spelling aliases and
        # Chinese/Latin, model-number and unit separators are never compacted.
        approved.add(CJK_GAP.sub('',SPACE.sub(' ',variant).strip()).casefold())
APPROVED=frozenset(approved)


def identity():
    if sha(RULES)!=RULES_SHA or sha(__file__)!=IMPLEMENTATION_SHA:
        raise ValueError('Name whitespace policy changed during export; use a new output directory')
    return {'version':VERSION,'rules_sha256':RULES_SHA,'implementation_sha256':IMPLEMENTATION_SHA}


def evidence_files():
    return [Path(__file__).resolve(),RULES]


def normalize_record(row,originals=None):
    result=dict(row)
    audit={'policy_version':VERSION,'fields':{},'warnings':{},'protected_fields':[]}
    references={str(originals.get(f)).strip() for f in FIELDS
                if originals and isinstance(originals.get(f),str) and originals.get(f).strip()}
    for field in FIELDS:
        if field not in row or row[field] is None:continue
        before=row[field]
        if not isinstance(before,str):raise ValueError('Name whitespace repair requires textual name fields')
        after=before.strip();rules=[]
        if after!=before:rules.append('edge_whitespace_trimmed')
        if PROTECTED.search(after):
            audit['protected_fields'].append(field)
        else:
            folded=SPACE.sub(' ',after)
            if folded!=after:rules.append('layout_whitespace_collapsed_to_single_separator')
            after=folded
            joined=CJK_GAP.sub('',after)
            if joined!=after:
                if joined in references:
                    after=joined;rules.append('cjk_spacing_supported_by_original_title')
                elif joined.casefold() in APPROVED:
                    after=joined;rules.append('cjk_spacing_supported_by_reviewed_spelling')
                else:
                    audit['warnings'][field]=['unconfirmed_cjk_spacing']
        if after!=before:
            result[field]=after
            audit['fields'][field]={'before':before,'after':after,'rules':rules}
    return result,audit


def new_report():
    return {'policy':identity(),'changed_records':0,'changed_fields':0,
            'flagged_records':0,'protected_records':0}


def observe(report,audit):
    report['changed_records']+=bool(audit['fields'])
    report['changed_fields']+=len(audit['fields'])
    report['flagged_records']+=bool(audit['warnings'])
    report['protected_records']+=bool(audit['protected_fields'])
