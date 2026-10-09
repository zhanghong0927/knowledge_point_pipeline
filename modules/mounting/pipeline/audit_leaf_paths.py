"""Read-only audit of whether deduped paths are taxonomy leaves."""
import json
from pathlib import Path
from collections import Counter
from taxonomy_leaf_paths import leaf_paths, normalize_path

ROOT = Path('/home/wangqiyuan/work/full_leaf_dedup_20260923')
TAX = Path('/mnt/nas_si002991c1cm/deliver/taxonomy')

def main():
    reports = json.loads((ROOT/'overall_summary.json').read_text())
    for report in reports:
        if 'dedup_total' not in report:
            continue
        subject = report['subject']
        taxonomy = TAX / f'{"economics" if subject == "economy" else subject}_taxonomy.json'
        if not taxonomy.is_file():
            print(subject, 'taxonomy_missing')
            continue
        leaves = leaf_paths(json.loads(taxonomy.read_text()))
        counts = Counter()
        examples = {}
        audit = ROOT / subject / 'dedup_audit.jsonl'
        for line in audit.open():
            row = json.loads(line)
            if not row['reason'].startswith('same_leaf'):
                continue
            path = row['branch']
            kind = 'leaf' if normalize_path(path) in leaves else 'not_verified_leaf'
            counts[kind] += 1
            examples.setdefault(kind, path)
        print(subject, dict(counts), examples)

if __name__ == '__main__':
    main()
