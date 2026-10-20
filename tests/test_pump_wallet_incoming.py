import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from economic_machine.pump_research.address_index import AddressIndex
from economic_machine.pump_research.wallet_map import Graph
from economic_machine.pump_research.store import encode, canonical


def key(n): return encode(bytes([n]) * 32)


class IncomingWalletTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); (self.base / 'evidence').mkdir()
        self.graph = Graph(self.base); self.addCleanup(self.graph.close)
        self.index = AddressIndex(self.graph.out / 'address-index.sqlite3'); self.addCleanup(self.index.close)
        self.scope = SimpleNamespace(guard=lambda: None)

    def ingest(self, n, source, destination):
        tx = dict(signature=encode(bytes([n]) * 64), slot=250000000+n, index=0, success=True,
            account_keys=list({source, destination}), transfers=[dict(ix=0,source=source,
            destination=destination,mint='SOL',amount='123',kind='system_transfer')], activity=[], launches=[])
        self.index.ingest(dict(slot=tx['slot'],time=1706000000+n,transactions=[tx],
            transaction_nodes=1,decode_errors=[]), dict(car_sha256=str(n)*64))

    def job(self, wallet_root=True):
        root=key(1) if wallet_root else key(9)
        with self.graph.db:
            self.graph.db.execute('INSERT OR IGNORE INTO roots VALUES(?,?)',
                (root,canonical(dict(mint=root,root_kind='wallet' if wallet_root else 'token'))))
            ident=self.graph.enqueue(root,key(1),0,None,0,'indexed_forward')
        return dict(self.graph.db.execute('SELECT * FROM frontier WHERE id=?',(ident,)).fetchone())

    def test_wallet_report_includes_both_directions_without_changing_endpoints(self):
        self.ingest(1,key(1),key(2));self.ingest(2,key(3),key(1));self.ingest(3,key(1),key(1))
        result=self.graph.indexed_forward(self.job(),self.scope)
        self.assertEqual(result['indexed_transfers'],3);self.assertEqual(result['indexed_incoming'],1)
        pairs={tuple(r) for r in self.graph.db.execute("SELECT source,target FROM edges WHERE kind='indexed_transfer'")}
        self.assertEqual(pairs,{(key(1),key(2)),(key(3),key(1)),(key(1),key(1))})
        self.assertFalse(self.graph.db.execute("SELECT 1 FROM frontier WHERE wallet=? AND source='local_forward'",(key(3),)).fetchone())

    def test_old_outgoing_cursor_does_not_skip_older_incoming_records(self):
        self.ingest(1,key(3),key(1));self.ingest(2,key(1),key(2))
        job=self.job()
        with self.graph.db:self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical({'transfer':2}),job['id']))
        job=dict(self.graph.db.execute('SELECT * FROM frontier WHERE id=?',(job['id'],)).fetchone())
        first=self.graph.indexed_forward(job,self.scope)
        self.assertEqual(first['indexed_transfers'],1);self.assertEqual(first['indexed_incoming'],1)
        next_job=dict(self.graph.db.execute('SELECT * FROM frontier WHERE id=?',(job['id'],)).fetchone())
        self.assertEqual(json.loads(next_job['cursor'])['transfer'],2)
        self.assertEqual(self.graph.indexed_forward(next_job,self.scope)['indexed_transfers'],0)

    def test_launch_participant_forward_tracing_keeps_original_direction(self):
        self.ingest(1,key(3),key(1));self.ingest(2,key(1),key(2))
        result=self.graph.indexed_forward(self.job(False),self.scope)
        self.assertEqual(result['indexed_transfers'],1);self.assertEqual(result['indexed_incoming'],0)


if __name__ == '__main__': unittest.main()
