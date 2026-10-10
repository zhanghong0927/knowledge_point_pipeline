"""Boundary generation must be a real, fail-closed mounting handoff."""

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_mounting_bridge import bridge, fake_routes, fixture

ROOT = Path(__file__).resolve().parents[1]


def generated_fixture(args, verdict='pass', missing=False, fallback=False, unreviewed=False, all_fallback=False):
    group = args.out / 'groups/mechanical_engineering'
    destination = group / 'boundaries'
    destination.mkdir(parents=True)
    index = bridge.read(group / 'node_index.json')
    generator = bridge.load_module(
        ROOT / 'modules/mounting/pipeline/generate_semantic_boundaries.py', 'boundary_fixture')
    nodes = generator.normalize(bridge.read(group / 'boundary_input.json'))
    cards = []
    for node in nodes:
        payload = {'node_code': node['code'], 'definition': node['name_zh'] + '的对象定义。',
                   'boundary': '以该节点原分类树范围为准，不改变节点结构。',
                   'includes': ['原节点覆盖的对象'], 'excludes': ['无关对象'],
                   'cross_boundary_rule': '按对象及功能选择最相关分支。', 'sibling_distinctions': []}
        cards.extend(generator.validate_cards({'cards': [payload]}, [node]))
    if missing:
        cards.pop()
    bridge.write_rows(destination / 'cross_validated_cards.jsonl', cards)
    bridge.dump(destination / 'normalized_nodes.json', nodes)
    cross = destination / 'cross_reviews'
    cross.mkdir()
    by_code = {card['node_code']: card for card in cards}
    if not missing:
        groups = destination / 'groups'
        groups.mkdir()
        for parent in dict.fromkeys(node['parent_code'] for node in nodes):
            for number, payload in enumerate(generator.planned_groups(nodes, parent, by_code)):
                group_cards = [by_code[node['code']] for node in payload['targets']]
                review = {'verdict': 'pass', 'checked_codes': [card['node_code'] for card in group_cards], 'issues': []}
                result = {'status': 'accepted_candidate', 'cards': group_cards, 'rounds': [
                    {'generation': {'result': group_cards}, 'review': {'result': review}}]}
                if unreviewed:
                    result = {'status': 'unreviewed_candidate', 'cards': group_cards,
                              'rounds': [{'generation': {'result': group_cards, 'attempts': [{
                                  'raw': {'choices': [{'finish_reason': 'stop', 'message': {
                                      'content': json.dumps({'cards': group_cards}, ensure_ascii=False)}}]}}]}}]}
                key = (parent or '__root__') + '#' + str(number)
                bridge.dump(groups / (hashlib.sha256(key.encode()).hexdigest()[:24] + '.json'),
                            {'payload': payload, 'output': result})
        for node in ([] if unreviewed else nodes):
            payload = generator.single_cross_payload(nodes, node['code'], by_code)
            result = {'verdict': 'pass', 'checked_codes': [node['code']], 'issues': [],
                      'self_issues': [], 'self_checks': [{'node_code': node['code'],
                                                        'verdict': 'pass', 'reason': 'Card is internally consistent.'}],
                      'ancestor_checks': [{'ancestor_code': ancestor['node']['code'],
                                           'verdict': 'pass', 'reason': 'Scope is compatible.'}
                                          for ancestor in payload['ancestors']]}
            bridge.dump(cross / (node['code'] + '.json'), {'payload': payload, 'output': {'result': result}})
    (destination / 'source_tree.json').write_bytes((group / 'boundary_input.json').read_bytes())
    bridge.dump(destination / 'summary.json', {
        'total_tree_nodes': len(index), 'selected_nodes': len(index),
        'accepted_candidate_cards': len(cards), 'full_tree_run': True,
        'source_unchanged': True, 'expert_approved': False,
        'status_counts': {'accepted_candidate': len(cards)}, 'advisory_nodes': 0,
        'cross_review': 'on', 'review_mode': 'strict',
        'cross_groups': len(index), 'cross_verdict_counts': {verdict: len(index)},
        'cross_validated_release': verdict == 'pass' and not missing,
    })
    if unreviewed:
        bridge.write_rows(destination / 'runtime_cards.jsonl', cards)
        bridge.write_rows(destination / 'cross_validated_cards.jsonl', [])
        bridge.dump(destination / 'boundary_fallbacks.json', [])
        summary = bridge.read(destination / 'summary.json')
        summary.update(accepted_candidate_cards=0, unreviewed_candidate_cards=len(cards),
                       status_counts={'unreviewed_candidate': len(cards)}, review_mode='off',
                       cross_review='off', cross_groups=0, cross_verdict_counts={},
                       cross_validated_release=False, model_reviewed=False,
                       runtime_release=not missing, failure_policy='strict', empty_boundary_nodes=0,
                       semantic_quality_degraded=False)
        bridge.dump(destination / 'summary.json', summary)
    if fallback:
        sys.path.insert(0, str(ROOT / 'modules/mounting/pipeline'))
        import boundary_recovery as recovery
        leaf_codes = {node['code'] for node in nodes if all_fallback or node['depth'] == 2}
        cards = [recovery.empty_card(node, 'technical_failure') if node['code'] in leaf_codes
                 else by_code[node['code']] for node in nodes]
        for path in (destination / 'groups').glob('*.json'):
            saved = bridge.read(path)
            if saved['payload']['targets'][0]['code'] in leaf_codes:
                target_codes = {node['code'] for node in saved['payload']['targets']}
                group_cards = [card for card in cards if card['node_code'] in target_codes]
                saved['payload'] = generator.group_payload(nodes, saved['payload']['targets'][0]['parent_code'],
                    {card['node_code']: card for card in cards}, target_codes)
                saved['output'] = {'status': recovery.EMPTY, 'reason': 'technical_failure',
                                   'cards': group_cards, 'rounds': []}
                bridge.dump(path, saved)
        for path in cross.glob('*.json'):
            if bridge.read(path)['payload']['cards'][0]['node_code'] in leaf_codes:
                path.unlink()
        bridge.write_rows(destination / 'runtime_cards.jsonl', cards)
        bridge.write_rows(destination / 'cross_validated_cards.jsonl', [])
        bridge.dump(destination / 'boundary_fallbacks.json', [
            {'node_code': card['node_code'], 'path': card['node_path'], 'reason': card['fallback_reason']}
            for card in cards if card.get('provenance') == recovery.EMPTY])
        summary = bridge.read(destination / 'summary.json')
        normal = len(cards) - len(leaf_codes)
        summary.update(accepted_candidate_cards=normal,
                       status_counts={**({'accepted_candidate': normal} if normal else {}), recovery.EMPTY: len(leaf_codes)},
                       cross_groups=normal, cross_verdict_counts={'pass': normal} if normal else {}, cross_validated_release=False,
                       runtime_release=True, failure_policy='empty', empty_boundary_nodes=len(leaf_codes),
                       semantic_quality_degraded=True)
        bridge.dump(destination / 'summary.json', summary)


