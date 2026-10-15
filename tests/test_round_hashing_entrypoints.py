"""The real round entrypoints must opt in; an unused hashing utility is not a fix."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import round_integrity
import staged_benchmark

ROOT=Path(__file__).resolve().parents[1]


def load(relative, name):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name=='posix','POSIX production file-fingerprint reuse')
class EntrypointHashingTests(unittest.TestCase):
    def test_front_finalization_environment_forces_identity_sources_to_be_read(self):
        screening=load('modules/book_screening/scripts/staged_screening_state.py','final_front_identity')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'source.csv';f.write_bytes(b'input')
            with round_integrity.verification_session():
                screening.identity([f],{'model':'fixture'})
                with patch.dict(os.environ,{'STAGED_SCREENING_FINALIZE':'1'}),patch.object(round_integrity,'_read_digest',wraps=round_integrity._read_digest) as reads:
                    screening.identity([f],{'model':'fixture'})
                self.assertEqual(sum(Path(c.args[0])==f.resolve() for c in reads.call_args_list),1)

    def test_front_identity_rejects_changed_integrity_implementation(self):
        screening=load('modules/book_screening/scripts/staged_screening_state.py','code_front_identity')
        for changed in ('staged_screening_state.py','round_integrity.py'):
            with self.subTest(file=changed),tempfile.TemporaryDirectory() as d:
                code=Path(d)/'code';stage=code/'modules/book_screening/scripts/staged_screening_state.py'
                stage.parent.mkdir(parents=True);stage.write_text('stage-v1');(code/'round_integrity.py').write_text('integrity-v1')
                source=code/'input.csv';source.write_text('input')
                with patch.object(screening,'__file__',str(stage)),round_integrity.verification_session():
                    identity=screening.identity([source],{});status=code/'state.json'
                    screening.save_status(status,identity,1,1)
                    target=stage if changed==stage.name else code/changed
                    target.write_text('different-implementation')
                    after=screening.identity([source],{})
                    with self.assertRaisesRegex(ValueError,'model or policy changed'):
                        screening.require_identity(status,after,2)

    def test_cached_hashes_still_reject_added_checkpoint_files(self):
        cleaning=load('modules/dictionary/portable_pipeline.py','incremental_set_clean')
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'passed.json').write_text('{}')
            with round_integrity.verification_session():
                state={'checkpoint_hashes':cleaning.staged_checkpoint_hashes(out)}
                (out/'unexpected.json').write_text('{}')
                with self.assertRaisesRegex(ValueError,'checkpoint changed'):
                    cleaning.verify_staged_checkpoint(out,state)

    def test_front_extraction_and_cleaning_reuse_verified_content_between_checks(self):
        screening=load('modules/book_screening/scripts/staged_screening_state.py','incremental_screen')
        extraction=load('modules/dictionary/src/run_fullbook_v5.py','incremental_extract')
        cleaning=load('modules/dictionary/portable_pipeline.py','incremental_clean')
        for name,fn in [('front',lambda f:screening.checkpoint_hashes([f])),
                        ('extraction',extraction.file_sha256),('cleaning',cleaning.digest)]:
            with self.subTest(stage=name),tempfile.TemporaryDirectory() as d:
                f=Path(d)/'checkpoint.json';f.write_bytes(b'unchanged-passed-result')
                original=Path.open;reads=[]
                def opened(p,*args,**kwargs):
                    mode=args[0] if args else kwargs.get('mode','r')
                    if p.resolve()==f.resolve() and 'b' in mode:reads.append(str(p))
                    return original(p,*args,**kwargs)
                with round_integrity.verification_session(),patch.object(Path,'open',opened):
                    a=fn(f);b=fn(f)
                self.assertEqual(a,b)
                self.assertEqual(len(reads),1,f'{name} reread unchanged checkpoint bytes')

    def test_cohort_reuses_hash_across_native_processes_without_changing_global_rounds(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'cache.json';file.write_bytes(b'passed')
            script="""
import json,sys
from unittest.mock import patch
import round_integrity as m
with patch.object(m,'_read_digest',wraps=m._read_digest) as read:
 digest=m.digest_file(sys.argv[1]);print(json.dumps({'reads':read.call_count,'digest':digest}))
"""
            rounds=[];transitions=[];reads=[]
            def run(book,number):
                result=subprocess.run([sys.executable,'-B','-c',script,str(file)],cwd=ROOT,capture_output=True,text=True,check=True)
                reads.append(json.loads(result.stdout)['reads']);rounds.append(number)
                return {'status':'awaiting_retry' if number==1 else 'ready_to_finalize',
                        'native':{'next_round':2 if number==1 else None}}
            staged_benchmark.cohort_transport_rounds(['book'],run,
                lambda api,limit,phase:transitions.append(limit),'literal-test',[512,128,32],[1,1,1],'test')
            self.assertEqual(rounds,[1,2]);self.assertEqual(transitions,[512,128])
            self.assertEqual(reads,[1,0],'The separate CLI processes lost the cohort verification session')
            self.assertNotIn('PIPELINE_ROUND_INTEGRITY',os.environ)


if __name__=='__main__':unittest.main()
