"""Generate and verify full-tree boundaries before activating mounting profiles."""

import os
import hashlib
import subprocess
import sys
import shutil

import mounting_bridge as bridge
import mounting_tree_cache as cache

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
    policy = manifest.get('boundary_failure_policy', 'strict')
    review_mode = manifest.get('boundary_review_mode', 'strict')
    if getattr(args, 'boundary_failure_policy', 'strict') != policy:
        raise ValueError('Boundary failure policy changed after preparation')
    if getattr(args, 'boundary_review_mode', 'strict') != review_mode:
        raise ValueError('Boundary review mode changed after preparation')
    configured_cache = getattr(args, 'boundary_cache_dir', None)
    cache_dir = str(configured_cache.resolve()) if configured_cache else None
    if manifest.get('boundary_cache_dir') != cache_dir:
        raise ValueError('Boundary cache directory changed after preparation')
    if cache_dir and getattr(args, 'boundary_reuse_root', None):
        raise ValueError('Use either a taxonomy cache or a legacy reuse root, not both')
    for slug in manifest['groups']:
        target = args.out / 'groups' / slug
        destination = target / 'boundaries'
        generation = dict(api_root=base, max_context_bytes=args.boundary_context_bytes,
                          max_tokens=args.max_tokens, rewrite_rounds=getattr(args, 'boundary_rewrite_rounds', 2),
                          workers=args.workers, timeout=args.timeout)
        cache_identity = cache.identity(slug, manifest['groups'][slug], target, review_mode, policy, args.model, generation)
        reused = cache.locate(cache_dir, cache_identity) if cache_dir else None
        command = [sys.executable, str(GENERATOR), '--tree', str(target / 'boundary_input.json'),
                   '--out', str(destination), '--base', base, '--model', args.model,
                   '--workers', str(args.workers), '--max-depth', '-1',
                   '--max-context-bytes', str(args.boundary_context_bytes),
                   '--review-mode', review_mode, '--cross-review', 'off' if review_mode == 'off' else 'on',
                   '--failure-policy', policy,
                   '--rewrite-rounds', str(getattr(args, 'boundary_rewrite_rounds', 2))]
        reuse_root = getattr(args, 'boundary_reuse_root', None)
        if reuse_root:
            command += ['--reuse-run', str(reuse_root / 'groups' / slug / 'boundaries')]
        if destination.exists():
            command.append('--resume')
        env = dict(os.environ, BOUNDARY_HTTP_TIMEOUT=str(args.timeout),
                   BOUNDARY_MAX_TOKENS=str(args.max_tokens))
        if key:
            env['BOUNDARY_API_KEY'] = key
        bridge.dump(target / 'boundary_command.json', command)
        # The native script records failed/review nodes even when its process exits zero.
        if reused:
            if destination.exists():
                raise FileExistsError('Cached boundary import requires a new generation directory')
            shutil.copytree(reused / 'boundaries', destination)
        else:
            subprocess.run(command, env=env, check=True)
        summary = bridge.read(destination / 'summary.json')
        report['groups'][slug] = {'summary': summary, 'cache_identity': cache_identity,
            'cache_reused': bool(reused), 'cache_source': str(reused) if reused else None, 'assets': {
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
    sys.path.insert(0, str(GENERATOR.parent))
    recovery = bridge.load_module(GENERATOR.with_name('boundary_recovery.py'), 'mount_boundary_recovery_verifier')
    unreviewed = manifest.get('boundary_review_mode', 'strict') == 'off'
    validator = (bridge.load_module(GENERATOR.with_name('boundary_unreviewed.py'), 'mount_boundary_unreviewed_verifier')
                 if unreviewed else recovery)
    allow_empty = manifest.get('boundary_failure_policy', 'strict') == 'empty'
    selected = {}
    report = {'groups': {}, 'verified_nodes': 0, 'unreviewed_nodes': 0,
              'fallback_nodes': 0, 'operational_nodes': 0, 'expert_approved': False,
              'review_mode': 'off' if unreviewed else 'strict', 'model_reviewed': not unreviewed}
    # Validate every subject before replacing any runtime asset.
    for slug, generated_group in generated['groups'].items():
        destination = args.out / 'groups' / slug / 'boundaries'
        actual = {str(path.relative_to(args.out)) for path in cache.approved_evidence(destination)}
        if actual != set(generated_group['assets']):
            raise ValueError('Generated boundary evidence file coverage changed')
        for name, digest in generated_group['assets'].items():
            if bridge.sha(args.out / name) != digest:
                raise ValueError('Generated boundary asset changed: ' + name)
        target = args.out / 'groups' / slug
        destination = target / 'boundaries'
        nodes = native.normalize(bridge.read(target / 'boundary_input.json'))
        count = len(nodes)
        summary = bridge.read(destination / 'summary.json')
        if (summary != generated_group['summary'] or not summary.get('full_tree_run')
                or not summary.get('source_unchanged')
                or summary.get('cross_review') != ('off' if unreviewed else 'on')
                or summary.get('review_mode') != ('off' if unreviewed else 'strict')
                or summary.get('advisory_nodes') != 0
                or summary.get('total_tree_nodes') != count or summary.get('selected_nodes') != count
                or (unreviewed and (summary.get('model_reviewed') is not False
                                   or summary.get('cross_validated_release') is not False
                                   or not summary.get('runtime_release')))
                or (not unreviewed and not allow_empty and not summary.get('cross_validated_release'))
                or (allow_empty and (summary.get('failure_policy') != 'empty' or not summary.get('runtime_release')))):
            raise ValueError('Semantic boundaries are incomplete or did not pass strict review: ' + slug)
        if (bridge.sha(destination / 'source_tree.json') != bridge.sha(target / 'boundary_input.json')
                or bridge.read(destination / 'normalized_nodes.json') != nodes):
            raise ValueError('Semantic boundary taxonomy identity changed: ' + slug)
        cards_file = 'runtime_cards.jsonl' if allow_empty or unreviewed else 'cross_validated_cards.jsonl'
        cards = bridge.rows(destination / cards_file)
        by_code = {card.get('node_code'): card for card in cards}
        if len(by_code) != len(cards) or set(by_code) != {node['code'] for node in nodes}:
            raise ValueError('Semantic boundary node coverage mismatch: ' + slug)
        fallback_codes = {card['node_code'] for card in cards if card.get('provenance') == recovery.EMPTY}
        if fallback_codes and not allow_empty:
            raise ValueError('Empty boundary fallback is not authorized')
        normal_codes = set(by_code) - fallback_codes
        candidate_status = 'unreviewed_candidate' if unreviewed else 'accepted_candidate'
        expected_status = {candidate_status: len(normal_codes)} if normal_codes else {}
        if fallback_codes:
            expected_status[recovery.EMPTY] = len(fallback_codes)
        expected_cross = set() if unreviewed else normal_codes
        if (summary.get('accepted_candidate_cards') != (0 if unreviewed else len(normal_codes))
                or (unreviewed and summary.get('unreviewed_candidate_cards') != len(normal_codes))
                or summary.get('status_counts') != expected_status
                or summary.get('cross_groups') != len(expected_cross)
                or summary.get('cross_verdict_counts') != ({'pass': len(expected_cross)} if expected_cross else {})):
            raise ValueError('Semantic boundary coverage or review counts mismatch')
        if allow_empty:
            expected_fallbacks = [dict(node_code=card['node_code'], path=card['node_path'], reason=card['fallback_reason'])
                                  for card in cards if card['node_code'] in fallback_codes]
            if (summary.get('empty_boundary_nodes') != len(fallback_codes)
                    or summary.get('semantic_quality_degraded') != bool(fallback_codes)
                    or summary.get('cross_validated_release') != (not unreviewed and not fallback_codes)
                    or bridge.read(destination / 'boundary_fallbacks.json') != expected_fallbacks):
                raise ValueError('Empty fallback ledger mismatch')
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
            if saved['payload'] != payload or result.get('cards') != group_cards:
                raise ValueError('Semantic boundary group-generation evidence mismatch')
            validator.validate_group(payload, result, allow_empty=allow_empty)
            if any(card['node_code'] in normal_codes for card in group_cards) and any(
                    item['card'].get('provenance') == recovery.EMPTY for item in payload['ancestors']):
                raise ValueError('Reviewed boundary cannot inherit an empty parent context')
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
        if reviewed != expected_cross:
            raise ValueError('Semantic boundary cross-review coverage mismatch')
        selected[slug] = cards
        report['groups'][slug] = {'nodes': count, 'taxonomy_sha256': manifest['groups'][slug]['source_sha256'],
                                  'cards_sha256': bridge.sha(destination / cards_file),
                                  'cross_reviewed_nodes': len(reviewed), 'verified_nodes': 0 if unreviewed else len(normal_codes),
                                  'unreviewed_nodes': len(normal_codes) if unreviewed else 0,
                                  'structurally_validated_nodes': count,
                                  'fallback_nodes': len(fallback_codes)}
        report['verified_nodes'] += 0 if unreviewed else len(normal_codes)
        report['unreviewed_nodes'] += len(normal_codes) if unreviewed else 0
        report['fallback_nodes'] += len(fallback_codes)
        report['operational_nodes'] += count
    for slug, cards in selected.items():
        target = args.out / 'groups' / slug
        profile = target / 'profile'
        bridge.write_rows(profile / 'data/semantic_cards.jsonl', cards)
        group = manifest['groups'][slug]
        group['semantic_cards'] = ('Generated boundaries without model review; identity and format checks only; not semantic approval'
                                   if unreviewed else
                                   'Reviewed generated boundaries; explicitly empty fallback nodes use original tree names and paths only; not expert approval')
        group['semantic_boundaries'] = report['groups'][slug]
        tree = cache.enriched_tree(target, cards)
        if not unreviewed:
            def mark_reviewed(node):
                if node['boundary_status'] != 'empty_boundary_fallback':
                    node['boundary_status'] = 'model_generated_reviewed'
                for child in node['children']:
                    mark_reviewed(child)
            mark_reviewed(tree)
        bridge.dump(target / 'taxonomy_enriched.json', tree)
        if manifest.get('boundary_cache_dir'):
            value = generated['groups'][slug]['cache_identity']
            expected = cache.identity(slug, group, target, manifest['boundary_review_mode'],
                                      manifest['boundary_failure_policy'], value['model'], value['generation'])
            if value != expected:
                raise ValueError('Generated taxonomy cache identity changed')
            asset = cache.publish(manifest['boundary_cache_dir'], value, target, group, report['groups'][slug])
            report['groups'][slug]['taxonomy_asset'] = str(asset)
            report['groups'][slug]['cache_reused'] = generated['groups'][slug]['cache_reused']
        assets = dict(generated['groups'][slug]['assets'])
        assets[str((target / 'taxonomy_enriched.json').relative_to(args.out))] = bridge.sha(target / 'taxonomy_enriched.json')
        assets.update({str(path.relative_to(args.out)): bridge.sha(path)
                       for path in profile.rglob('*') if path.is_file()})
        group['assets'].update(assets)
    report['model_reviewed'] = report['verified_nodes'] > 0
    report['generation_report_sha256'] = bridge.sha(generated_path)
    bridge.dump(args.out / 'BOUNDARIES_VERIFIED.json', report)
    manifest.update(boundaries_verified=True,
                    boundary_model_reviewed=report['model_reviewed'],
                    boundary_quality_degraded=report['fallback_nodes'] > 0,
                    boundary_fallback_nodes=report['fallback_nodes'],
                    boundaries_verified_sha256=bridge.sha(args.out / 'BOUNDARIES_VERIFIED.json'),
                    boundary_generation_report_sha256=bridge.sha(generated_path))
    bridge.dump(args.out / 'PREPARED.json', manifest)
    bridge.frozen(args.out)
    return report
