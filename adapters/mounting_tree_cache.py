"""Immutable, checksum-bound taxonomy assets shared by independent runs."""

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import mounting_bridge as bridge

GENERATOR_DIR = bridge.ROOT / 'modules/mounting/pipeline'


def identity(slug, group, target, review_mode, policy, model, generation):
    versions = {name: bridge.sha(GENERATOR_DIR / name) for name in (
        'generate_semantic_boundaries.py', 'boundary_recovery.py', 'boundary_unreviewed.py')}
    versions['mounting_tree_cache.py'] = bridge.sha(Path(__file__))
    versions['mounting_boundaries.py'] = bridge.sha(Path(__file__).with_name('mounting_boundaries.py'))
    return dict(schema=1, subject_slug=slug, source_sha256=group['source_sha256'],
                boundary_input_sha256=bridge.sha(target / 'boundary_input.json'),
                review_mode=review_mode, failure_policy=policy, model=model,
                generator_versions=versions, generation=generation)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def locate(root, value):
    destination = Path(root) / value['subject_slug'] / key(value)
    return validate_asset(destination, value)


def validate_asset(destination, value):
    if not destination.exists():
        return None
    if destination.is_symlink():
        raise ValueError('Taxonomy asset must not be a symlink')
    manifest = bridge.read(destination / 'MANIFEST.json')
    if manifest.get('identity') != value or manifest.get('asset_key') != key(value):
        raise ValueError('Taxonomy asset identity changed')
    expected = manifest.get('assets', {})
    if any(path.is_symlink() for path in destination.rglob('*')):
        raise ValueError('Unexpected symlink in taxonomy asset')
    actual = {str(path.relative_to(destination)) for path in destination.rglob('*')
              if path.is_file() and path.name != 'MANIFEST.json'}
    if not expected or actual != set(expected):
        raise ValueError('Taxonomy asset file coverage changed')
    for name, digest in expected.items():
        path = destination / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink()
                or not path.resolve().is_relative_to(destination.resolve())
                or bridge.sha(path) != digest):
            raise ValueError('Taxonomy asset checksum changed: ' + name)
    if (bridge.sha(destination / 'taxonomy_original.json') != value['source_sha256']
            or bridge.sha(destination / 'boundary_input.json') != value['boundary_input_sha256']):
        raise ValueError('Taxonomy asset source changed')
    return destination


def approved_evidence(destination):
    top = {'config.json', 'models.json', 'source_tree.json', 'normalized_nodes.json',
           'generation_prompt.txt', 'review_prompt.txt', 'cross_prompt.txt', 'unreviewed_config.json',
           'semantic_cards.jsonl', 'runtime_cards.jsonl', 'cross_validated_cards.jsonl',
           'boundary_fallbacks.json', 'node_status.json', 'knowledge_tree.json', 'summary.json',
           'cross_issues.json', 'cross_warnings.json', 'cross_unresolved_before_fallback.json'}
    paths = []
    for path in destination.rglob('*'):
        if path.is_symlink():
            raise ValueError('Unexpected symlink in boundary evidence')
        if not path.is_file():
            continue
        relative = path.relative_to(destination)
        allowed = (len(relative.parts) == 1 and (path.name in top or re.fullmatch(r'cross_rewrite_\d+\.json', path.name)))
        if len(relative.parts) == 2 and relative.parts[0] in ('groups', 'cross_reviews', 'group_history', 'cross_history'):
            allowed = bool(re.fullmatch(r'[A-Za-z0-9_-]+\.json', path.name))
        if not allowed:
            raise ValueError('Unexpected extra boundary evidence file: ' + str(relative))
        paths.append(path)
    return paths


def enriched_tree(target, cards):
    tree = copy.deepcopy(bridge.read(target / 'profile/data/knowledge_tree.json'))
    by_code = {card['node_code']: card for card in cards}
    used = set()

    def add(node):
        card = by_code[node['code']]
        if card['node_path'] != node['path']:
            raise ValueError('Enriched taxonomy path changed')
        used.add(node['code'])
        node['semantic_boundary'] = {name: copy.deepcopy(card.get(name)) for name in (
            'definition', 'boundary', 'includes', 'excludes', 'cross_boundary_rule', 'sibling_distinctions')}
        node['semantic_card'] = card['semantic_card']
        node['boundary_status'] = ('empty_boundary_fallback' if card.get('provenance') == 'empty_boundary_fallback'
                                   else 'model_generated_unreviewed')
        for child in node['children']:
            add(child)

    add(tree)
    if used != set(by_code):
        raise ValueError('Enriched taxonomy node coverage changed')
    return tree


def publish(root, value, target, group, report):
    evidence = approved_evidence(target / 'boundaries')
    existing = locate(root, value)
    output_hash = bridge.sha(target / 'taxonomy_enriched.json')
    if existing and bridge.sha(existing / 'taxonomy_enriched.json') == output_hash:
        return existing
    destination = Path(root) / value['subject_slug'] / key(value)
    if existing:
        destination = destination.with_name(destination.name + '--' + output_hash)
        version = validate_asset(destination, value)
        if version:
            return version
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish only complete assets; readers never observe an unfinished version.
    with tempfile.TemporaryDirectory(prefix='.publishing-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'asset'
        staged.mkdir()
        for source in evidence:
            name = source.relative_to(target / 'boundaries')
            (staged / 'boundaries' / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged / 'boundaries' / name)
        for source, name in ((Path(group['taxonomy']), 'taxonomy_original.json'),
                             (target / 'boundary_input.json', 'boundary_input.json'),
                             (target / 'taxonomy_enriched.json', 'taxonomy_enriched.json')):
            shutil.copyfile(source, staged / name)
        bridge.dump(staged / 'MANIFEST.json', {
            'identity': value, 'asset_key': key(value), 'nodes': report['nodes'],
            'model_reviewed': report['verified_nodes'] > 0, 'expert_approved': False,
            'fallback_nodes': report['fallback_nodes'],
            'enriched_tree_sha256': output_hash,
            'assets': {str(path.relative_to(staged)): bridge.sha(path)
                       for path in staged.rglob('*') if path.is_file()},
        })
        try:
            staged.rename(destination)
        except OSError:
            collision = validate_asset(destination, value)
            if collision is None:
                raise
            if bridge.sha(collision / 'taxonomy_enriched.json') != output_hash:
                return publish(root, value, target, group, report)
    return validate_asset(destination, value)
