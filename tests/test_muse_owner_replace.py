import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/'products/pacifica_muse/owner_replace.py'
spec=importlib.util.spec_from_file_location('owner_replace',PATH);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'control').mkdir()
        self.lease={'live':True,'session_id':'fixed','start_ms':1000,'deadline_ms':172801000}
        (self.root/'session-start.json').write_text(json.dumps(self.lease))
        self.db=sqlite3.connect(self.root/'control/model.sqlite')
        self.db.executescript('CREATE TABLE scope(id INTEGER,body TEXT);CREATE TABLE calls(id INTEGER);')
        self.budget={'session_id':'fixed','deadline_ms':172801000,'maximum_usd':'unlimited','max_calls':720}
        self.db.execute('INSERT INTO scope VALUES(1,?)',(json.dumps(self.budget),));self.db.commit()

    def tearDown(self):self.db.close();self.tmp.cleanup()

    def test_only_exact_current_session_processes_selected(self):
        root=str(module.LIVE)
        owner=['python3','products/pacifica_muse/owner_start.py','--live','--root',root]
        self.assertEqual(module.classify(owner),'supervisor')
        self.assertIsNone(module.classify(owner[:-1]+[root+'-other']))
        self.assertIsNone(module.classify(['bash','-c',' '.join(owner)]))
        self.assertIsNone(module.classify(owner+['--root',root]))
        self.assertEqual(module.classify(['python3','-m','economic_machine.muse_policy_worker','--root',root]),'worker')
        self.assertEqual(module.classify(['/release/bin/muse-maker','--live',root,root+'/control','5','/socket']),'native')
        self.assertIsNone(module.classify(['/release/bin/muse-maker','--observe',root,root+'/control','45']))

    def test_original_time_and_ledger_not_reopened(self):
        before=module.scope(self.root,2000);self.assertEqual(before['max_calls'],720)
        with self.assertRaises(ValueError):module.scope(self.root,self.lease['deadline_ms'])
        self.db.execute('UPDATE scope SET body=?',(json.dumps(dict(self.budget,session_id='other')),));self.db.commit()
        with self.assertRaises(ValueError):module.scope(self.root,2000)

    def test_exhausted_calls_not_reset(self):
        self.db.executemany('INSERT INTO calls VALUES(?)',[(i,) for i in range(720)]);self.db.commit()
        with self.assertRaises(ValueError):module.scope(self.root,2000)

    def test_release_integrity_and_scope(self):
        r=self.root/'release';(r/'evidence').mkdir(parents=True)
        names=['bin/muse-maker','source/products/pacifica_muse/owner_start.py','source/products/pacifica_muse/owner_replace.py',
               'source/src/economic_machine/muse_maker.py','source/src/economic_machine/muse_policy_worker.py']
        hashes={}
        for name in names:
            p=r/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture');hashes[name]=hashlib.sha256(b'fixture').hexdigest()
        data={'per_side_usd':600,'aggregate_usd':1200,'ttl_ms':60000,'sha256':hashes}
        (r/'evidence/release.json').write_text(json.dumps(data));module.verify_release(r)
        (r/'bin/muse-maker').write_bytes(b'changed')
        with self.assertRaises(ValueError):module.verify_release(r)

    def test_check_cannot_signal_or_start(self):
        with patch.object(module.sys,'argv',['owner_replace.py','--check']),patch.object(module.sys,'platform','linux'),\
             patch.object(module,'verify_release',return_value={}),patch.object(module,'scope',return_value={}),\
             patch.object(module,'processes',return_value=[]),patch.object(module.os,'pidfd_open') as opened,\
             patch.object(module.os,'execv') as execute,patch.object(module.signal,'pidfd_send_signal') as signal:
            module.main();opened.assert_not_called();execute.assert_not_called();signal.assert_not_called()

    def test_apply_requires_human_terminal(self):
        with patch.object(module.sys,'argv',['owner_replace.py','--apply']),patch.object(module.sys,'platform','linux'),\
             patch.object(module.sys.stdin,'isatty',return_value=False),patch.object(module.os,'execv') as execute:
            with self.assertRaises(SystemExit):module.main()
            execute.assert_not_called()

    def test_orphan_or_multiple_supervisors_do_not_get_killed(self):
        for found in ([{'pid':1,'kind':'native'}],[{'pid':1,'kind':'supervisor'},{'pid':2,'kind':'supervisor'}]):
            with patch.object(module.sys,'argv',['owner_replace.py','--check']),patch.object(module.sys,'platform','linux'),\
                 patch.object(module,'verify_release',return_value={}),patch.object(module,'scope',return_value={}),\
                 patch.object(module,'processes',return_value=found),patch.object(module.signal,'pidfd_send_signal') as signal:
                with self.assertRaises(ValueError):module.main()
                signal.assert_not_called()

    def test_mock_replacement_preserves_same_root_and_budget(self):
        scope=module.scope(self.root,2000)
        with patch.object(module.sys,'argv',['owner_replace.py','--apply']),patch.object(module.sys,'platform','linux'),\
             patch.object(module.sys.stdin,'isatty',return_value=True),patch.object(module,'LIVE',self.root),\
             patch.object(module,'verify_release',return_value={}),patch.object(module,'scope',return_value=scope),\
             patch.object(module,'processes',return_value=[]),patch.object(module.os,'chdir'),patch.object(module.os,'execv') as execute:
            module.main();argv=execute.call_args.args[1]
            self.assertEqual(argv[argv.index('--root')+1],str(self.root))
            self.assertEqual(argv[argv.index('--model-budget-usd')+1],'unlimited')
            self.assertIn('--agent-encrypted-file',argv)


if __name__=='__main__':unittest.main()
