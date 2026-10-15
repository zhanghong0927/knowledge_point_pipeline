"""Approved source updates must not disable native-module integrity checks."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from validate_offline import important_llm_core_changes


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ModuleOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.module=self.root/'modules/important_llm'
        self.module.mkdir(parents=True)
        (self.module/'a.py').write_bytes(b'approved current version\n')
        (self.module/'b.py').write_bytes(b'original unchanged file\n')
        self.baseline={'sha256':{'a.py':digest(b'original version\n'),
                                 'b.py':digest(b'original unchanged file\n')}}

    def approve_a(self):
        self.baseline['integration_overrides']={'files':{'a.py':{
            'original_sha256':self.baseline['sha256']['a.py'],
            'current_sha256':digest((self.module/'a.py').read_bytes())}}}

    def test_undeclared_change_is_reported(self):
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['a.py'])

    def test_declared_update_accepts_exact_bytes_without_replacing_original(self):
        original=self.baseline['sha256'].copy()
        self.approve_a()
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),[])
        self.assertEqual(self.baseline['sha256'],original)

    def test_other_file_tampering_is_reported(self):
        self.approve_a()
        (self.module/'b.py').write_bytes(b'changed outside approved update\n')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['b.py'])

    def test_declared_file_tampering_is_reported(self):
        self.approve_a()
        (self.module/'a.py').write_bytes(b'different current bytes\n')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['a.py'])

    def test_wrong_original_identity_is_rejected(self):
        self.approve_a()
        self.baseline['integration_overrides']['files']['a.py']['original_sha256']='0'*64
        with self.assertRaises(ValueError):important_llm_core_changes(self.root,self.baseline)

    def test_unknown_file_override_is_rejected(self):
        self.approve_a()
        self.baseline['integration_overrides']['files']['unknown.py']={
            'original_sha256':'0'*64,'current_sha256':'1'*64}
        with self.assertRaises(ValueError):important_llm_core_changes(self.root,self.baseline)

    def test_invalid_new_digest_is_rejected(self):
        self.approve_a()
        self.baseline['integration_overrides']['files']['a.py']['current_sha256']='invalid'
        with self.assertRaises(ValueError):important_llm_core_changes(self.root,self.baseline)


if __name__=='__main__':unittest.main()
