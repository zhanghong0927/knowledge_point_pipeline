"""A new round must not reread its just-appended journal to seal its prefix."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_mounting_bridge import fixture, bridge
from test_mounting_staged_rounds import native_module
import round_integrity


class MountingRoundIOTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name=='posix','Warm controller session on POSIX')
    def test_bridge_finalization_reads_original_source_not_only_native_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));native=native_module();args.only_round=1;args.finalize_only=False
            def run_native(command,check=True,env=None):
                with patch.object(sys,'argv',command[1:]),patch.object(native,'route_record',return_value=({'status':'ok','decision':'needs_review','token_usage':{}},native.TokenUsage())):
                    native.main()
            with round_integrity.verification_session(),patch.object(bridge.subprocess,'run',side_effect=run_native):
                bridge.route(args);args.only_round=None;args.finalize_only=True
                with patch.object(round_integrity,'_read_digest',wraps=round_integrity._read_digest) as reads:
                    bridge.route(args)
                self.assertEqual(sum(Path(c.args[0])==args.input.resolve() for c in reads.call_args_list),1,
                                 'Bridge-only original input was not fully read during finalize')

    def test_final_native_entry_reads_request_cache_even_inside_live_session(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));target=args.out/'groups/mechanical_engineering'
            native=native_module();output=target/'routed.jsonl'
            argv=['native','--input',str(target/'input.jsonl'),'--output',str(output),
                  '--profile-dir',str(target/'profile'),'--model','literal-fake',
                  '--api-url','http://127.0.0.1:9/v1/chat/completions','--skip-preflight']
            def result(record,root,options,prompt):
                (options.staged_request_cache/'passed.json').write_text('{"value":"passed"}')
                return {'status':'ok','decision':'needs_review','token_usage':{}},native.TokenUsage()
            with round_integrity.verification_session():
                with patch.object(sys,'argv',argv+['--only-round','1']),patch.object(native,'route_record',side_effect=result):
                    native.main()
                reads=[];original=round_integrity._read_digest
                def observe(path):
                    if path.name=='passed.json':reads.append(str(path))
                    return original(path)
                with patch.object(sys,'argv',argv+['--finalize-only']),patch.object(round_integrity,'_read_digest',side_effect=observe):
                    native.main()
                self.assertEqual(len(reads),1,'Final native audit reused a warm request-cache digest')

    def test_append_updates_prefix_digest_without_reading_the_journal_again(self):
        with tempfile.TemporaryDirectory() as d:
            args,_=fixture(Path(d));target=args.out/'groups/mechanical_engineering'
            native=native_module();output=target/'routed.jsonl';events=output.with_suffix('.rounds.jsonl')
            argv=['native','--input',str(target/'input.jsonl'),'--output',str(output),
                  '--profile-dir',str(target/'profile'),'--model','literal-fake',
                  '--api-url','http://127.0.0.1:9/v1/chat/completions','--skip-preflight']
            original=Path.open;opened=[]
            def open_file(p,*a,**kw):
                mode=a[0] if a else kw.get('mode','r')
                if p==events and 'r' in mode:opened.append(mode)
                return original(p,*a,**kw)
            with patch.object(sys,'argv',argv+['--only-round','1']),patch.object(native,'route_record',side_effect=TimeoutError('timed out')),patch.object(Path,'open',open_file):
                native.main()
            self.assertEqual(opened,[],'Round 1 reread all newly committed events to recompute SHA')
            data=events.read_bytes();state=json.loads(output.with_suffix('.rounds.json').read_text())
            self.assertEqual(state['completed_prefix_sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(state['completed_prefix_bytes'],len(data))
            opened.clear()
            answer={'status':'ok','decision':'needs_review','token_usage':{}}
            with patch.object(sys,'argv',argv+['--only-round','2']),patch.object(native,'route_record',return_value=(answer,native.TokenUsage())),patch.object(Path,'open',open_file):
                native.main()
            self.assertEqual(len(opened),1,'Round 2 must stream the old history once, not reread it at the end')
            data=events.read_bytes();state=json.loads(output.with_suffix('.rounds.json').read_text())
            self.assertEqual(state['completed_prefix_sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(state['pending_transport'],0)
            with patch.object(sys,'argv',argv+['--finalize-only']),patch.object(native,'route_record',side_effect=AssertionError('Finalization called a model')):
                native.main()
            self.assertEqual(len(output.read_text('utf-8').splitlines()),3)


if __name__=='__main__':unittest.main()
