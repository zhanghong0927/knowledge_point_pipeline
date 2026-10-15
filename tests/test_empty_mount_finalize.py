"""Execute the production summary block after an empty mounting handoff."""
import ast
import inspect
import tempfile
from pathlib import Path
import unittest

import staged_benchmark as bench


class EmptyMountFinalizeTests(unittest.TestCase):
    def test_summary_survives_empty_disposition_dict_and_retains_unfinished_marker(self):
        tree = ast.parse(inspect.getsource(bench.run_track))
        body = tree.body[0].body
        start = next(i for i, node in enumerate(body) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'unresolved_stages'
                             for t in node.targets))
        # Use the actual closeout statements; upstream model stages are outside
        # this regression and must never run merely to test an empty dictionary.
        tail = ast.FunctionDef(name='finish', args=ast.arguments(posonlyargs=[], args=[],
            kwonlyargs=[], kw_defaults=[], defaults=[]), body=body[start:], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[tail], type_ignores=[]))
        with tempfile.TemporaryDirectory() as tmp:
            values = dict(vars(bench))
            values.update(root=Path(tmp), track='important', front=[], admissions=[], selected=[{}],
                cleaned=[], cleaned_record_count=0, mounts=[], prepared_mounts=[], with_boundaries=[], ready_mounts=[],
                successful=[], dedup=[], partial_extraction_books=0, extraction_unresolved_items=0,
                failed_boundaries=[], degraded_boundaries=[], remaining_cleaning_errors=0,
                mount_transport_unresolved=0, review_format_semantic_unresolved=0,
                mount_dispositions={}, boundary_rounds={}, route_rounds={}, review_rounds={},
                final_outputs={'missing_exports':0}, cohort={'shortfall':0})
            exec(compile(module, '<production-summary>', 'exec'), values)
            result = values['finish']()
        self.assertEqual(result['status'], 'finished_with_unresolved')
        self.assertIn('no_mounting_input', result['unresolved_stages'])
        self.assertFalse(result['semantic_quality_approved'])


if __name__ == '__main__': unittest.main()
