import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.store import atomic


def write_many(path, writer):
    for n in range(40):
        atomic(path, {'writer':writer, 'sequence':n, 'body':str(writer)*12000})


class AtomicSnapshotTests(unittest.TestCase):
    def test_processes_never_share_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'
            ctx=multiprocessing.get_context('spawn')
            workers=[ctx.Process(target=write_many,args=(path,n)) for n in range(8)]
            for worker in workers:worker.start()
            for worker in workers:
                worker.join(30)
                self.assertEqual(worker.exitcode,0)
            value=json.loads(path.read_text())
            self.assertEqual(value['sequence'],39)
            self.assertEqual(value['body'],str(value['writer'])*12000)
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_failed_replace_keeps_previous_and_cleans_own_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json';atomic(path,{'old':True})
            with patch('economic_machine.pump_research.store.os.replace',side_effect=OSError('test')):
                with self.assertRaises(OSError):atomic(path,{'new':True})
            self.assertEqual(json.loads(path.read_text()),{'old':True})
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_invalid_json_does_not_create_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):atomic(Path(folder)/'status.json',{'x':float('nan')})
            self.assertFalse(list(Path(folder).iterdir()))
