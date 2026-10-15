"""Build representative native payloads in a separate performance workspace."""
import hashlib
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
from urllib.request import Request, ProxyHandler, build_opener

from batch_pipeline import save

ROOT = Path(__file__).resolve().parent


def extraction_packet(identifier, units, lo, hi, model, output_tokens=8192):
    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= len(units):
        raise ValueError('Invalid native ownership range')
    sys.path.insert(0, str(ROOT / 'modules/dictionary/src'))
    import fullbook_llm_v5 as native
    part = native.structure.packet(units, lo, hi, 4)
    payload = {'model': model, 'temperature': 0, 'max_tokens': output_tokens,
               'chat_template_kwargs': {'enable_thinking': False},
               'response_format': {'type': 'json_object'}, 'messages': [
                   {'role': 'system', 'content': native.PROMPT},
                   {'role': 'user', 'content': native.v2.legacy.encode(part)}]}
    return {'id': identifier, 'kind': 'extraction', 'payload': payload,
            'native_ownership': {'lo': lo, 'hi': hi}, 'output_tokens': output_tokens}


class CapturedPayload(BaseException):
    pass


def important_front_packets(source, output, service, maximum=8):
    """Capture exact layout-classifier calls; native preparation is CPU-only."""
    sys.path.insert(0, str(ROOT / 'modules/important_books/scripts'))
    import layout_model_validation as native
    rows = json.loads((Path(source) / 'inputs/important_candidates_verified.json').read_text())
    packets, skipped = [], []
    for row in rows:
        home = Path(output) / 'important_shadow' / row['identifier']
        book = {key: row[key] for key in ('identifier', 'title', 'md_path', 'pdf_path') if key in row}
        save(home / 'books.json', [book])
        prepared, classified = home / 'prepared', home / 'classification'
        try:
            summary = native.prepare(home / 'books.json', None, None, prepared)
            if summary['failures']:
                raise ValueError('Native source preparation failed')
            captured = []

            def capture(_url, _config, payload=None):
                if payload is None:
                    raise AssertionError('Unexpected native HTTP request during packet capture')
                captured.append(payload)
                raise CapturedPayload()

            config = {'api_url': 'http://127.0.0.1:9', 'model': service['model'],
                      'context_limit': service['configured_context'], 'max_tokens': 8192,
                      'timeout': 600, 'defer_transport_retry': True,
                      'chat_template_kwargs': {'enable_thinking': False}}
            try:
                with patch.object(native, 'http_json', side_effect=capture):
                    native.process(row['identifier'], prepared, classified, config,
                                   native.read_json(prepared / 'manifest.json'))
            except CapturedPayload:
                pass
            if len(captured) != 1:
                raise ValueError('Native request budget prevented packet capture')
            packets.append({'id': 'important_' + str(len(packets)), 'kind': 'front',
                            'payload': captured[0], 'source_identifier': row['identifier'],
                            'native_front_track': 'important',
                            'source_md_sha256': native.sha256(Path(row['md_path']))})
            if len(packets) >= maximum:
                break
        except (ValueError, OSError, KeyError, TypeError) as exc:
            skipped.append({'identifier': row.get('identifier'), 'error': str(exc)})
    save(Path(output) / 'IMPORTANT_PACKET_PREPARATION.json',
         {'captured': len(packets), 'skipped': skipped, 'real_model_calls': 0})
    if len(packets) < 4:
        raise ValueError('Insufficient real important-book front packets')
    return packets


