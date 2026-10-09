import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from non_dictionary_pipeline import choose_cards, validate_entry, prepare_one, run_queue, latest_progress_time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NonDictionaryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_entry(self):
        source = self.root / 'without_dictionary.jsonl'
        source.write_text('{"id":"n1","name":"术语"}\n', encoding='utf-8')
        full = self.root / 'full.json'
        dictionary = self.root / 'dictionary.json'
        full.write_text('full', encoding='utf-8')
        dictionary.write_text('dictionary', encoding='utf-8')
        entry = {'subject': 'sample', 'without_dictionary': 1,
                 'full_source': {'path': str(full), 'sha256': digest(full)},
                 'dictionary_source': {'path': str(dictionary), 'sha256': digest(dictionary)}}
        return entry, source, full

    def make_cards(self):
        old = self.root / 'old' / 'sample'
        cards = old / 'boundaries'
        cards.mkdir(parents=True)
        tree = old / 'taxonomy.json'
        tree.write_text('{"name_zh":"学科","children":[]}', encoding='utf-8')
        (cards / 'source_tree.json').write_bytes(tree.read_bytes())
        (cards / 'summary.json').write_text(json.dumps({
            'total_tree_nodes': 1, 'accepted_candidate_cards': 1, 'source_unchanged': True
        }), encoding='utf-8')
        (cards / 'knowledge_tree.json').write_text('{"name_zh":"学科","children":[]}', encoding='utf-8')
        (cards / 'semantic_cards.jsonl').write_text('{"code":"root"}\n', encoding='utf-8')
        return old, cards, tree

    def test_reused_cards_require_complete_coverage_and_identical_tree(self):
        old, cards, tree = self.make_cards()
        (cards / 'summary.json').write_text(json.dumps({
            'total_tree_nodes': 3, 'accepted_candidate_cards': 2, 'source_unchanged': True
        }), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'complete boundary'):
            choose_cards(old, tree)
        (cards / 'summary.json').write_text(json.dumps({
            'total_tree_nodes': 3, 'accepted_candidate_cards': 3, 'source_unchanged': True
        }), encoding='utf-8')
        self.assertEqual(choose_cards(old, tree), cards)
        tree.write_text('{"name_zh":"changed","children":[]}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'tree mismatch'):
            choose_cards(old, tree)

    def test_non_dictionary_input_requires_unchanged_deliveries(self):
        entry, source, full = self.make_entry()
        self.assertEqual(validate_entry(entry, source)['records'], 1)
        full.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'full source changed'):
            validate_entry(entry, source)

    def test_prepare_one_freezes_input_and_reused_cards(self):
        entry, source, _ = self.make_entry()
        old, _, _ = self.make_cards()
        destination = self.root / 'new' / 'sample'
        manifest = prepare_one(entry, source, old, destination)
        self.assertEqual(manifest['records'], 1)
        self.assertEqual((destination / 'source.snapshot').read_bytes(), source.read_bytes())
        self.assertEqual((destination / 'boundaries' / 'semantic_cards.jsonl').read_text(encoding='utf-8'), '{"code":"root"}\n')
        self.assertEqual(manifest['source_sha256'], digest(source))

    def test_failed_subject_does_not_block_next_subject(self):
        run_root = self.root / 'run'
        run_root.mkdir()
        (run_root / 'inventory.json').write_text(json.dumps([
            {'subject': 'first', 'records': 1},
            {'subject': 'second', 'records': 2},
            {'subject': 'third', 'records': 3},
        ]), encoding='utf-8')

        def controlled_run(subject, *_):
            if subject == 'first':
                raise RuntimeError('one subject failed')
            return {'subject': subject, 'state': 'completed', 'returncode': 0}

        with patch('non_dictionary_pipeline.old_dictionary_active', return_value=False), \
                patch('non_dictionary_pipeline.run_subject', side_effect=controlled_run):
            run_queue(root=run_root, old_root=self.root, max_parallel=2, workers=4,
                      timeout_seconds=2, wait_for_dictionary_seconds=0)
        status = json.loads((run_root / 'queue_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['finished'], 3)
        self.assertEqual(status['states']['orchestrator_error'], 1)
        self.assertEqual(status['states']['completed'], 2)

    def test_activity_watchdog_uses_latest_stage_progress(self):
        destination = self.root / 'subject'
        (destination / 'audit').mkdir(parents=True)
        old = destination / 'status.json'
        new = destination / 'audit' / 'progress.json'
        old.write_text('{}', encoding='utf-8')
        new.write_text('{}', encoding='utf-8')
        import os
        os.utime(old, (100, 100))
        os.utime(new, (200, 200))
        self.assertEqual(latest_progress_time(destination, 50), 200)
        self.assertEqual(latest_progress_time(destination, 300), 300)


if __name__ == '__main__':
    unittest.main()
