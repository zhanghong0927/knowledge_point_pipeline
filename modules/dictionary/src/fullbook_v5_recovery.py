"""Read a V5 recovery origin without modifying its files or completion markers."""
import hashlib
import json
from collections import Counter
from pathlib import Path
import re


def ensure_separate(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('Recovery output must be separate: source/output overlap')
    return source


def snapshot(source, output, book, units, units_sha256):
    source = ensure_separate(source, output)
    files = {}
    def load(path):
        content = path.read_bytes()
        files[str(path.relative_to(source))] = hashlib.sha256(content).hexdigest()
        return json.loads(content.decode('utf-8-sig'))
    old = load(source / 'INPUT.json')
    if old.get('version') != 'fullbook-structured-v5':
        raise ValueError('Unsupported recovery source version')
    old_book = old.get('book', {})
    if (old_book.get('identifier') != book['identifier']
            or old_book.get('md_sha256') != book['md_sha256']
            or Path(old_book.get('md_path', '')).resolve() != Path(book['md_path']).resolve()):
        raise ValueError('Recovery source MD identity differs')
    old_units = load(source / 'units.json')
    if old_units != units or old.get('units_sha256') != units_sha256:
        raise ValueError('Recovery source structure differs from the current MD')
    chunks, repairs = {}, {}
    for path in sorted((source / 'chunks').glob('*.json')):
        match = re.fullmatch(r'(\d{8})_(\d{8})', path.stem)
        if not match:
            raise ValueError('Invalid recovery source chunk name')
        lo, hi = map(int, match.groups())
        if not 0 <= lo < hi <= len(units):
            raise ValueError('Recovery source chunk is outside the MD')
        state = load(path)
        if state.get('status') == 'split':
            if type(state.get('mid')) is not int or not lo < state['mid'] < hi:
                raise ValueError('Invalid recovery source split')
        elif state.get('status') in ('completed', 'partial', 'oversized_structure'):
            if (not isinstance(state.get('accepted_raw'), list)
                    or not isinstance(state.get('unresolved'), list)
                    or (state['status'] == 'completed' and state.get('unresolved'))):
                raise ValueError('Invalid recovery source leaf schema')
        else:
            raise ValueError('Unsupported recovery source chunk status')
        chunks[path.stem] = state
    for path in sorted((source / 'responses').glob('*.json')):
        match = re.fullmatch(r'(\d{8}_\d{8})_(\d+)', path.stem)
        if not match:
            raise ValueError('Invalid recovery response filename')
        data = load(path)
        try:
            choice = data['choices'][0]
            payload = json.loads(choice['message']['content'])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if (choice.get('finish_reason') == 'stop' and isinstance(payload, dict)
                and isinstance(payload.get('repairs'), list)):
            rows=[row for row in payload['repairs'] if isinstance(row,dict)
                  and isinstance(row.get('repair_id'),str)]
            counts=Counter(row['repair_id'] for row in rows)
            repairs.setdefault(match[1], []).extend(row for row in rows if counts[row['repair_id']]==1)
    for path in sorted((source / 'errors').glob('*.json')):
        load(path)
    if (source / 'SUMMARY.json').is_file():
        load(source / 'SUMMARY.json')
    if any(hashlib.sha256((source / name).read_bytes()).hexdigest() != digest
           for name, digest in files.items()):
        raise ValueError('Recovery source changed while reading the snapshot')
    return {'origin': {'path': str(source), 'files_sha256': files},
            'chunks': chunks, 'repairs': repairs}
