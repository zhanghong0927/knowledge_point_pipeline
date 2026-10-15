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

    def add_test(self):
        path=self.module/'tests/integration_test.py'
        path.parent.mkdir()
        path.write_bytes(b'approved integration test\n')
        return path

    def approve_addition(self,path):
        self.baseline['integration_additions']={'files':{
            path.relative_to(self.module).as_posix():digest(path.read_bytes())}}

    def test_undeclared_added_source_is_reported(self):
        self.approve_a()
        (self.module/'unexpected.py').write_bytes(b'undeclared source\n')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['unexpected.py'])

    def test_registered_integration_test_passes_without_changing_original_map(self):
        self.approve_a()
        original=self.baseline['sha256'].copy()
        self.approve_addition(self.add_test())
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),[])
        self.assertEqual(self.baseline['sha256'],original)

    def test_changed_or_missing_registered_addition_is_reported(self):
        self.approve_a()
        path=self.add_test()
        self.approve_addition(path)
        path.write_bytes(b'unapproved replacement\n')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['tests/integration_test.py'])
        path.unlink()
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['tests/integration_test.py'])

    def test_missing_original_file_is_reported(self):
        self.approve_a()
        (self.module/'b.py').unlink()
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['b.py'])

    def test_addition_cannot_replace_original_pin_or_escape_module(self):
        self.approve_a()
        for name in ('a.py','../outside.py','/absolute.py','tests\\outside.py'):
            with self.subTest(name=name):
                self.baseline['integration_additions']={'files':{name:'0'*64}}
                with self.assertRaises(ValueError):important_llm_core_changes(self.root,self.baseline)

    def test_malformed_additions_are_rejected(self):
        self.approve_a()
        for files in ([],{'test.py':'invalid'}):
            with self.subTest(files=files):
                self.baseline['integration_additions']={'files':files}
                with self.assertRaises(ValueError):important_llm_core_changes(self.root,self.baseline)

    def test_local_caches_and_environment_are_excluded_but_not_source(self):
        self.approve_a()
        for name in ('__pycache__/a.cpython-312.pyc','.venv/lib/site.py',
                     '.pytest_cache/v/cache/nodeids','outputs/result.json','.llm_services.json'):
            path=self.module/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'local runtime file')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),[])
        (self.module/'added_source.py').write_bytes(b'undeclared source\n')
        self.assertEqual(important_llm_core_changes(self.root,self.baseline),['added_source.py'])


if __name__=='__main__':unittest.main()
