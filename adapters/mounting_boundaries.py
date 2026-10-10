"""Generate and verify full-tree boundaries before activating mounting profiles."""

import os
import hashlib
import subprocess
import sys

import mounting_bridge as bridge

GENERATOR = bridge.ROOT / 'modules/mounting/pipeline/generate_semantic_boundaries.py'


def generate(args):
    manifest = bridge.frozen(args.out, allow_pending_boundaries=True)
    if not manifest.get('require_semantic_boundaries') or manifest.get('boundaries_verified'):
        raise ValueError('Prepare a new mounting run with required semantic boundaries')
    if not manifest['groups']:
        raise ValueError('No configured boundary subject groups to generate')
    if (args.out / 'BOUNDARIES_GENERATED.json').exists():
        raise FileExistsError('Boundary generation already ended; use a new run directory')
    base, key = bridge.api_settings(args) if manifest['groups'] else ('', '')
    report = {'groups': {}, 'expert_approved': False}
    for slug in manifest['groups']:
        target = args.out / 'groups' / slug
        destination = target / 'boundaries'
        command = [sys.executable, str(GENERATOR), '--tree', str(target / 'boundary_input.json'),
                   '--out', str(destination), '--base', base, '--model', args.model,
                   '--workers', str(args.workers), '--max-depth', '-1',
                   '--max-context-bytes', str(args.boundary_context_bytes),
                   '--review-mode', 'strict', '--cross-review', 'on']
        if destination.exists():
            command.append('--resume')
        env = dict(os.environ, BOUNDARY_HTTP_TIMEOUT=str(args.timeout),
                   BOUNDARY_MAX_TOKENS=str(args.max_tokens))
        if key:
            env['BOUNDARY_API_KEY'] = key
        bridge.dump(target / 'boundary_command.json', command)
        # The native script records failed/review nodes even when its process exits zero.
        subprocess.run(command, env=env, check=True)
        summary = bridge.read(destination / 'summary.json')
        report['groups'][slug] = {'summary': summary, 'assets': {
            str(path.relative_to(args.out)): bridge.sha(path)
            for path in destination.rglob('*') if path.is_file()}}
    bridge.frozen(args.out, allow_pending_boundaries=True)
    bridge.dump(args.out / 'BOUNDARIES_GENERATED.json', report)
    return report


