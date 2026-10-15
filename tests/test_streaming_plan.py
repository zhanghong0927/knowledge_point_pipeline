import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('streaming_pipeline', ROOT / 'pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class StreamingPlanTests(unittest.TestCase):
    def config(self, track='dictionary'):
        cfg, values = pipeline.load_config(ROOT / 'configs/pipeline.example.json')
        cfg['track'] = track
        return cfg, values

    def test_legacy_stays_two_tasks(self):
        cfg, values = self.config()
        names = [t['name'] for t in pipeline.plan(cfg, values)[1]['tasks']]
        self.assertIn('prepare_structure', names)
        self.assertIn('classify', names)
        self.assertNotIn('stream_structure', names)

    def test_streaming_both_tracks_keeps_verification(self):
        for track in ('dictionary', 'important'):
            with self.subTest(track=track):
                cfg, values = self.config(track)
                cfg['structure_streaming'] = {'enabled': True, 'prepare_workers': 2, 'max_pending': 32}
                tasks = pipeline.plan(cfg, values)[1]['tasks']
                stream = next(t for t in tasks if t['name'] == 'stream_structure')
                self.assertIn('prepare-run', stream['command'])
                self.assertEqual(stream['command'][-4:], ['--prepare-workers', '2', '--max-pending', '32'])
                self.assertIn(values['classification_model_config'], stream['requires'])
                self.assertTrue(any(t['name'] == 'verify_classification' for t in tasks))
                self.assertFalse(any(t['name'] == 'classify' for t in tasks))

    def test_invalid_streaming_limits_rejected(self):
        for options in ({'enabled': 'true'}, {'enabled': True, 'prepare_workers': 0},
                        {'enabled': True, 'max_pending': True}):
            cfg, values = self.config()
            cfg['structure_streaming'] = options
            with self.assertRaises(ValueError):
                pipeline.plan(cfg, values)

    def test_important_selection_wires_existing_proof(self):
        cfg, values = self.config('important')
        cfg['important_selection'] = {'enabled': True}
        values['approved_books'] = str(ROOT / 'example_selected.json')
        task = pipeline.plan(cfg, values)[2]['tasks'][0]
        self.assertIn('--selected-books', task['command'])
        self.assertIn(values['approved_books'], task['requires'])

    def test_selection_never_silently_ignored(self):
        cfg, values = self.config('important')
        cfg['important_selection'] = {'enabled': True}
        values['approved_books'] = ''
        with self.assertRaises(ValueError):
            pipeline.plan(cfg, values)
        values['approved_books'] = str(ROOT / 'example_selected.json')
        cfg['important_extraction'] = 'llm'
        with self.assertRaises(ValueError):
            pipeline.plan(cfg, values)


if __name__ == '__main__':
    unittest.main()
