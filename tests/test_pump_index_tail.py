import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from economic_machine.pump_research.address_index import AddressIndex,search
from economic_machine.pump_research.index_tail import consume
from economic_machine.pump_research.store import encode

class TailTests(unittest.TestCase):
    def test_separate_origin_idempotent_replay_and_unknown_blocktime(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'data/result-archive').mkdir(parents=True)
            (root/'data/result-archive/durable-tail.json').write_text(json.dumps(dict(segment='00000007',offset=10)))
            key=bytes([1])*32;wallet=encode(key)
            tx=dict(signature=encode(bytes([2])*64),slot=300000000,index=0,success=True,transfers=[],activity=[],launches=[])
            info=NS(is_vote=False,transaction=NS(message=NS(account_keys=[key])),meta=NS(loaded_writable_addresses=[],loaded_readonly_addresses=[]))
            update=NS(WhichOneof=lambda _: 'transaction',transaction=NS(transaction=info,slot=300000000))
            pb=NS(SubscribeUpdate=NS(FromString=lambda _:update),SLOT_DEAD=6)
            reader=NS(frame=lambda *_:(b'public-frame',1,999,10),size=lambda _:10)
            scope=NS(guard=lambda *args,**kw:None)
            index=AddressIndex(root/'index.sqlite3')
            try:
                with patch('economic_machine.pump_research.index_tail.from_geyser',return_value=tx):
                    c=consume(index,reader,pb,root,None,scope);c2=consume(index,reader,pb,root,None,scope)
                self.assertEqual(c['origin'],dict(segment='00000007',offset=0))
                self.assertFalse(c['history_before_origin_indexed']);self.assertTrue(c['caught_up_to_sampled_head'])
                self.assertEqual(c['transactions'],1);self.assertEqual(c2['transactions'],0)
                r=search(root/'index.sqlite3',wallet);self.assertEqual(len(r['transactions']),1)
                self.assertIsNone(r['transactions'][0]['stamp']);self.assertEqual(r['transactions'][0]['source'],'ticking_retained')
                self.assertFalse(r['transactions'][0]['signature_verification'])
            finally:index.close()

if __name__=='__main__':unittest.main()
