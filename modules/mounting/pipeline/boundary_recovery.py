"""Bounded rewrites and explicit empty descriptions, never fabricated approval."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path

import generate_semantic_boundaries as b

EMPTY = 'empty_boundary_fallback'


def empty_card(node, reason):
    return dict(node_code=node['code'], node_path=node['path'], node_name_zh=node['name_zh'],
                depth=node['depth'], definition='', boundary='', includes=[], excludes=[],
                sibling_distinctions=[], cross_boundary_rule='', semantic_card='',
                provenance=EMPTY, seed_examples=[], fallback_reason=reason)


def validate_group(payload, output, allow_empty=False):
    cards = output.get('cards', [])
    if output.get('status') == EMPTY:
        if not allow_empty or not output.get('reason'):
            raise ValueError('Empty boundary fallback is not authorized or lacks a reason')
        expected = [empty_card(node, output['reason']) for node in payload['targets']]
        if cards != expected:
            raise ValueError('Fallback card contains text or changed identity')
        return cards
    if output.get('status') != 'accepted_candidate':
        raise ValueError('Boundary group has not passed review')
    if b.validate_cards({'cards': cards}, payload['targets'], payload['sibling_context']) != cards:
        raise ValueError('Boundary group card identity mismatch')
    rounds = output.get('rounds', [])
    if not rounds or rounds[-1].get('generation', {}).get('result') != cards:
        raise ValueError('Boundary group generation evidence mismatch')
    review = rounds[-1].get('review', {}).get('result')
    if review is None or b.validate_review(review, [node['code'] for node in payload['targets']])['verdict'] != 'pass':
        raise ValueError('Boundary group review did not pass')
    return cards


def recover_group(base, model, payload, max_bytes, revisions, feedback=None, previous=None):
    working = dict(payload)
    if previous:
        cards = b.validate_cards({'cards': previous['cards']}, payload['targets'], payload['sibling_context'])
        working['previous_cards'] = cards
        if not feedback:
            review = b.call(base, model, b.REVIEW_PROMPT, dict(payload, cards=cards),
                            lambda obj: b.validate_review(obj, [node['code'] for node in payload['targets']]))
            if review['result'] and review['result']['verdict'] == 'pass':
                return dict(status='accepted_candidate', cards=cards, automatic_recovery='current_ancestors_rechecked',
                            rounds=[*previous['rounds'], {'generation': {'result': cards, 'attempts': [],
                                                                       'reused_generation': True}, 'review': review}])
            working['revision_feedback'] = review['result'] or {'reason': 'Current ancestor recheck failed'}
    if feedback:
        working['cross_revision_feedback'] = feedback
    result = b.process_group(base, model, working, max_bytes, revision_limit=revisions)
    if result['status'] == 'accepted_candidate':
        return result
    if result.get('reason') == 'tree_conflict' or len(payload['targets']) < 2:
        return result
    # Keep every sibling and ancestor, reducing only the generated target count.
    parts = []
    for target in payload['targets']:
        one = dict(working, targets=[target])
        part = b.process_group(base, model, one, max_bytes, revision_limit=revisions)
        parts.append(part)
    if not all(part['status'] == 'accepted_candidate' for part in parts):
        return dict(status='technical_failure' if any(part['status'] == 'technical_failure' for part in parts)
                    else 'needs_review', reason='single_target_recovery_exhausted',
                    rounds=result.get('rounds', []), recovery_parts=parts)
    cards = [card for part in parts for card in part['cards']]
    b.validate_cards({'cards': cards}, payload['targets'], payload['sibling_context'])
    review = b.call(base, model, b.REVIEW_PROMPT, dict(payload, cards=cards),
                    lambda obj: b.validate_review(obj, [node['code'] for node in payload['targets']]))
    if review['result'] is None or review['result']['verdict'] != 'pass':
        return dict(status='needs_review', reason='recovered_joint_review_failed',
                    rounds=result.get('rounds', []), recovery_parts=parts, recovery_review=review)
    return dict(status='accepted_candidate', cards=cards,
                automatic_recovery='single_target_full_sibling_context', recovery_parts=parts,
                rounds=[*result.get('rounds', []),
                        {'generation': {'result': cards, 'attempts': [], 'recovery': True}, 'review': review}])


def archive(path, history):
    if path.exists():
        history.mkdir(parents=True, exist_ok=True)
        b.atomic_json(history / (path.stem + '_' + hashlib.sha256(path.read_bytes()).hexdigest()[:12] + '.json'),
                      json.loads(path.read_text(encoding='utf-8')))


def run(nodes, out, base, model, workers, max_bytes, policy='empty', rewrite_rounds=2, reuse=None):
    if policy not in ('strict', 'empty') or not 0 <= rewrite_rounds <= 5:
        raise ValueError('Invalid boundary recovery policy')
    out = Path(out)
    groups = out / 'groups'
    groups.mkdir(parents=True, exist_ok=True)
    if reuse is not None:
        reuse = Path(reuse)
        if json.loads((reuse / 'normalized_nodes.json').read_text(encoding='utf-8')) != nodes:
            raise ValueError('Reused boundary taxonomy changed')
    b.atomic_json(out / 'normalized_nodes.json', nodes)
    by = {node['code']: node for node in nodes}
    accepted, states, code_groups = {}, {}, {}
    reused = set()
    depth_limit = max(node['depth'] for node in nodes)

    def top_down(rewrite=None, force_empty=None):
        rewrite, force_empty = rewrite or {}, force_empty or set()
        for depth in range(depth_limit + 1):
            pending = []
            parents = list(dict.fromkeys(node['parent_code'] for node in nodes if node['depth'] == depth))
            for parent in parents:
                if parent is not None and parent not in accepted:
                    for node in nodes:
                        if node['parent_code'] == parent:
                            states[node['code']] = 'blocked_by_parent'
                            accepted.pop(node['code'], None)
                    continue
                for number, payload in enumerate(b.planned_groups(nodes, parent, accepted)):
                    key = (parent or '__root__') + '#' + str(number)
                    path = groups / (hashlib.sha256(key.encode()).hexdigest()[:24] + '.json')
                    for node in payload['targets']:
                        code_groups[node['code']] = key
                    reason = ('cross_revision_exhausted' if key in force_empty else
                              'parent_empty_boundary' if parent and accepted[parent].get('provenance') == EMPTY else None)
                    if reason and policy == 'empty':
                        result = dict(status=EMPTY, reason=reason,
                                      cards=[empty_card(node, reason) for node in payload['targets']], rounds=[])
                        archive(path, out / 'group_history')
                        b.atomic_json(path, dict(payload=payload, output=result))
                        for node, card in zip(payload['targets'], result['cards']):
                            states[node['code']] = EMPTY
                            accepted[node['code']] = card
                        continue
                    saved_path = path if path.exists() else reuse / 'groups' / path.name if reuse else None
                    previous = None
                    if saved_path and saved_path.exists():
                        saved = json.loads(saved_path.read_text(encoding='utf-8'))
                        if saved['output'].get('status') == 'accepted_candidate':
                            validate_group(saved['payload'], saved['output'])
                            previous = saved['output']
                        if saved['payload'] == payload and previous and key not in rewrite:
                            cards = validate_group(payload, previous)
                            b.atomic_json(path, saved)
                            reused.add(key)
                            for card in cards:
                                accepted[card['node_code']] = card
                                states[card['node_code']] = 'accepted_candidate'
                            continue
                    pending.append((key, payload, path, previous))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                jobs = {pool.submit(recover_group, base, model, payload, max_bytes, rewrite_rounds,
                                    rewrite.get(key), previous): (key, payload, path) for key, payload, path, previous in pending}
                for future in as_completed(jobs):
                    key, payload, path = jobs[future]
                    result = future.result()
                    if result['status'] != 'accepted_candidate' and policy == 'empty':
                        reason = result.get('reason') or result['status']
                        result = dict(result, original_status=result['status'], status=EMPTY, reason=reason,
                                      cards=[empty_card(node, reason) for node in payload['targets']])
                    archive(path, out / 'group_history')
                    b.atomic_json(path, dict(payload=payload, output=result))
                    for node in payload['targets']:
                        states[node['code']] = result['status']
                        accepted.pop(node['code'], None)
                    for card in result.get('cards', []):
                        accepted[card['node_code']] = card
                    print('GROUP', key, result['status'], 'AVAILABLE_CARDS', len(accepted), flush=True)
            print('LEVEL_DONE', depth, 'available_cards', len(accepted), flush=True)

    cross = out / 'cross_reviews'
    cross.mkdir(exist_ok=True)
    reuse_cross = (reuse and (reuse / 'cross_prompt.txt').is_file()
                   and (reuse / 'cross_prompt.txt').read_text(encoding='utf-8') == b.PAIR_PROMPT)
    (out / 'cross_prompt.txt').write_text(b.PAIR_PROMPT, encoding='utf-8')
    if reuse_cross and (reuse / 'cross_reviews').is_dir():
        for path in (reuse / 'cross_reviews').glob('*.json'):
            if not (cross / path.name).exists():
                b.atomic_json(cross / path.name, json.loads(path.read_text(encoding='utf-8')))

    def audit():
        for path in cross.glob('*.json'):
            saved = json.loads(path.read_text(encoding='utf-8'))
            code = saved['payload']['cards'][0]['node_code']
            if code not in accepted or accepted[code].get('provenance') == EMPTY:
                archive(path, out / 'cross_history')
                path.unlink()
        codes, reviews = b.cross_audit(nodes, accepted, base, model, workers, max_bytes, cross, allow_changed=True)
        return codes, reviews

    def failures(codes, reviews):
        failed = {}
        for code, result in zip(codes, reviews):
            if result['result'] is None or result['result']['verdict'] != 'pass':
                affected = {code_groups[code]}
                if result['result']:
                    affected.update(code_groups[issue['ancestor_code']] for issue in result['result']['issues'])
                record = {'node_code': code, 'review': result['result'], 'error': result.get('error'),
                          'attempt_errors': [attempt.get('error') for attempt in result.get('attempts', [])]}
                for key in affected:
                    failed.setdefault(key, []).append(record)
        return failed

    top_down()
    codes, reviews = audit()
    used_rounds = 0
    for _ in range(rewrite_rounds):
        failed = failures(codes, reviews)
        if not failed:
            break
        used_rounds += 1
        b.atomic_json(out / ('cross_rewrite_' + str(used_rounds) + '.json'), failed)
        top_down(rewrite=failed)
        codes, reviews = audit()
    unresolved = failures(codes, reviews)
    if unresolved and policy == 'empty':
        b.atomic_json(out / 'cross_unresolved_before_fallback.json', unresolved)
        top_down(force_empty=set(unresolved))
        codes, reviews = audit()
    cards = [accepted[node['code']] for node in nodes if node['code'] in accepted]
    fallback = [card for card in cards if card.get('provenance') == EMPTY]
    complete = (len(cards) == len(nodes) and not failures(codes, reviews)
                and all(states.get(node['code']) in ('accepted_candidate', EMPTY) for node in nodes))
    released = complete and not fallback
    runtime_release = complete and (not fallback or policy == 'empty')
    for name, values in [('semantic_cards.jsonl', cards), ('runtime_cards.jsonl', cards if runtime_release else []),
                         ('cross_validated_cards.jsonl', cards if released else [])]:
        (out / name).write_text(''.join(json.dumps(card, ensure_ascii=False) + '\n' for card in values), encoding='utf-8')
    b.atomic_json(out / 'boundary_fallbacks.json', [dict(node_code=card['node_code'], path=card['node_path'],
                                                     reason=card['fallback_reason']) for card in fallback])
    b.atomic_json(out / 'node_status.json', [dict(node_code=node['code'], path=node['path'], depth=node['depth'],
                                               status=states.get(node['code'], 'blocked_by_parent')) for node in nodes])
    b.atomic_json(out / 'cross_issues.json', [issue for review in reviews if review['result'] for issue in review['result']['issues']])
    def export(code):
        node = by[code]
        return dict(code=code, name_zh=node['name_zh'], name_en=node['name_en'], path=node['path'],
                    children=[export(child) for child in node['children_codes']])
    b.atomic_json(out / 'knowledge_tree.json', export(next(node['code'] for node in nodes if node['parent_code'] is None)))
    summary = dict(total_tree_nodes=len(nodes), selected_nodes=len(nodes), accepted_candidate_cards=len(cards)-len(fallback),
                   status_counts=dict(Counter(states.values())), max_depth=depth_limit, full_tree_run=True,
                   expert_approved=False, source_unchanged=True, review_mode='strict', cross_review='on', advisory_nodes=0,
                   cross_groups=len(codes), cross_verdict_counts=dict(Counter(
                       review['result']['verdict'] if review['result'] else 'technical_failure' for review in reviews)),
                   cross_validated_release=released, runtime_release=runtime_release, failure_policy=policy,
                   empty_boundary_nodes=len(fallback), semantic_quality_degraded=bool(fallback),
                   cross_rewrite_rounds=used_rounds, reused_groups=len(reused),
                   semantic_cards_over_700=sum(len(card['semantic_card'])>700 for card in cards))
    b.atomic_json(out / 'summary.json', summary)
    return summary