class MountingBoundaryTests(unittest.TestCase):
    def prepare(self, root, policy='strict'):
        args, records = fixture(root)
        args.out = root / 'with_boundaries'
        args.require_boundaries = True
        args.boundary_context_bytes = 50000
        args.boundary_failure_policy = policy
        args.boundary_rewrite_rounds = 2
        bridge.prepare(args)
        return args, records

    def generate(self, args, verdict='pass', missing=False, fallback=False):
        def fake_native(command, **kwargs):
            self.assertIn('--cross-review', command)
            self.assertEqual(command[command.index('--cross-review') + 1], 'on')
            self.assertEqual(command[command.index('--review-mode') + 1], 'strict')
            self.assertEqual(command[command.index('--max-depth') + 1], '-1')
            self.assertEqual(kwargs['env']['BOUNDARY_MAX_TOKENS'], str(args.max_tokens))
            generated_fixture(args, verdict, missing, fallback)
        with patch.object(bridge.subprocess, 'run', side_effect=fake_native):
            return bridge.generate_boundaries(args)

    def prepare_unreviewed(self, root):
        args, records = fixture(root)
        args.out = root / 'unreviewed_boundaries'
        args.require_boundaries = True
        args.boundary_review_mode = 'off'
        args.boundary_context_bytes = 50000
        args.boundary_failure_policy = 'strict'
        args.boundary_rewrite_rounds = 2
        bridge.prepare(args)
        return args, records

    def test_unreviewed_mode_generates_without_group_or_cross_audit(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare_unreviewed(Path(d))
            def native(command, **kwargs):
                self.assertEqual(command[command.index('--review-mode') + 1], 'off')
                self.assertEqual(command[command.index('--cross-review') + 1], 'off')
                generated_fixture(args, unreviewed=True)
            with patch.object(bridge.subprocess, 'run', side_effect=native):
                bridge.generate_boundaries(args)
            report = bridge.verify_boundaries(args)
            self.assertEqual(report['verified_nodes'], 0)
            self.assertEqual(report['unreviewed_nodes'], 4)
            self.assertFalse(report['model_reviewed'])
            manifest = bridge.frozen(args.out)
            self.assertTrue(manifest['boundaries_verified'])
            self.assertFalse(manifest['boundary_model_reviewed'])
            fake_routes(args)
            items, _ = bridge.review_plan(args.out, manifest)
            self.assertTrue(all(card['boundary_status'] == 'model_generated_unreviewed'
                                for card in items[0]['taxonomy_context']['node_semantic_cards']))

    def test_unreviewed_handoff_still_rejects_missing_generation_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare_unreviewed(Path(d))
            with patch.object(bridge.subprocess, 'run', side_effect=lambda *a, **k:
                              generated_fixture(args, unreviewed=True)):
                bridge.generate_boundaries(args)
            path = args.out / 'BOUNDARIES_GENERATED.json'
            report = bridge.read(path)
            assets = report['groups']['mechanical_engineering']['assets']
            name = next(name for name in assets if Path(name).parent.name == 'groups')
            saved = bridge.read(args.out / name)
            saved['output']['rounds'][0]['generation']['result'] = []
            bridge.dump(args.out / name, saved)
            assets[name] = bridge.sha(args.out / name)
            bridge.dump(path, report)
            with self.assertRaisesRegex(ValueError, '[Gg]eneration|identity'):
                bridge.verify_boundaries(args)
            self.assertFalse((args.out / 'BOUNDARIES_VERIFIED.json').exists())

    def test_review_mode_cannot_change_after_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare_unreviewed(Path(d))
            args.boundary_review_mode = 'strict'
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('No network')):
                with self.assertRaisesRegex(ValueError, 'review.*changed'):
                    bridge.generate_boundaries(args)

    def test_pipeline_passes_explicit_review_mode_to_preparation_and_generation(self):
        spec = importlib.util.spec_from_file_location('unreviewed_pipeline', ROOT / 'pipeline.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        config, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        config['mounting']['boundaries'].update(review_mode='off')
        tasks = pipeline.plan(config, values)[4]['tasks']
        for name in ('prepare_mounting', 'generate_mounting_boundaries'):
            command = next(task['command'] for task in tasks if task['name'] == name)
            self.assertEqual(command[command.index('--boundary-review-mode') + 1], 'off')
        config['mounting']['boundaries']['review_mode'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'review'):
            pipeline.plan(config, values)

    def test_explicit_empty_fallback_activates_without_claiming_full_semantic_review(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d), policy='empty')
            self.generate(args, fallback=True)
            report = bridge.verify_boundaries(args)
            self.assertEqual(report['verified_nodes'], 2)
            self.assertEqual(report['fallback_nodes'], 2)
            manifest = bridge.frozen(args.out)
            self.assertTrue(manifest['boundary_quality_degraded'])
            cards = bridge.rows(args.out / 'groups/mechanical_engineering/profile/data/semantic_cards.jsonl')
            self.assertEqual(sum(card['semantic_card'] == '' for card in cards), 2)
            fake_routes(args)
            items, _ = bridge.review_plan(args.out, manifest)
            contexts = items[0]['taxonomy_context']['node_semantic_cards']
            self.assertEqual(contexts[-1]['boundary_status'], 'empty_boundary_fallback')
            self.assertEqual(contexts[-1]['semantic_card'], '')

    def test_strict_policy_never_accepts_empty_fallback_report(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            self.generate(args, fallback=True)
            with self.assertRaisesRegex(ValueError, 'incomplete|policy|fallback'):
                bridge.verify_boundaries(args)

    def test_all_empty_fallback_never_claims_model_reviewed_cards(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d), policy='empty')
            with patch.object(bridge.subprocess, 'run', side_effect=lambda *a, **k:
                              generated_fixture(args, fallback=True, all_fallback=True)):
                bridge.generate_boundaries(args)
            report = bridge.verify_boundaries(args)
            self.assertEqual(report['verified_nodes'], 0)
            self.assertFalse(report['model_reviewed'])
            self.assertFalse(bridge.frozen(args.out)['boundary_model_reviewed'])

    def test_fallback_with_invented_text_is_rejected_even_when_hashes_match(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d), policy='empty')
            self.generate(args, fallback=True)
            path = args.out / 'groups/mechanical_engineering/boundaries/runtime_cards.jsonl'
            values = bridge.rows(path)
            values[-1]['semantic_card'] = 'Invented supposedly approved scope'
            bridge.write_rows(path, values)
            report = bridge.read(args.out / 'BOUNDARIES_GENERATED.json')
            report['groups']['mechanical_engineering']['assets'][str(path.relative_to(args.out))] = bridge.sha(path)
            for group_path in (path.parent / 'groups').glob('*.json'):
                saved = bridge.read(group_path)
                if saved['output']['status'] == 'empty_boundary_fallback':
                    saved['output']['cards'][-1]['semantic_card'] = values[-1]['semantic_card']
                    bridge.dump(group_path, saved)
                    report['groups']['mechanical_engineering']['assets'][str(group_path.relative_to(args.out))] = bridge.sha(group_path)
            bridge.dump(args.out / 'BOUNDARIES_GENERATED.json', report)
            with self.assertRaisesRegex(ValueError, 'Fallback|fallback|identity|content'):
                bridge.verify_boundaries(args)

    def test_pipeline_schedules_generation_and_verification_before_routing(self):
        spec = importlib.util.spec_from_file_location('boundary_pipeline', ROOT / 'pipeline.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        config, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        config['mounting']['boundaries'] = {'enabled': True, 'workers': 1024, 'max_tokens': 8192}
        tasks = pipeline.plan(config, values)[4]['tasks']
        self.assertEqual([task['name'] for task in tasks], [
            'prepare_mounting', 'generate_mounting_boundaries', 'verify_mounting_boundaries',
            'route_mounting', 'review_mounting', 'export_mounting'])
        self.assertIn('--require-boundaries', tasks[0]['command'])
        self.assertIn(str(Path(values['run']) / '05_mounting/BOUNDARIES_VERIFIED.json'), tasks[3]['requires'])
        self.assertEqual(tasks[1]['command'][tasks[1]['command'].index('--workers') + 1], '1024')
        config['hooks']['mounting'] = [['echo', 'custom']]
        with self.assertRaisesRegex(ValueError, 'boundar'):
            pipeline.plan(config, values)

    def test_pending_boundaries_block_routing(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            with self.assertRaisesRegex(ValueError, 'boundar'):
                bridge.frozen(args.out)

    def test_generated_cards_are_installed_and_source_fields_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            args, records = self.prepare(Path(d))
            self.generate(args)
            report = bridge.verify_boundaries(args)
            self.assertEqual(report['verified_nodes'], 4)
            manifest = bridge.frozen(args.out)
            group = args.out / 'groups/mechanical_engineering'
            self.assertEqual(bridge.rows(group / 'profile/data/semantic_cards.jsonl'),
                             bridge.rows(group / 'boundaries/cross_validated_cards.jsonl'))
            self.assertTrue(manifest['boundaries_verified'])
            self.assertFalse(report['expert_approved'])
            self.assertEqual(bridge.rows(args.out / 'input.snapshot.jsonl'), records)
            self.assertEqual(list(bridge.read(group / 'original_records.json').values()), records)
            (group / 'boundaries/summary.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                bridge.frozen(args.out)

    def test_unreleased_or_incomplete_boundaries_never_activate(self):
        for verdict, missing in [('needs_revision', False), ('pass', True)]:
            with self.subTest(verdict=verdict, missing=missing), tempfile.TemporaryDirectory() as d:
                args, _ = self.prepare(Path(d))
                profile = args.out / 'groups/mechanical_engineering/profile/data/semantic_cards.jsonl'
                before = profile.read_bytes()
                self.generate(args, verdict, missing)
                with self.assertRaisesRegex(ValueError, 'boundar'):
                    bridge.verify_boundaries(args)
                self.assertEqual(profile.read_bytes(), before)
                self.assertFalse((args.out / 'BOUNDARIES_VERIFIED.json').exists())

    def test_generation_input_keeps_existing_context_and_stable_codes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args, _ = fixture(root)
            tree = bridge.read(args.taxonomy)
            tree['boundary'] = '机械系统及其部件，不改动原树。'
            bridge.dump(args.taxonomy, tree)
            args.out = root / 'context_mount'
            args.require_boundaries = True
            bridge.prepare(args)
            group = args.out / 'groups/mechanical_engineering'
            nodes = bridge.read(group / 'boundary_input.json')['nodes']
            index = bridge.read(group / 'node_index.json')
            self.assertEqual({n['node_code'] for n in nodes}, set(index))
            self.assertEqual(nodes[0]['boundary'], tree['boundary'])
            self.assertEqual([n['path'] for n in nodes], [n['path'] for n in index.values()])

    def test_changed_taxonomy_stops_generation_before_network(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            bridge.dump(args.taxonomy, {'name': 'different'})
            with patch.object(bridge.subprocess, 'run', side_effect=AssertionError('No network')):
                with self.assertRaisesRegex(ValueError, 'taxonomy changed'):
                    bridge.generate_boundaries(args)

    def test_cross_review_files_are_required_not_only_summary_claim(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            self.generate(args)
            report_path = args.out / 'BOUNDARIES_GENERATED.json'
            report = bridge.read(report_path)
            assets = report['groups']['mechanical_engineering']['assets']
            name = next(name for name in assets if 'cross_reviews' in Path(name).parts)
            (args.out / name).unlink()
            del assets[name]
            bridge.dump(report_path, report)
            with self.assertRaisesRegex(ValueError, 'cross-review coverage'):
                bridge.verify_boundaries(args)
            self.assertFalse((args.out / 'BOUNDARIES_VERIFIED.json').exists())

    def test_path_review_receives_verified_node_and_ancestor_cards(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            self.generate(args)
            bridge.verify_boundaries(args)
            fake_routes(args)
            items, _ = bridge.review_plan(args.out, bridge.frozen(args.out))
            cards = items[0]['taxonomy_context']['node_semantic_cards']
            self.assertEqual([card['path'] for card in cards], ['机械工程', '机械工程/传动', '机械工程/传动/齿轮'])
            self.assertTrue(all('主挂载' in card['semantic_card'] for card in cards))

    def test_group_review_files_are_required_not_only_summary_claim(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = self.prepare(Path(d))
            self.generate(args)
            path = args.out / 'BOUNDARIES_GENERATED.json'
            report = bridge.read(path)
            assets = report['groups']['mechanical_engineering']['assets']
            name = next(name for name in assets if Path(name).parent.name == 'groups')
            (args.out / name).unlink()
            del assets[name]
            bridge.dump(path, report)
            with self.assertRaisesRegex(ValueError, 'group.*coverage'):
                bridge.verify_boundaries(args)

    def test_empty_mounting_groups_cannot_be_declared_boundary_verified(self):
        with tempfile.TemporaryDirectory() as d:
            args, _ = fixture(Path(d), records=[])
            args.out = Path(d) / 'empty_boundaries'
            args.require_boundaries = True
            args.boundary_context_bytes = 50000
            bridge.prepare(args)
            with self.assertRaisesRegex(ValueError, 'boundary.*groups'):
                bridge.generate_boundaries(args)

    def test_full_card_mode_does_not_silently_cut_exclusion_rules(self):
        native = bridge.load_module(bridge.NATIVE / 'scripts/hierarchical-knowledge-labeling-beam-v3.py', 'boundary_wire_test')
        text = 'Scope: ' + 'a' * 700 + '; Excludes: outside objects.'
        node = native.TreeNode(code='n', name_zh='n', name_en='', path='n', semantic_card=text, examples=[])
        self.assertEqual(native.node_payload(node, 0)['semantic_card'], text)
        self.assertLess(len(native.node_payload(node)['semantic_card']), len(text))

    def test_cleaning_proof_subject_cannot_be_overridden_by_record_tag(self):
        from cleaning_handoff import seal_export
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            args, records = fixture(root)
            export = root / 'cleaned'
            export.mkdir()
            bridge.write_rows(export / 'records.jsonl', records)
            bridge.write_rows(export / 'trace.jsonl', [])
            report = {'records': len(records), **seal_export(export, [args.taxonomy], 'dictionary', '哲学')}
            bridge.dump(export / 'report.json', report)
            args.input = export / 'records.jsonl'
            args.out = root / 'wrong_proof_mount'
            args.require_cleaned = True
            with self.assertRaisesRegex(ValueError, 'cleaning.*subject'):
                bridge.prepare(args)
            self.assertFalse(args.out.exists())


if __name__ == '__main__':
    unittest.main()
