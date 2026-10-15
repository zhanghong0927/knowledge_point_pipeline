"""Fixed-population request replays; performance evidence is not semantic approval."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from batch_pipeline import save, now
from adapters.gateway_control import transition_phase


def fixed_population(packets, count):
    if not packets or type(count) is not int or count < 1:
        raise ValueError('Nonempty packets and a positive fixed population are required')
    return [packets[index % len(packets)] for index in range(count)]


@lru_cache(maxsize=16)
def source_text(path, sha256):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('Native validation source hash changed')
    return raw.decode('utf-8-sig')


def prepare_validators(packets):
    import sys
    root = Path(__file__).resolve().parent
    books = {}
    for packet in packets:
        if packet.get('validation_book'):
            sys.path.insert(0, str(root / 'modules/dictionary/src'))
            import fullbook_llm_v5
            book = packet['validation_book']
            books[(book['md_path'], book['md_sha256'])] = book
        if packet.get('native_front_track') == 'dictionary':
            sys.path.insert(0, str(root / 'modules/dictionary/classification/scripts'))
            import run_unseen20_classification
        elif packet.get('native_front_track') == 'important':
            sys.path.insert(0, str(root / 'modules/important_books/scripts'))
            import layout_model_validation
    for path, checksum in books:
        source_text(path, checksum)
    return {'preloaded_source_books': len(books), 'model_calls': 0}


def _validation_rates(counts):
    for field in ('api_protocol_valid', 'content_valid'):
        if field in counts:
            counts[field + '_rate'] = counts[field] / counts['requests'] if counts['requests'] else None
    return counts


def _sum_counts(rows):
    counts = {field: sum(row[field] for row in rows)
              for field in ('requests', 'protocol_valid', 'transport_errors')}
    # Older reports only recorded the combined native guard. Missing split
    # observations cannot be recovered from HTTP status or that legacy count.
    for field in ('api_protocol_valid', 'content_valid', 'api_protocol_errors',
                  'content_validation_errors'):
        if all(field in row for row in rows):
            counts[field] = sum(row[field] for row in rows)
    return _validation_rates(counts)


def combine_passes(rows):
    combined = []
    for limit in sorted({row['concurrency'] for row in rows}):
        passes = [row for row in rows if row['concurrency'] == limit]
        group_rows = {}
        for row in passes:
            for key, group in row.get('groups', {}).items():
                group_rows.setdefault(key, []).append(group)
        groups = {key: _sum_counts(parts) for key, parts in group_rows.items()}
        combined.append({**_sum_counts(passes), 'concurrency': limit,
            'wall_seconds': sum(row['wall_seconds'] for row in passes),
            'p95_seconds': max(row['p95_seconds'] for row in passes),
            'population_sha256': passes[0]['population_sha256'], 'groups': groups, 'passes': passes})
    return combined


def validate_response(raw, kind, packet=None):
    """Keep API completion health separate from generated content/native guards.

    ``protocol_valid`` retains its legacy strict content meaning for packet
    admission callers. Concurrency selection uses ``api_protocol_valid``.
    """
    try:
        if not isinstance(raw, dict) or raw.get('error') is not None:
            raise ValueError('API response object without an error is required')
        if not isinstance(raw.get('choices'), list) or not raw['choices']:
            raise ValueError('Nonempty API choices are required')
        choice = raw['choices'][0]
        if not isinstance(choice, dict):
            raise ValueError('API choice object required')
        if choice['finish_reason'] != 'stop':
            raise ValueError('truncated_or_incomplete_response')
        message = choice['message']
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise ValueError('API message with string content required')
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return {'api_protocol_valid': False, 'content_valid': False, 'protocol_valid': False,
                'api_protocol_error': str(exc), 'error': str(exc)}
    try:
        value = json.loads(message['content'])
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        if kind == 'important_cleaning' and packet:
            from important_calibration import validate_cleaning
            validate_cleaning(value, packet)
        if kind == 'extraction' and (value.get('scanned_all') is not True or
                                      not isinstance(value.get('entries'), list)):
            raise ValueError('Invalid native discovery schema')
        if kind == 'extraction' and packet and packet.get('validation_book'):
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent / 'modules/dictionary/src'))
            import fullbook_llm_v5 as native
            book = packet['validation_book']
            part = json.loads(packet['payload']['messages'][1]['content'])
            valid, rejected, excluded = native.validate_items(value['entries'], part,
                source_text(book['md_path'], book['md_sha256']), book)
            if rejected or (packet.get('require_nonempty_entries') and not valid):
                raise ValueError('Native literal-anchor/ownership validation failed or empty positive control')
        if kind == 'front' and packet and packet.get('native_front_track'):
            import sys
            root = Path(__file__).resolve().parent
            if packet['native_front_track'] == 'important':
                sys.path.insert(0, str(root / 'modules/important_books/scripts'))
                import layout_model_validation as native
                windows = json.loads(packet['payload']['messages'][1]['content'])['windows']
                native.validate_result(value, windows)
            else:
                sys.path.insert(0, str(root / 'modules/dictionary/classification/scripts'))
                import run_unseen20_classification as native
                windows = json.loads(packet['payload']['messages'][1]['content'])['windows']
                native.validate(value, windows)
        return {'api_protocol_valid': True, 'content_valid': True, 'protocol_valid': True, 'value': value}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        return {'api_protocol_valid': True, 'content_valid': False, 'protocol_valid': False,
                'content_validation_error': str(exc), 'error': str(exc)}


def choose_level(rows, max_transport_error_rate=.02, min_protocol_valid_rate=.90):
    """Choose by API goodput/latency; native evidence is reported, not gated.

    The threshold argument keeps its old name for callers. Reports without
    split metrics conservatively use the historical combined valid count.
    """
    def api_completions(row):
        return row['api_protocol_valid'] if 'api_protocol_valid' in row else row['protocol_valid']

    def acceptable(row):
        return (row['requests'] > 0 and row['transport_errors'] / row['requests'] <= max_transport_error_rate
                and api_completions(row) / row['requests'] >= min_protocol_valid_rate)
    healthy = [row for row in rows if row['requests'] > 0 and row['wall_seconds'] > 0 and
               acceptable(row) and all(acceptable(group) for group in row.get('groups', {}).values())
               and all(acceptable(part) and all(acceptable(group) for group in part.get('groups', {}).values())
                       for part in row.get('passes', []))]
    if not healthy:
        return None
    best = max(api_completions(row) / row['wall_seconds'] for row in healthy)
    near = [row for row in healthy if api_completions(row) / row['wall_seconds'] >= best * .95]
    return min(near, key=lambda row: (row['p95_seconds'], row['concurrency']))


def run_level(api_url, kind, population, concurrency, output, timeout, gateway=None):
    if type(concurrency) is not int or concurrency < 1 or timeout <= 0:
        raise ValueError('Invalid calibration limit or timeout')
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    started = time.monotonic()
    active = peak = observed_gateway_peak = 0
    lock = threading.Lock()
    stop = threading.Event()
    observations = []

    def sample():
        nonlocal observed_gateway_peak
        while not stop.is_set():
            try:
                value = gateway.stats()
                row = {'at': now(), 'phase': value['phase'], 'limit': value['limit'],
                       'active_chat': value['routes'].get('v1/chat/completions', {}).get('active', 0)}
                observations.append(row)
                observed_gateway_peak = max(observed_gateway_peak, row['active_chat'])
            except Exception as exc:
                observations.append({'at': now(), 'error': str(exc)})
            stop.wait(.2)

    def work(pair):
        nonlocal active, peak
        index, packet = pair
        encoded = json.dumps(packet['payload'], ensure_ascii=False, separators=(',', ':')).encode()
        record = {'index': index, 'packet_id': packet['id'], 'started_at': now(),
                  'group': packet.get('native_front_track', kind),
                  'payload_sha256': hashlib.sha256(encoded).hexdigest(),
                  'api_protocol_valid': False, 'content_valid': False, 'protocol_valid': False,
                  'http_status': None, 'transport_error': False}
        begun = time.monotonic()
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            request = Request(api_url + '/v1/chat/completions', data=encoded,
                              headers={'Content-Type': 'application/json'})
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                body = response.read(4194305)
                record['http_status'] = response.status
            if len(body) > 4194304:
                raise ValueError('Response exceeds calibration body bound')
            raw = json.loads(body)
            if not isinstance(raw, dict):
                raise ValueError('API response object required')
            save(output / 'responses' / f'{index:04}.json', raw)
            checked = validate_response(raw, kind, packet)
            record.update({key: value for key, value in checked.items() if key != 'value'})
            record['usage'] = raw.get('usage')
        except HTTPError as exc:
            record.update(http_status=exc.code, transport_error=True,
                          error=f'HTTP {exc.code}')
        except (OSError, TimeoutError) as exc:
            record.update(transport_error=True, error=f'{type(exc).__name__}: {exc}')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            record.update(api_protocol_error=f'{type(exc).__name__}: {exc}',
                          error=f'{type(exc).__name__}: {exc}')
        finally:
            record['seconds'] = time.monotonic() - begun
            with lock:
                active -= 1
            save(output / 'requests' / f'{index:04}.json', record)
        return record

    sampler = threading.Thread(target=sample, daemon=True) if gateway else None
    if sampler:
        sampler.start()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            records = list(pool.map(work, enumerate(population)))
    finally:
        stop.set()
        if sampler:
            sampler.join(6)
            if sampler.is_alive():
                raise RuntimeError('Calibration sampler did not close')
    durations = sorted(row['seconds'] for row in records)
    def counts(subset):
        return _validation_rates({'requests': len(subset),
            'protocol_valid': sum(row['protocol_valid'] for row in subset),
            'api_protocol_valid': sum(row['api_protocol_valid'] for row in subset),
            'content_valid': sum(row['content_valid'] for row in subset),
            'transport_errors': sum(row['transport_error'] for row in subset),
            'api_protocol_errors': sum('api_protocol_error' in row for row in subset),
            'content_validation_errors': sum('content_validation_error' in row for row in subset)})
    groups = {key: counts([row for row in records if row['group'] == key])
              for key in {row['group'] for row in records}}
    summary = {**counts(records), 'kind': kind, 'concurrency': concurrency,
               'http_statuses': {str(status): sum(row['http_status'] == status for row in records)
                                 for status in {row['http_status'] for row in records}},
               'wall_seconds': time.monotonic() - started,
               'p95_seconds': durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else 0,
               'client_peak_inflight': peak, 'sampled_gateway_peak_chat': observed_gateway_peak,
               'groups': groups,
               'scope': 'API completion health for concurrency; model JSON/schema/native evidence reported separately',
               'validation_rate_denominator': 'requests',
               'protocol_valid_scope': 'legacy combined JSON/schema/native evidence guard',
               'semantic_quality_approved': False,
               'population_sha256': hashlib.sha256(json.dumps(
                   [row['payload_sha256'] for row in records]).encode()).hexdigest()}
    save(output / 'GATEWAY_SAMPLES.json', observations)
    save(output / 'SUMMARY.json', summary)
    return summary


def calibrate_stage(gateway, kind, packets, levels, root, timeout, population_size,
                    fallback_levels=()):
    preparation_start = time.monotonic()
    prepared = prepare_validators(packets)
    save(Path(root) / f'{kind.upper()}_VALIDATOR_PREPARATION.json',
         dict(prepared, seconds=time.monotonic() - preparation_start))
    population = fixed_population(packets, population_size)
    rows = []
    def measure(limit, pass_number):
        transition_phase(gateway.api_url, limit, f'calibration_{kind}_{limit}_pass{pass_number}')
        row = run_level(gateway.api_url, kind, population, limit,
                        Path(root) / f'{kind}_{limit}_pass{pass_number}', timeout, gateway)
        row['pass_number'] = pass_number
        rows.append(row)
        save(Path(root) / f'{kind.upper()}_REPLAYS.json', rows)
    for limit in levels:
        measure(limit, 1)
    # Reverse sweep reduces the fixed ascending-order/cache advantage.
    for limit in reversed(levels):
        measure(limit, 2)
    combined = combine_passes(rows)
    if choose_level(combined) is None:
        for limit in fallback_levels:
            measure(limit, 1)
            measure(limit, 2)
            combined = combine_passes(rows)
            if choose_level(combined) is not None:
                break
    if len({row['population_sha256'] for row in rows}) != 1:
        raise ValueError('Calibration population changed across levels')
    save(Path(root) / f'{kind.upper()}_LEVELS.json', combined)
    selected = choose_level(combined)
    if selected is None:
        raise RuntimeError(f'No transport/API-healthy {kind} concurrency level; important continuation is held')
    return {'selected_limit': selected['concurrency'], 'levels': combined,
            'passes_per_level': 2, 'order': 'forward then reverse; all passes must be healthy',
            'selection': 'highest healthy API completion goodput; within5percent choose lower latency',
            'selection_metric': 'api_protocol_valid',
            'content_quality_gate_applied': False,
            'health_policy': {'max_transport_error_rate': .02, 'min_api_protocol_valid_rate': .90,
                              'checked_per_group_and_pass': True},
            'semantic_quality_approved': False}
