import unittest
from pathlib import Path
import tempfile
import hashlib

import benchmark_support as support
from benchmark_support import select_cohort


class BenchmarkCohortTests(unittest.TestCase):
    def test_shortfall_is_not_a_quality_or_execution_failure(self):
        admitted = [{'subject': 'mechanical_engineering', 'book': {'identifier': str(i)}} for i in range(48)]
        self.assertEqual(len(select_cohort(admitted)), 48)

    def test_never_exceeds_target_and_is_deterministic(self):
        admitted = [{'subject': 'mechanical_engineering', 'book': {'identifier': str(i)}} for i in range(80)]
        self.assertEqual(len(select_cohort(admitted)), 50)
        self.assertEqual(select_cohort(admitted), select_cohort(list(reversed(admitted))))

    def test_zero_is_empty_not_fabricated_success(self):
        self.assertEqual(select_cohort([]), [])

    def test_duplicate_identifiers_still_fail(self):
        item = {'subject': 'x', 'book': {'identifier': 'a'}}
        with self.assertRaises(ValueError):
            select_cohort([item, item])

    def test_source_snapshot_is_rechecked_before_reusing_classification(self):
        self.assertTrue(callable(getattr(support, 'validate_sources', None)))
        with tempfile.TemporaryDirectory() as directory:
            md = Path(directory) / 'book.md'
            md.write_text('Original entry', encoding='utf-8')
            stat = md.stat()
            prep = {'source_stats': [{'path': str(md), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}],
                    'md_sha256': hashlib.sha256(md.read_bytes()).hexdigest()}
            support.validate_sources(prep)
            md.write_text('Changed entry', encoding='utf-8')
            with self.assertRaises(ValueError):
                support.validate_sources(prep)


if __name__ == '__main__':
    unittest.main()
