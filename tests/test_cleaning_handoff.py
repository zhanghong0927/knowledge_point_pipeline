import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adapters'))
import cleaning_handoff as handoff
import normalize_records as normalize
import pipeline


def dump(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def rows(path, values):
    path.write_text(''.join(json.dumps(v) + '\n' for v in values), encoding='utf-8')


class CleaningHandoffTests(unittest.TestCase):
    def test_important_restore_binds_only_native_keep_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'rule_pass.jsonl'
            rows(source, [{'id': 'keep', 'cleaning_record_id': 'internal-keep-key'},
                          {'id': 'pending', 'cleaning_record_id': 'internal-pending-key'}])
            judgments = root / 'judgments.jsonl'
            vote = {'decision': 'keep', 'name': 'Energy'}
            rows(judgments, [{'cleaning_record_id': 'internal-keep-key', 'model_cleaning': vote},
                            {'cleaning_record_id': 'internal-pending-key', 'model_cleaning': {'decision': 'review'}}])
            clean = root / 'clean.jsonl'
            rows(clean, [{'id': 'keep', 'name': 'Energy'}])
            report = root / 'model_report.json'
            dump(report, {'stage': 'model_clean', 'subject': 'Mechanical', 'finished_at': 'fixture',
                          'input': str(source), 'outputs': {'clean': str(clean), 'judgments': str(judgments)},
                          'counts': {'keep': 1, 'review': 1}})
            trace = root / 'trace.jsonl'
            rows(trace, [{'id': 'keep', 'subject': 'Mechanical', 'original_record': {'source': {'book': 'B1'}}}])
            argv = ['normalize_records.py', '--restore', '--input', str(clean), '--trace', str(trace),
                    '--cleaning-report', str(report), '--subject', 'Mechanical', '--slug', 'mechanical',
                    '--out', str(root / 'export')]
            with patch.object(sys, 'argv', argv):
                normalize.main()
            proof = handoff.verify_export(root / 'export/records.jsonl')
            self.assertEqual(proof['cleaning_export']['track'], 'important')
            value = json.loads((root / 'export/records.jsonl').read_text())
            self.assertEqual(value['source'], {'book': 'B1'})
            rows(clean, [{'id': 'pending', 'name': 'Energy'}])
            with self.assertRaisesRegex(ValueError, 'keep'):
                normalize.verify_model_keep(clean, report, 'Mechanical')
            with self.assertRaisesRegex(ValueError, 'changed'):
                handoff.verify_export(root / 'export/records.jsonl')

    @unittest.skipIf(sys.platform == 'win32', 'Scheduler locks require Linux fcntl')
    def test_resume_validates_cleaning_evidence_before_skip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / 'config.json'
            dump(cfg, {})
            export = root / 'export'
            export.mkdir()
            rows(export / 'records.jsonl', [{'id': '1', 'name': 'Energy'}])
            rows(export / 'trace.jsonl', [{'id': '1'}])
            evidence = root / 'STATE.json'
            dump(evidence, {'status': 'finished'})
            dump(export / 'report.json', {'records': 1, **handoff.seal_export(export, [evidence], 'dictionary', 'Mechanical')})
            task = {'name': 'clean_dictionary', 'command': ['fixture'], 'produces': [str(export / 'records.jsonl')],
                    'requires': [], 'cwd': str(root), 'env': {}, 'blocked': None}
            signature = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
            dump(root / 'pipeline_state.json', {'04.clean_dictionary': {'status': 'completed', 'signature': signature}})
            stages = [{'stage': '04', 'tasks': [task]}]
            pipeline.run_stages(cfg, {}, {'run': str(root)}, stages, True)
            dump(evidence, {'status': 'running'})
            with self.assertRaisesRegex(ValueError, 'changed'), patch.object(pipeline.subprocess, 'run') as call:
                pipeline.run_stages(cfg, {}, {'run': str(root)}, stages, True)
            call.assert_not_called()