def dictionary_front_packets(source, model, maximum=8):
    from concurrency_calibration import validate_response
    values = []
    for path in sorted((Path(source) / 'dictionary/front').glob('*/*/run/02_structure/classification/sent/*_0.json')):
        row = json.loads(path.read_text())
        if row.get('payload', {}).get('model') != model:
            continue
        packet = {'id': 'dictionary_' + str(len(values)), 'kind': 'front',
                       'native_front_track': 'dictionary',
                       'payload': row['payload'], 'source': str(path),
                       'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        original_response = path.parent.parent / 'raw' / path.name
        if (not original_response.is_file() or
                not validate_response(json.loads(original_response.read_text()), 'front', packet)['protocol_valid']):
            continue
        packet['original_validated_response_sha256'] = hashlib.sha256(original_response.read_bytes()).hexdigest()
        values.append(packet)
        if len(values) >= maximum:
            break
    return values


def dictionary_extraction_packets(source, model, maximum=12):
    packets = []
    candidates = []
    for raw in sorted((Path(source) / 'dictionary/production').glob('*/*/run/03_extraction/raw')):
        for folder in sorted(raw.iterdir()):
            if folder.is_dir() and (folder / 'INPUT.json').is_file() and (folder / 'units.json').is_file():
                choices = []
                for chunk in sorted((folder / 'chunks').glob('*.json')):
                    match = re.fullmatch(r'(\d+)_(\d+)\.json', chunk.name)
                    if not match:
                        continue
                    row = json.loads(chunk.read_text())
                    if row.get('status') == 'completed' and row.get('accepted_raw') and not row.get('rule_only'):
                        choices.append((chunk, int(match[1]), int(match[2])))
                    if len(choices) == 2:
                        break
                if choices:
                    candidates.append((folder, choices))
    for folder, choices in candidates:
        units, identity = checked_extraction_source(folder, model)
        md = Path(identity['book']['md_path'])
        if hashlib.sha256(md.read_bytes()).hexdigest() != identity['book']['md_sha256']:
            raise ValueError('Dictionary MD changed since extraction')
        for chunk, lo, hi in choices:
            packet = extraction_packet('extraction_' + str(len(packets)), units, lo, hi, model)
            packet.update(source=str(chunk), source_sha256=hashlib.sha256(chunk.read_bytes()).hexdigest(),
                          source_identifier=identity['book']['identifier'],
                          validation_book=identity['book'], require_nonempty_entries=True)
            packets.append(packet)
            if len(packets) >= maximum:
                return packets
    if len(packets) < 4:
        raise ValueError('Insufficient resolved native dictionary chunk packets')
    return packets


def checked_extraction_source(folder, model):
    sys.path.insert(0, str(ROOT / 'modules/dictionary/src'))
    import fullbook_llm_v5 as native
    folder = Path(folder)
    units = json.loads((folder / 'units.json').read_text())
    identity = json.loads((folder / 'INPUT.json').read_text())
    if (identity.get('model') != model or identity.get('overlap') != 4 or
            identity.get('output_tokens') != 8192 or identity.get('context') != 32768 or
            native.v2.legacy.digest(native.v2.legacy.encode(units).encode()) != identity.get('units_sha256')):
        raise ValueError('Native cached units/model/overlap/output/context identity mismatch')
    for name, expected in identity['code'].items():
        current = ROOT / 'modules/dictionary/src' / name
        if not current.is_file() or hashlib.sha256(current.read_bytes()).hexdigest() != expected:
            raise ValueError('Native extraction code differs from cached source provenance')
    return units, identity


def post_json(url, payload, timeout=120):
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(url, data=body, headers={'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        result = response.read(1048577)
    if len(result) > 1048576:
        raise ValueError('Tokenization response exceeds body bound')
    value = json.loads(result)
    if not isinstance(value, dict):
        raise ValueError('Tokenization object required')
    return value


def context_checked_packets(api_url, packets, service, report):
    accepted, rows = [], []
    for packet in packets:
        payload = packet['payload']
        if (payload['model'] != service['model'] or type(payload.get('max_tokens')) is not int
                or not 1 <= payload['max_tokens'] <= 8192):
            raise ValueError('Calibration model/output budget mismatch')
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > 16777216:
            raise ValueError('Calibration request exceeds gateway body bound')
        count = post_json(api_url + '/tokenize', {'model': service['model'],
            'messages': payload['messages'], 'add_generation_prompt': True,
            'chat_template_kwargs': payload.get('chat_template_kwargs', {'enable_thinking': False})})['count']
        if type(count) is not int or count < 0:
            raise ValueError('Invalid actual service token count')
        fits = count + payload['max_tokens'] + 2048 <= service['configured_context']
        rows.append({'id': packet['id'], 'prompt_tokens': count,
                     'output_tokens': payload['max_tokens'], 'reserve_tokens': 2048,
                     'context': service['configured_context'], 'fits': fits,
                     'payload_sha256': hashlib.sha256(body).hexdigest()})
        if fits:
            accepted.append(dict(packet, prompt_tokens=count))
    save(report, {'rows': rows, 'accepted': len(accepted), 'chat_calls': 0})
    return accepted
