"""Performance must never turn a changed/cold checkpoint into a trusted one."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    integrity = importlib.import_module('round_integrity')
except ModuleNotFoundError:
    integrity = None


class RoundIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(integrity, 'Missing controller-scoped checkpoint verification cache')

    def child_hash(self, file, force=False):
        # Instrument actual content reads in the native child, not an invented cost model.
        script = '''
import json,sys
from unittest.mock import patch
import round_integrity as m
with patch.object(m, '_read_digest', wraps=m._read_digest) as read:
 value=m.digest_file(sys.argv[1],force_full=sys.argv[2]=='true')
 print(json.dumps({'digest':value,'reads':read.call_count}))
'''
        p=subprocess.run([sys.executable,'-B','-c',script,str(file),str(force).lower()],
                         cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,check=True)
        return json.loads(p.stdout)

    @unittest.skipUnless(os.name=='posix','POSIX production optimization; Windows uses full validation')
    def test_two_native_processes_reuse_only_this_live_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed')
            with integrity.verification_session():
                a=self.child_hash(f);b=self.child_hash(f);final=self.child_hash(f,True)
            self.assertEqual([a['reads'],b['reads'],final['reads']],[1,0,1])
            self.assertEqual(b['digest'],hashlib.sha256(b'passed').hexdigest())
            self.assertEqual(self.child_hash(f)['reads'],1)

    def test_default_and_final_decorator_always_read_content(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'state.json';f.write_bytes(b'original')
            with integrity.verification_session():
                integrity.digest_file(f)
                @integrity.full_verification
                def finalize():return integrity.digest_file(f)
                with patch.object(integrity,'_read_digest',wraps=integrity._read_digest) as reads:
                    self.assertEqual(finalize(),hashlib.sha256(b'original').hexdigest())
                    self.assertEqual(reads.call_count,1)

    def test_same_size_write_with_restored_mtime_is_rehashed(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed');old=f.stat()
            with integrity.verification_session():
                integrity.digest_file(f);f.write_bytes(b'tamper')
                os.utime(f,ns=(old.st_atime_ns,old.st_mtime_ns))
                self.assertEqual(integrity.digest_file(f),hashlib.sha256(b'tamper').hexdigest())

    def test_replaced_inode_and_missing_file_do_not_reuse_hash(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed');old=f.stat()
            with integrity.verification_session():
                integrity.digest_file(f)
                other=Path(d)/'replacement';other.write_bytes(b'tamper')
                os.utime(other,ns=(old.st_atime_ns,old.st_mtime_ns));os.replace(other,f)
                self.assertEqual(integrity.digest_file(f),hashlib.sha256(b'tamper').hexdigest())
                f.unlink()
                with self.assertRaises(FileNotFoundError):integrity.digest_file(f)

    def test_invalid_owner_session_falls_back_to_real_content(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed')
            with patch.dict(os.environ,{'PIPELINE_ROUND_INTEGRITY':'{"pid":-1,"start":"wrong"}'}):
                self.assertEqual(self.child_hash(f)['reads'],1)

    @unittest.skipUnless(os.name=='posix','POSIX session cache only')
    def test_coarse_timestamp_and_reused_pid_identity_force_content_reads(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed')
            whole=int(f.stat().st_mtime)
            os.utime(f,(whole,whole))
            with integrity.verification_session():
                self.assertEqual(self.child_hash(f)['reads'],1)
                self.assertEqual(self.child_hash(f)['reads'],1)
            f.write_bytes(b'passed')
            with integrity.verification_session():
                integrity.digest_file(f)
                token=json.loads(os.environ['PIPELINE_ROUND_INTEGRITY'])
                token['start']='wrong-start-for-this-live-pid'
                with patch.dict(os.environ,{'PIPELINE_ROUND_INTEGRITY':json.dumps(token)}):
                    self.assertEqual(self.child_hash(f)['reads'],1)

    @unittest.skipUnless(os.name=='posix','POSIX owner liveness proof')
    def test_orphaned_index_is_not_reused_after_controller_exit(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed')
            script='''
import os,sys
import round_integrity as m
with m.verification_session():
 m.digest_file(sys.argv[1]);print(os.environ['PIPELINE_ROUND_INTEGRITY'],flush=True)
 os._exit(0)
'''
            child=subprocess.run([sys.executable,'-B','-c',script,str(f)],capture_output=True,text=True,check=True)
            token=json.loads(child.stdout);cache=Path(token['db']).parent
            try:
                self.assertTrue(Path(token['db']).is_file())
                with patch.dict(os.environ,{'PIPELINE_ROUND_INTEGRITY':json.dumps(token)}):
                    self.assertEqual(self.child_hash(f)['reads'],1)
            finally:
                self.assertEqual(cache.resolve().parent,Path('/tmp').resolve())
                self.assertTrue(cache.name.startswith('pipeline-round-integrity-'))
                shutil.rmtree(cache)

    @unittest.skipUnless(os.name=='posix','POSIX session cache only')
    def test_corrupt_index_row_cannot_forge_a_passed_hash(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'cache.json';f.write_bytes(b'passed')
            with integrity.verification_session():
                integrity.digest_file(f)
                token=json.loads(os.environ['PIPELINE_ROUND_INTEGRITY'])
                with sqlite3.connect(token['db']) as db:
                    db.execute("UPDATE digests SET digest=?,signature=?",('0'*64,'forged'))
                self.assertEqual(self.child_hash(f),{'digest':hashlib.sha256(b'passed').hexdigest(),'reads':1})


if __name__=='__main__':unittest.main()
