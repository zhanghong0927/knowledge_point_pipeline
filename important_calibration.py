"""Capture and validate important-book native front and cleaning workloads."""
import argparse
from collections import defaultdict
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

from batch_pipeline import save
from calibration_packets import CapturedPayload, important_front_packets

ROOT = Path(__file__).resolve().parent


def balanced_rows(rows, maximum=18):
    groups = defaultdict(list)
    for row in rows:
        groups[row['subject_slug']].append(row)
    for subject in groups:
        groups[subject].sort(key=lambda row: (row.get('priority_rank', 1000000), row['identifier']))
    selected = []
    for number in range(max((len(group) for group in groups.values()), default=0)):
        for group in groups.values():
            if number < len(group):
                selected.append(group[number])
                if len(selected) == maximum:
                    return selected
    return selected


def validate_workload_coverage(packets, source_rows):
    available_subjects = {row['subject_slug'] for row in source_rows}
    required_subjects = min(2, len(available_subjects))
    if (len(packets) < 4 or len({packet['source_identifier'] for packet in packets}) < 2
            or len({packet['subject_slug'] for packet in packets}) < required_subjects
            or not available_subjects):
        raise ValueError('Insufficient representative native important-book workload')


@lru_cache(maxsize=1)
def cleaning_native():
    path = ROOT / 'modules/cleaning/scripts/model_clean.py'
    spec = importlib.util.spec_from_file_location('important_calibration_native_clean', path)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    return native


def capture_cleaning_command(command, maximum_batches=3):
    """Run native body/schema construction; intercept the final HTTP boundary."""
    native = cleaning_native()
    with patch.object(sys, 'argv', [str(command[1]), *map(str, command[2:])]):
        args = native.parse_args()
    native.validate_args(args)
    index = native.build_taxonomy_index(args.taxonomy, args.taxonomy_depth)
    summary, _ = native.taxonomy_summary(index, args.taxonomy_chars, args.taxonomy_depth,
                                        args.taxonomy_scope_chars)
    prompt = native.system_prompt(args.subject, args.subject_description, summary)
    packets = []
    for number, batch in enumerate(native.iter_batches(args.input, set(), args.batch_size,
            args.context_chars, 0, maximum_batches * args.batch_size, index,
            args.taxonomy_candidate_nodes, args.taxonomy_scope_chars), 1):
        captured = []
        def capture(request, **_):
            captured.append(json.loads(request.data))
            raise CapturedPayload()
        try:
            with patch.object(native.urllib.request, 'urlopen', side_effect=capture):
                native.request_model(f'batch-{number:08d}', batch, prompt, args)
        except CapturedPayload:
            pass
        if len(captured) != 1:
            raise ValueError('Native cleaning request was not captured exactly once')
        packets.append({'id': f'important_cleaning_{number}', 'kind': 'important_cleaning',
                        'payload': captured[0], 'validation_batch': batch,
                        'validation_args': {'model': args.model,
                            'confidence_threshold': args.confidence_threshold,
                            'fix_swapped_languages': args.fix_swapped_languages}})
    return packets


def validate_cleaning(value, packet):
    native = cleaning_native()
    expected = json.loads(packet['payload']['messages'][1]['content'])
    batch = packet['validation_batch']
    items = value.get('items')
    if (value.get('batch_id') != expected['batch_id'] or not isinstance(items, list)
            or len(items) != len(batch) or {item.get('key') for item in items if isinstance(item, dict)}
            != {row['key'] for row in batch}):
        raise ValueError('Native cleaning batch identity or item key set differs')
    args = argparse.Namespace(**packet['validation_args'])
    by_key = {row['key']: row['source_row'] for row in batch}
    fields = ('name', 'knowledge_point', 'definition', 'en_definition', 'description',
              'en_description', 'reason', 'evidence')
    for item in items:
        if (not isinstance(item, dict) or item.get('decision') not in ('keep', 'drop', 'review')
                or type(item.get('confidence')) not in (int, float) or not 0 <= item['confidence'] <= 1
                or type(item.get('subject_relevant')) is not bool
                or any(not isinstance(item.get(field), str) for field in fields)
                or not isinstance(item.get('quality_flags'), list)
                or not all(isinstance(flag, str) for flag in item['quality_flags'])
                or item.get('pair_consistency') not in ('consistent', 'inconsistent', 'uncertain', 'not_applicable')
                or not isinstance(item.get('field_results'), dict)):
            raise ValueError('Invalid native cleaning contract')
        for field in ('name', 'knowledge_point'):
            verdict = item['field_results'].get(field)
            if (not isinstance(verdict, dict) or verdict.get('decision') not in ('keep', 'drop', 'empty')
                    or not isinstance(verdict.get('reason'), str)):
                raise ValueError('Missing native cleaning field contract')
        checked = native.normalize_result(by_key[item['key']], item, args)
        local_failures = {'invalid_field_result', 'invalid_pair_consistency',
                          'language_assignment_swapped_by_model',
                          'standardized_name_not_grounded_in_source_titles',
                          'standardized_content_not_sufficiently_grounded',
                          'incomplete_review_contract', 'invalid_name_after_model',
                          'invalid_knowledge_point_after_model'}
        if set(checked['quality_flags']) & local_failures:
            raise ValueError('Native cleaning local evidence validation failed')