def verify(args):
    manifest = bridge.frozen(args.out, allow_pending_boundaries=True)
    if not manifest.get('require_semantic_boundaries') or manifest.get('boundaries_verified'):
        raise ValueError('No pending semantic boundary handoff to verify')
    generated_path = args.out / 'BOUNDARIES_GENERATED.json'
    generated = bridge.read(generated_path)
    if set(generated['groups']) != set(manifest['groups']):
        raise ValueError('Semantic boundary subject coverage mismatch')
    if not generated['groups']:
        raise ValueError('No boundary subject groups to verify')
    native = bridge.load_module(GENERATOR, 'mount_boundary_verifier')
    selected = {}
    report = {'groups': {}, 'verified_nodes': 0, 'expert_approved': False}
    # Validate every subject before replacing any runtime asset.
    for slug, generated_group in generated['groups'].items():
        for name, digest in generated_group['assets'].items():
            if bridge.sha(args.out / name) != digest:
                raise ValueError('Generated boundary asset changed: ' + name)
        target = args.out / 'groups' / slug
        destination = target / 'boundaries'
        nodes = native.normalize(bridge.read(target / 'boundary_input.json'))
        count = len(nodes)
        summary = bridge.read(destination / 'summary.json')
        if (summary != generated_group['summary'] or not summary.get('full_tree_run')
                or not summary.get('source_unchanged') or not summary.get('cross_validated_release')
                or summary.get('cross_review') != 'on' or summary.get('review_mode') != 'strict'
                or summary.get('advisory_nodes') != 0
                or summary.get('total_tree_nodes') != count or summary.get('selected_nodes') != count
                or summary.get('accepted_candidate_cards') != count
                or summary.get('status_counts') != {'accepted_candidate': count}
                or summary.get('cross_groups') != count - 1
                or summary.get('cross_verdict_counts') != ({'pass': count - 1} if count > 1 else {})):
            raise ValueError('Semantic boundaries are incomplete or did not pass strict review: ' + slug)
        if (bridge.sha(destination / 'source_tree.json') != bridge.sha(target / 'boundary_input.json')
                or bridge.read(destination / 'normalized_nodes.json') != nodes):
            raise ValueError('Semantic boundary taxonomy identity changed: ' + slug)
        cards = bridge.rows(destination / 'cross_validated_cards.jsonl')
        by_code = {card.get('node_code'): card for card in cards}
        if len(by_code) != len(cards) or set(by_code) != {node['code'] for node in nodes}:
            raise ValueError('Semantic boundary node coverage mismatch: ' + slug)
        for parent in dict.fromkeys(node['parent_code'] for node in nodes):
            siblings = [node for node in nodes if node['parent_code'] == parent]
            group_cards = [by_code[node['code']] for node in siblings]
            if native.validate_cards({'cards': group_cards}, siblings) != group_cards:
                raise ValueError('Semantic boundary card identity or content changed: ' + slug)
        expected_groups = {}
        for parent in dict.fromkeys(node['parent_code'] for node in nodes):
            for number, payload in enumerate(native.planned_groups(nodes, parent, by_code)):
                key = (parent or '__root__') + '#' + str(number)
                filename = hashlib.sha256(key.encode()).hexdigest()[:24] + '.json'
                expected_groups[filename] = payload
        group_files = {path.name: path for path in (destination / 'groups').glob('*.json')}
        if set(group_files) != set(expected_groups):
            raise ValueError('Semantic boundary group-review coverage mismatch')
        for name, payload in expected_groups.items():
            saved = bridge.read(group_files[name])
            result = saved['output']
            group_cards = [by_code[node['code']] for node in payload['targets']]
            rounds = result.get('rounds', [])
            if (saved['payload'] != payload or result.get('status') != 'accepted_candidate'
                    or result.get('cards') != group_cards or not rounds
                    or rounds[-1].get('generation', {}).get('result') != group_cards):
                raise ValueError('Semantic boundary group-generation evidence mismatch')
            review = rounds[-1].get('review', {}).get('result')
            if review is None or native.validate_review(review, [card['node_code'] for card in group_cards])['verdict'] != 'pass':
                raise ValueError('Semantic boundary group review failed')
        cross_reviews = list((destination / 'cross_reviews').glob('*.json'))
        reviewed = set()
        for path in cross_reviews:
            saved = bridge.read(path)
            payload = saved['payload']
            if len(payload.get('cards', [])) != 1:
                raise ValueError('Invalid semantic boundary cross review')
            code = payload['cards'][0]['node_code']
            if code in reviewed or payload != native.single_cross_payload(nodes, code, by_code):
                raise ValueError('Semantic boundary cross-review identity mismatch')
            result = saved['output']['result']
            if result is None or native.validate_pair_review(result, payload)['verdict'] != 'pass':
                raise ValueError('Semantic boundary cross review failed')
            reviewed.add(code)
        if reviewed != {node['code'] for node in nodes if node['parent_code'] is not None}:
            raise ValueError('Semantic boundary cross-review coverage mismatch')
        selected[slug] = cards
        report['groups'][slug] = {'nodes': count, 'taxonomy_sha256': manifest['groups'][slug]['source_sha256'],
                                  'cards_sha256': bridge.sha(destination / 'cross_validated_cards.jsonl'),
                                  'cross_reviewed_nodes': len(reviewed)}
        report['verified_nodes'] += count
    for slug, cards in selected.items():
        target = args.out / 'groups' / slug
        profile = target / 'profile'
        bridge.write_rows(profile / 'data/semantic_cards.jsonl', cards)
        group = manifest['groups'][slug]
        group['semantic_cards'] = 'full-tree LLM generation; strict sibling and ancestor review; not expert approval'
        group['semantic_boundaries'] = report['groups'][slug]
        assets = dict(generated['groups'][slug]['assets'])
        assets.update({str(path.relative_to(args.out)): bridge.sha(path)
                       for path in profile.rglob('*') if path.is_file()})
        group['assets'].update(assets)
    report['generation_report_sha256'] = bridge.sha(generated_path)
    bridge.dump(args.out / 'BOUNDARIES_VERIFIED.json', report)
    manifest.update(boundaries_verified=True,
                    boundaries_verified_sha256=bridge.sha(args.out / 'BOUNDARIES_VERIFIED.json'),
                    boundary_generation_report_sha256=bridge.sha(generated_path))
    bridge.dump(args.out / 'PREPARED.json', manifest)
    bridge.frozen(args.out)
    return report
