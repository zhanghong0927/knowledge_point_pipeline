"""Prepare a separate V5 recovery run; add --run to execute global 8/2/1 rounds."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import fullbook_llm_v5 as native
import fullbook_v5_recovery as recovery
import run_fullbook_v5 as driver


def source_spec(source, explicit_manifest=None):
    identity = json.loads((source / 'INPUT.json').read_text(encoding='utf-8-sig'))
    book = identity['book']
    paths = [Path(explicit_manifest)] if explicit_manifest else []
    rounds_path = source.parent / 'ROUNDS.json'
    if not paths and rounds_path.is_file():
        rounds = json.loads(rounds_path.read_text(encoding='utf-8-sig'))
        for row in rounds.get('rounds', []):
            command = row.get('command', [])
            if '--manifest' in command and command.index('--manifest') + 1 < len(command):
                path = Path(command[command.index('--manifest') + 1])
                if path.is_file():
                    paths.append(path)
                    break
    spec = {key: book[key] for key in ('identifier', 'title', 'md_path')}
    if paths:
        records = json.loads(paths[0].read_text(encoding='utf-8-sig'))
        if not isinstance(records, list):
            raise ValueError('The original native manifest must be a book list')
        matches = [row for row in records if isinstance(row, dict)
                   and row.get('identifier') == book['identifier']]
        if len(matches) != 1:
            raise ValueError('Original manifest does not uniquely identify the recovery book')
        spec = dict(matches[0])
    if Path(spec['md_path']).resolve() != Path(book['md_path']).resolve():
        raise ValueError('Original manifest MD path differs from recovery source')
    return {**spec, 'recovery_source': str(source)}


def prepare(args):
    sources = [recovery.ensure_separate(path, args.out) for path in args.source_run]
    if len(set(sources)) != len(sources):
        raise ValueError('Duplicate recovery source directories')
    specs, origins = [], []
    for source in sources:
        spec = source_spec(source, args.original_manifest)
        content = Path(spec['md_path']).read_bytes()
        text = content.decode('utf-8-sig')
        book = {key: spec[key] for key in ('identifier', 'title', 'md_path')}
        book['md_sha256'] = hashlib.sha256(content).hexdigest()
        units = native.structure.annotate(native.structural_units(text))
        native.attach_format(units, spec.get('pdf_evidence_path'), book['md_sha256'], text)
        digest = native.v2.legacy.digest(native.v2.legacy.encode(units).encode())
        origin = recovery.snapshot(source, args.out, book, units, digest)
        spec['recovery_source_sha256']=native.v2.legacy.digest(native.v2.legacy.encode(origin['origin']).encode())
        for key, state in origin['chunks'].items():
            if state['status'] == 'split':
                continue
            lo, hi = map(int, key.split('_'))
            packet = native.structure.packet(units, lo, hi, args.overlap)
            _, bad, omitted = native.validate_items(state.get('accepted_raw', []), packet, text, book)
            if bad or omitted:
                raise ValueError('Saved recovery entries no longer match source')
        specs.append(spec)
        origins.append(origin['origin'])
    if len({spec['identifier'] for spec in specs}) != len(specs):
        raise ValueError('Duplicate recovery book identifiers')
    manifest = args.out / 'MANIFEST.json'
    native_args = ['--manifest', str(manifest), '--out', str(args.out / 'native'),
                   '--api-url', args.api_url, '--model', args.model,
                   '--context', str(args.context), '--output-tokens', str(args.output_tokens),
                   '--timeout', str(args.timeout), '--book-workers', str(args.book_workers),
                   '--round-workers', *map(str, args.round_workers), '--overlap', str(args.overlap),
                   '--repair-batch-size', str(args.repair_batch_size),
                   '--repair-attempts', str(args.repair_attempts),
                   '--repair-max-requests', str(args.repair_max_requests),
                   '--tokenize-attempts', str(args.tokenize_attempts)]
    for url in args.metrics_url or []:
        native_args += ['--metrics-url', url]
    driver.parse_args(native_args)
    code = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob('*.py'))}
    payload = {'version': 1, 'specs': specs, 'origins': origins,
               'driver_arguments': native_args, 'code_sha256': code}
    plan = args.out / 'RECOVERY_PLAN.json'
    if args.out.exists() and any(args.out.iterdir()):
        if (not plan.is_file() or not manifest.is_file()
                or json.loads(plan.read_text(encoding='utf-8-sig')) != payload
                or json.loads(manifest.read_text(encoding='utf-8-sig')) != specs):
            raise ValueError('Recovery profile/output differs; choose a new output directory')
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        driver.atomic_write(manifest, specs)
        driver.atomic_write(plan, payload)
    return native_args


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, action='append', required=True,
                        help='Repeat for native book folders containing INPUT.json and units.json')
    parser.add_argument('--original-manifest', type=Path,
                        help='Optional original native manifest, preserving PDF/layout input metadata')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--context', type=int, default=32768)
    parser.add_argument('--output-tokens', type=int, default=8192)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--book-workers', type=int, default=4)
    parser.add_argument('--round-workers', type=int, nargs=3, default=(8, 2, 1))
    parser.add_argument('--overlap', type=int, default=4)
    parser.add_argument('--repair-batch-size', type=int, default=8)
    parser.add_argument('--repair-attempts', type=int, default=2)
    parser.add_argument('--repair-max-requests', type=int, default=128)
    parser.add_argument('--tokenize-attempts', type=int, default=3)
    parser.add_argument('--metrics-url', action='append')
    parser.add_argument('--run', action='store_true', help='Execute the prepared separate recovery')
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    try:
        native_args = prepare(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'refused', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.run:
        return driver.main(native_args)
    print(json.dumps({'status': 'prepared', 'model_calls': 0, 'books': len(args.source_run),
                      'manifest': str(args.out / 'MANIFEST.json'),
                      'driver_arguments': native_args}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
