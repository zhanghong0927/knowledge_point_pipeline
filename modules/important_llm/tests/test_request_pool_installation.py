"""Import and dispatch an isolated installed package without the pipeline checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / 'src/book_extractor'
CODE = PACKAGE.parents[3]

# Optional model libraries are replaced at their import boundaries. Both actual
# package modules are imported normally; their executor and dispatcher code runs.
BOOTSTRAP = r'''
import concurrent.futures
import importlib
import importlib.abc
import importlib.util
import os
from pathlib import Path
import socket
import sys
import threading
import time
import types
import uuid

site, mode, helper_path = sys.argv[1:]
sys.path.insert(0, site)
def no_network(*_args, **_kwargs):
    raise AssertionError('network is forbidden in installation regressions')
socket.socket.connect = no_network
socket.create_connection = no_network

optional = {
    'instructor': (), 'instructor.core': (),
    'instructor.core.exceptions': ('IncompleteOutputException', 'InstructorRetryException'),
    'openai': ('APIStatusError',), 'pydantic': ('BaseModel', 'ValidationError'),
    'book_extractor.llm': ('ContextBudgetError', 'ExtractionCancelledError', 'LLMClient', 'fatal_service_error'),
    'book_extractor.markdown': ('Chunk', 'Unit', 'chunk_units', 'parse_markdown', 'token_count'),
    'book_extractor.models': ('Candidate', 'EvidenceSpan', 'NameDiscovery', 'Record', 'Regions',
                             'Finding', 'Grouping', 'IndependentSynthesis', 'SynthesisReview', 'is_english_source'),
    'book_extractor.prompts': ('DISCOVER', 'REGION_PROMPT', 'GROUP_PROMPT', 'INDEPENDENT_SYNTHESIS_PROMPT',
                              'SOURCE_ENGLISH_SUFFIX', 'SYNTHESIS_PROMPT', 'VERIFICATION_PROMPT'),
    'book_extractor.rule_candidates': ('extract_rule_candidates', 'rule_only_units', 'strip_structural_prefix'),
    'book_extractor.storage': ('replace_checkpoint',),
    'book_extractor.telemetry': ('EventLog', 'TelemetryWriteError', 'summarize_events'),
}
for name, attributes in optional.items():
    module = types.ModuleType(name)
    if name in ('instructor', 'instructor.core'):
        module.__path__ = []
    for attribute in attributes:
        setattr(module, attribute, type(attribute, (Exception,), {}))
    sys.modules[name] = module
sys.modules['book_extractor.llm'].DEFAULT_ATTEMPTS = 3

attempted = []
class MissingHelper(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'shared_request_pool':
            attempted.append(fullname)
            raise ModuleNotFoundError('standalone installation has no shared_request_pool', name=fullname)
sys.meta_path.insert(0, MissingHelper())

if mode == 'local':
    os.environ.pop('PIPELINE_SHARED_POOL_URL', None)
else:
    os.environ['PIPELINE_SHARED_POOL_URL'] = 'http://127.0.0.1:9'

shared = None
if mode == 'shared':
    spec = importlib.util.spec_from_file_location('shared_request_pool', helper_path)
    shared = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = shared
    spec.loader.exec_module(shared)
    budget = shared.ProducerBudget()
    class Client:
        def __init__(self, _url): self.owner = uuid.uuid4().hex
        def claim(self, count): return budget.claim(self.owner, count, limit=2)
        def release(self, count): budget.release(self.owner, count)
        def close(self): budget.close(self.owner)
    shared.LeaseClient = Client

pipeline = importlib.import_module('book_extractor.pipeline')
synthesis = importlib.import_module('book_extractor.synthesis')
assert not attempted, 'package import eagerly required the external helper'
if mode == 'shallow_missing':
    for module in (pipeline, synthesis):
        module.__file__ = str(Path(Path(module.__file__).anchor) / 'book_extractor' / (module.__name__.rsplit('.', 1)[1] + '.py'))

live = peak = 0
lock = threading.Lock()
def serial(records, *_args, **_kwargs):
    global live, peak
    with lock:
        live += 1
        peak = max(peak, live)
    time.sleep(.005)
    with lock:
        live -= 1
    return records, []
synthesis._verify_records_serial = serial

if mode in ('missing', 'shallow_missing'):
    for module in (pipeline, synthesis):
        try:
            module._request_executor(2)
        except ModuleNotFoundError as error:
            assert error.name == 'shared_request_pool', repr(error)
        else:
            raise AssertionError('configured shared gateway silently fell back to a local pool')
    try:
        synthesis._verify_records([1, 2], {}, types.SimpleNamespace(), Path('.'), workers=2)
    except ModuleNotFoundError as error:
        assert error.name == 'shared_request_pool'
    else:
        raise AssertionError('dispatcher ignored missing configured shared helper')
    assert peak == 0
else:
    for module in (pipeline, synthesis):
        with module._request_executor(2) as executor:
            assert isinstance(executor, shared.SharedExecutor if shared else concurrent.futures.ThreadPoolExecutor)
            assert executor.submit(lambda value: value + 1, 4).result(timeout=2) == 5
    actual, errors = synthesis._verify_records(list(range(8)), {}, types.SimpleNamespace(), Path('.'), workers=2)
    assert actual == list(range(8)) and errors == []
    assert 0 < peak <= 2
    if shared:
        assert budget.active == 0
    else:
        assert not attempted and 'shared_request_pool' not in sys.modules
print('ISOLATED_PACKAGE_OK=' + mode)
'''


class InstalledRequestPoolTests(unittest.TestCase):
    def run_isolated(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'site/book_extractor'
            target.mkdir(parents=True)
            for name in ('__init__.py', 'pipeline.py', 'synthesis.py'):
                shutil.copyfile(PACKAGE / name, target / name)
            env = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONPATH', 'PIPELINE_SHARED_POOL_URL')}
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', BOOTSTRAP,
                                     str(root / 'site'), mode, str(CODE / 'shared_request_pool.py')],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('ISOLATED_PACKAGE_OK=' + mode, result.stdout)

    def test_standalone_install_imports_and_dispatches_without_root_helper(self):
        self.run_isolated('local')

    def test_configured_shared_gateway_cannot_fall_back_when_helper_is_missing(self):
        self.run_isolated('missing')

    def test_configured_shared_gateway_with_shallow_install_reports_missing_helper(self):
        self.run_isolated('shallow_missing')

    def test_configured_shared_gateway_uses_real_shared_executor_when_available(self):
        self.run_isolated('shared')


if __name__ == '__main__':
    unittest.main()