def important_front_workload(home, calibration, service):
    rows = balanced_rows(json.loads((home / 'inputs/important_candidates_verified.json').read_text()), 18)
    pilot = calibration / 'pilot'
    save(pilot / 'inputs/important_candidates_verified.json', rows)
    packets = important_front_packets(pilot, pilot, service, maximum=len(rows))
    mapping = {row['identifier']: row for row in rows}
    for packet in packets:
        packet['native_front_track'] = 'important'
        packet['subject_slug'] = mapping[packet['source_identifier']]['subject_slug']
    save(pilot / 'PURPOSE.json', {'scope': 'isolated native calibration workload only',
         'production_admission_granted': False, 'source_outputs_reused': False,
         'selected_candidates': len(rows), 'packet_count': len(packets)})
    return rows, packets


def important_cleaning_workload(home, calibration, service, gateway, front_limit, rows):
    """Real pilot classification -> N1/N3 rules -> normalization/rules -> captured cleaning."""
    from benchmark_support import configure
    from stage_scheduling import run_simple_phase
    from staged_benchmark import selected_job, native_task
    sys.path.insert(0, str(ROOT / 'modules/important_books/scripts'))
    import layout_model_validation as native
    pilot = calibration / 'pilot'
    def classify(row):
        job_home = pilot / 'books' / row['subject_slug'] / row['identifier']
        book = {key: row[key] for key in ('identifier', 'title', 'md_path', 'pdf_path')}
        job = configure(home, service, 'important', row['subject_slug'], [row], job_home,
            'calibration_' + row['identifier'], gateway.job_url('calibration_' + row['identifier']),
            workers=1, selected=[book], global_limit=128, defer_transport_retry=True)
        prepared, classified = job_home / 'run/02_structure/prepared', job_home / 'run/02_structure/classification'
        native.prepare(job_home / 'inputs/selected_books.json', None, None, prepared)
        native.run(prepared, classified, job_home / 'inputs/model.json', workers=1)
        decision = json.loads((classified / 'results' / (row['identifier'] + '.json')).read_text())
        return row, job, decision
    outcomes = run_simple_phase(rows, classify, min(len(rows), front_limit))
    packets, skipped = [], []
    for row, job, decision in outcomes:
        if decision.get('status') != 'classified' or decision.get('primary_class') not in ('N1', 'N3'):
            skipped.append({'identifier': row['identifier'], 'status': decision.get('status'),
                            'primary_class': decision.get('primary_class')})
            continue
        extraction = selected_job(job, '03', 'extract_N1_N3')
        clean = selected_job(job, '04', 'normalize_fields', 'rules') if extraction['status'] == 'completed' else {}
        if clean.get('status') != 'completed':
            skipped.append({'identifier': row['identifier'], 'status': 'native_cpu_preparation_failed'})
            continue
        for packet in capture_cleaning_command(native_task(job, '04', 'model_clean')['command'], 2):
            packet.update(id='clean_' + row['identifier'] + '_' + str(len(packets)),
                          source_identifier=row['identifier'], subject_slug=row['subject_slug'])
            packets.append(packet)
    packets = balanced_rows([dict(packet, identifier=packet['id']) for packet in packets], 16)
    save(pilot / 'CLEANING_PACKET_PREPARATION.json', {'captured': len(packets), 'skipped': skipped,
          'books': sorted({packet['source_identifier'] for packet in packets}),
          'subjects': sorted({packet['subject_slug'] for packet in packets}),
          'source': 'native classified N1/N3 with native rule extraction and cleaning preprocessing',
          'production_admission_granted': False, 'captured_requests_sent': 0})
    validate_workload_coverage(packets, rows)
    return packets
