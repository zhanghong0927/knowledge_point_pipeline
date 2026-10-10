"""Full-tree boundary generation without model or cross review."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path

import boundary_recovery as recovery
import generate_semantic_boundaries as b


STATUS = 'unreviewed_candidate'
TECHNICAL = 'technical_failure'
EMPTY = recovery.EMPTY
FALLBACK_REASONS = {'context_byte_budget_exceeded', 'generation_technical_failure',
                    'parent_empty_boundary'}


def validate_group(payload, output, allow_empty=False):
    """Accept only exact generation evidence or an explicit technical empty card."""
    if not isinstance(output, dict):
        raise ValueError('Invalid boundary group output')
    if 'review' in output or 'verdict' in output:
        raise ValueError('Review evidence is forbidden in unreviewed output')
    cards = output.get('cards')
    rounds = output.get('rounds')
    if output.get('status') == EMPTY:
        reason = output.get('reason')
        if not allow_empty or reason not in FALLBACK_REASONS:
            raise ValueError('Empty boundary fallback requires a technical reason and policy')
        if not set(output) <= {'status', 'reason', 'cards', 'rounds', 'original_status', 'technical_error'}:
            raise ValueError('Unexpected fallback output fields')
        if cards != [recovery.empty_card(node, reason) for node in payload['targets']]:
            raise ValueError('Fallback card identity or text changed')
        if not isinstance(rounds, list) or len(rounds) > 1:
            raise ValueError('Invalid fallback generation rounds')
        if reason == 'parent_empty_boundary':
            if rounds:
                raise ValueError('Parent fallback cannot contain generation evidence')
        elif reason == 'generation_technical_failure':
            if len(rounds) != 1 or not isinstance(rounds[0], dict) or set(rounds[0]) != {'generation'}:
                raise ValueError('Missing technical generation evidence')
            generation = rounds[0]['generation']
            if not isinstance(generation, dict) or set(generation) != {'result', 'attempts'} or generation['result'] is not None or not isinstance(generation['attempts'], list):
                raise ValueError('Invalid technical generation evidence')
        elif rounds:
            raise ValueError('Budget fallback cannot contain generation evidence')
        return cards
    if output.get('status') != STATUS:
        raise ValueError('Not an unreviewed generation candidate')
    if set(output) != {'status', 'cards', 'rounds'}:
        raise ValueError('Unexpected unreviewed output fields')
    if not isinstance(rounds, list) or len(rounds) != 1 or not isinstance(rounds[0], dict) or set(rounds[0]) != {'generation'}:
        raise ValueError('Expected exactly one generation round without review')
    generation = rounds[0]['generation']
    if not isinstance(generation, dict) or set(generation) != {'result', 'attempts'}:
        raise ValueError('Invalid generation evidence')
    if not isinstance(generation['attempts'], list) or not generation['attempts']:
        raise ValueError('Missing generation attempts')
    if generation['result'] != cards:
        raise ValueError('Generation result differs from output cards')
    if b.validate_cards({'cards': cards}, payload['targets'], payload['sibling_context']) != cards:
        raise ValueError('Boundary group card identity mismatch')
    try:
        last = generation['attempts'][-1]
        choice = last['raw']['choices'][0]
        if choice['finish_reason'] != 'stop':
            raise ValueError('Generation did not finish normally')
        content = choice['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0]
        raw_object = json.loads(content)
        if not isinstance(raw_object, dict) or set(raw_object) != {'cards'} or not isinstance(raw_object['cards'], list):
            raise ValueError('Raw generation contains unexpected fields')
        if any(not isinstance(card, dict) or 'review' in card for card in raw_object['cards']):
            raise ValueError('Raw generation contains review evidence')
        actual = b.validate_cards(raw_object, payload['targets'], payload['sibling_context'])
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('Missing or invalid raw generation response') from exc
    if actual != cards:
        raise ValueError('Raw generation response differs from output cards')
    return cards


def generate_group(base, model, payload, max_bytes):
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > max_bytes:
        return dict(status=TECHNICAL, reason='context_byte_budget_exceeded',
                    technical_error='sibling context exceeds configured byte budget; no partial generation', rounds=[])
    def validate_generation(obj):
        if not isinstance(obj, dict) or set(obj) != {'cards'}:
            raise ValueError('Raw generation contains unexpected fields')
        if not isinstance(obj['cards'], list) or any(not isinstance(card, dict) or 'review' in card
                                                    for card in obj['cards']):
            raise ValueError('Raw generation contains invalid cards or review evidence')
        return b.validate_cards(obj, payload['targets'], payload['sibling_context'])
    generation = b.call(base, model, b.PROMPT, payload, validate_generation)
    rounds = [dict(generation=generation)]
    if generation['result'] is None:
        return dict(status=TECHNICAL, reason='generation_technical_failure', rounds=rounds)
    return dict(status=STATUS, cards=generation['result'], rounds=rounds)


def run(nodes, out, base, model, workers, max_bytes, policy='strict', reuse=None):
    if policy not in ('strict', 'empty') or not isinstance(workers, int) or not 1 <= workers <= 1024 or max_bytes <= 0:
        raise ValueError('Invalid unreviewed generation policy or limits')
    if not nodes:
        raise ValueError('Full tree requires nodes')
    out = Path(out)
    groups = out / 'groups'
    groups.mkdir(parents=True, exist_ok=True)
    normalized = out / 'normalized_nodes.json'
    if normalized.exists() and json.loads(normalized.read_text(encoding='utf-8')) != nodes:
        raise ValueError('Resume taxonomy changed')
    prompt_file = out / 'generation_prompt.txt'
    if prompt_file.exists() and prompt_file.read_text(encoding='utf-8') != b.PROMPT:
        raise ValueError('Resume generation prompt changed')
    settings = out / 'unreviewed_config.json'
    config = dict(base=base.rstrip('/'), model=model, max_bytes=max_bytes, policy=policy)
    if settings.exists() and json.loads(settings.read_text(encoding='utf-8')) != config:
        raise ValueError('Resume generation configuration changed')
    if reuse is not None:
        reuse = Path(reuse)
        if json.loads((reuse / 'normalized_nodes.json').read_text(encoding='utf-8')) != nodes:
            raise ValueError('Reused boundary taxonomy changed')
        if (reuse / 'generation_prompt.txt').read_text(encoding='utf-8') != b.PROMPT:
            raise ValueError('Reused generation prompt changed')
        reuse_config = reuse / 'unreviewed_config.json'
        if not reuse_config.exists() or json.loads(reuse_config.read_text(encoding='utf-8')) != config:
            raise ValueError('Reused generation configuration changed')
    b.atomic_json(normalized, nodes)
    b.atomic_json(settings, config)
    prompt_file.write_text(b.PROMPT, encoding='utf-8')

    by = {node['code']: node for node in nodes}
    roots = [node for node in nodes if node['parent_code'] is None]
    if len(roots) != 1 or len(by) != len(nodes):
        raise ValueError('Full tree requires one root and unique codes')
    cards_by_code, states = {}, {}
    reused = set()
    attempted = set()
    depth_limit = max(node['depth'] for node in nodes)

    def checkpoint(parent, number):
        key = (parent or '__root__') + '#' + str(number)
        return key, groups / (hashlib.sha256(key.encode()).hexdigest()[:24] + '.json')

    def record(payload, result):
        for node in payload['targets']:
            states[node['code']] = result['status']
        for card in result.get('cards', []):
            cards_by_code[card['node_code']] = card

    for depth in range(depth_limit + 1):
        pending = []
        parents = list(dict.fromkeys(node['parent_code'] for node in nodes if node['depth'] == depth))
        for parent in parents:
            if parent is not None and parent not in cards_by_code:
                for node in nodes:
                    if node['parent_code'] == parent:
                        states[node['code']] = 'blocked_by_parent'
                continue
            for number, payload in enumerate(b.planned_groups(nodes, parent, cards_by_code)):
                key, path = checkpoint(parent, number)
                if parent is not None and cards_by_code[parent]['provenance'] == EMPTY and policy == 'empty':
                    result = dict(status=EMPTY, reason='parent_empty_boundary', rounds=[],
                                  cards=[recovery.empty_card(node, 'parent_empty_boundary') for node in payload['targets']])
                    validate_group(payload, result, allow_empty=True)
                    if path.exists():
                        recovery.archive(path, out / 'group_history')
                    b.atomic_json(path, dict(payload=payload, output=result))
                    record(payload, result)
                    continue
                saved_path = path if path.exists() else (reuse / 'groups' / path.name if reuse else None)
                if saved_path is not None and saved_path.exists():
                    saved = json.loads(saved_path.read_text(encoding='utf-8'))
                    if saved.get('payload') == payload and saved.get('output', {}).get('status') == STATUS:
                        validate_group(payload, saved['output'])
                        if path != saved_path:
                            b.atomic_json(path, saved)
                        reused.add(key)
                        record(payload, saved['output'])
                        continue
                pending.append((key, payload, path))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(generate_group, base.rstrip('/'), model, payload, max_bytes): (key, payload, path)
                       for key, payload, path in pending}
            for future in as_completed(futures):
                key, payload, path = futures[future]
                result = future.result()
                attempted.add(key)
                if result['status'] == TECHNICAL and policy == 'empty':
                    reason = result['reason']
                    result = dict(result, original_status=TECHNICAL, status=EMPTY, cards=[
                        recovery.empty_card(node, reason) for node in payload['targets']])
                if result['status'] in (STATUS, EMPTY):
                    validate_group(payload, result, allow_empty=policy == 'empty')
                if path.exists():
                    recovery.archive(path, out / 'group_history')
                b.atomic_json(path, dict(payload=payload, output=result))
                record(payload, result)
    for node in nodes:
        states.setdefault(node['code'], 'blocked_by_parent')
    cards = [cards_by_code[node['code']] for node in nodes if node['code'] in cards_by_code]
    fallback = [card for card in cards if card['provenance'] == EMPTY]
    source_unchanged = json.loads(normalized.read_text(encoding='utf-8')) == nodes
    complete = (source_unchanged and len(cards) == len(nodes)
                and all(states[node['code']] in (STATUS, EMPTY) for node in nodes)
                and (policy == 'empty' or not fallback))
    for name, values in (('semantic_cards.jsonl', cards),
                         ('runtime_cards.jsonl', cards if complete else []),
                         ('cross_validated_cards.jsonl', [])):
        (out / name).write_text(''.join(json.dumps(card, ensure_ascii=False) + '\n' for card in values), encoding='utf-8')
    b.atomic_json(out / 'boundary_fallbacks.json', [dict(node_code=card['node_code'], path=card['node_path'],
                                                        reason=card['fallback_reason']) for card in fallback])
    b.atomic_json(out / 'node_status.json', [dict(node_code=node['code'], path=node['path'], depth=node['depth'],
                                             status=states[node['code']]) for node in nodes])

    def export(code):
        node = by[code]
        return dict(code=code, name_zh=node['name_zh'], name_en=node['name_en'], path=node['path'],
                    children=[export(child) for child in node['children_codes']])

    b.atomic_json(out / 'knowledge_tree.json', export(roots[0]['code']))
    summary = dict(total_tree_nodes=len(nodes), selected_nodes=len(nodes), full_tree_run=True,
                   source_unchanged=source_unchanged, expert_approved=False, review_mode='off', cross_review='off',
                   cross_groups=0, cross_verdict_counts={}, cross_validated_release=False,
                   model_reviewed=False, runtime_release=complete, failure_policy=policy,
                   unreviewed_candidate_cards=sum(card['provenance'] == 'llm_candidate' for card in cards),
                   accepted_candidate_cards=0, empty_boundary_nodes=len(fallback),
                   status_counts=dict(Counter(states[node['code']] for node in nodes)), advisory_nodes=0,
                   max_depth=depth_limit, reused_groups=len(reused), groups_attempted=len(attempted),
                   semantic_quality_degraded=bool(fallback))
    b.atomic_json(out / 'summary.json', summary)
    return summary
