from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from economic_machine.arbitrum_research.graph import Graph, TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH


def addr(n):
    return "0x" + f"{n:040x}"


def h(n):
    return "0x" + f"{n:064x}"


def data(*nums):
    return "0x" + "".join(f"{n:064x}" for n in nums)


def tx(i=0, status=1, value=13, to=2):
    return {"transactionIndex": i, "hash": h(100 + i), "from": addr(1),
            "to": addr(to) if to is not None else None, "value": str(value), "status": status}


def trace(path=(), amount=13, to=2, sender=1, kind="call", error=None, call_type="call"):
    result = {"transactionIndex": 0, "traceAddress": list(path), "type": kind,
              "error": error, "action": {"from": addr(sender), "to": addr(to),
              "value": str(amount), "callType": call_type}, "result": {}}
    return result


def block(number=0, transactions=None, traces=None, logs=None):
    return {"header": {"number": number, "hash": h(number + 1), "parentHash": h(number)},
            "transactions": [tx()] if transactions is None else transactions,
            "traces": [trace()] if traces is None else traces, "logs": logs or []}


def log(topics, payload, i=0):
    return {"transactionIndex": 0, "logIndex": i, "address": addr(9), "topics": topics, "data": payload}


def keccak256(raw):
    # Independent small Keccak-f reference for event-signature golden tests.
    mask = (1 << 64) - 1
    rotations = ((0,36,3,41,18),(1,44,10,45,2),(62,6,43,15,61),(28,55,25,21,56),(27,20,39,8,14))
    constants = (0x1,0x8082,0x800000000000808a,0x8000000080008000,0x808b,0x80000001,
                 0x8000000080008081,0x8000000000008009,0x8a,0x88,0x80008009,0x8000000a,
                 0x8000808b,0x800000000000008b,0x8000000000008089,0x8000000000008003,
                 0x8000000000008002,0x8000000000000080,0x800a,0x800000008000000a,
                 0x8000000080008081,0x8000000000008080,0x80000001,0x8000000080008008)
    def rot(v, n):
        return ((v << n) | (v >> ((64 - n) % 64))) & mask
    padded = bytearray(raw); padded.append(1)
    padded.extend(bytes((-len(padded)) % 136)); padded[-1] |= 128
    a = [0] * 25
    for offset in range(0, len(padded), 136):
        for i in range(17):
            a[i] ^= int.from_bytes(padded[offset + i*8:offset + (i+1)*8], "little")
        for rc in constants:
            c = [a[x] ^ a[x+5] ^ a[x+10] ^ a[x+15] ^ a[x+20] for x in range(5)]
            d = [c[(x-1)%5] ^ rot(c[(x+1)%5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    a[x+5*y] ^= d[x]
            b = [0] * 25
            for x in range(5):
                for y in range(5):
                    b[y+5*((2*x+3*y)%5)] = rot(a[x+5*y], rotations[x][y])
            for x in range(5):
                for y in range(5):
                    a[x+5*y] = b[x+5*y] ^ ((~b[(x+1)%5+5*y]) & b[(x+2)%5+5*y])
            a[0] ^= rc
    return b"".join(v.to_bytes(8, "little") for v in a)[:32].hex()


class OccurrenceGraphTest(unittest.TestCase):
    def graph(self,root,occurrences=True):
        graph=Graph(Path(root)/'graph.sqlite3',transaction_occurrences=occurrences);self.addCleanup(graph.close);return graph

    def test_same_hash_success_failure_both_visible_only_success_value_moves(self):
        with tempfile.TemporaryDirectory() as root:
            g=self.graph(root);blocks=[block(0,transactions=[tx(status=1)],traces=[]),block(1,transactions=[tx(status=0)],traces=[])]
            g.ingest(blocks,'a'*64,0,1,{'blob':'a'})
            self.assertEqual([tuple(r) for r in g.db.execute('select block,tx_index,status from transactions where hash=? order by block',(h(100),))],[(0,0,1),(1,0,0)])
            self.assertEqual([tuple(r) for r in g.db.execute('select block,amount from edges')],[(0,'13')])
            self.assertEqual(g.snapshot()['counts']['transactions'],2)
            self.assertEqual(g.snapshot()['anomaly_counts']['REPEATED_TRANSACTION_HASH'],1)
            self.assertEqual([x['status'] for x in g.wallet(addr(1))['interactions']],[0,1])
            self.assertEqual(g.identity['schema'],2)

    def test_two_success_occurrences_not_merged(self):
        with tempfile.TemporaryDirectory() as root:
            g=self.graph(root);g.ingest([block(0,traces=[]),block(1,traces=[])],'b'*64,0,1,{'blob':'b'})
            self.assertEqual(g.snapshot()['counts']['transactions'],2);self.assertEqual(g.snapshot()['counts']['edges'],2)

    def test_duplicate_block_index_still_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            g=self.graph(root)
            with self.assertRaisesRegex(ValueError,'duplicate transaction index'):
                g.ingest([block(0,transactions=[tx(),tx()],traces=[])],'c'*64,0,0,{'blob':'c'})
            self.assertEqual(g.snapshot()['counts']['blocks'],0)

    def test_legacy_identity_replay_stays_schema1(self):
        with tempfile.TemporaryDirectory() as root:
            g=self.graph(root,False);b=[block(0)];g.ingest(b,'d'*64,0,0,{'blob':'d'})
            self.assertTrue(g.ingest(b,'d'*64,0,0,{'blob':'d'})['idempotent']);self.assertEqual(g.identity['schema'],1)
            with self.assertRaisesRegex(ValueError,'identity mismatch'):Graph(Path(root)/'graph.sqlite3',transaction_occurrences=True)

    def test_trace_of_other_occurrence_does_not_fill_missing_trace(self):
        with tempfile.TemporaryDirectory() as root:
            g=self.graph(root);g.ingest([block(0),block(1,traces=[])],'e'*64,0,1,{'blob':'e'})
            self.assertEqual(g.snapshot()['transactions_without_trace_rows'],1)


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = Graph(Path(self.tmp.name) / "graph.sqlite")

    def tearDown(self):
        self.g.close()
        self.tmp.cleanup()

    def ingest(self, blocks=None, sha=None):
        blocks = blocks or [block()]
        sha = sha or hashlib.sha256(json.dumps(blocks, sort_keys=True).encode()).hexdigest()
        return self.g.ingest(blocks, sha, blocks[0]["header"]["number"], blocks[-1]["header"]["number"], {"blob": sha})

    def edges(self):
        return [dict(x) for x in self.g.db.execute("SELECT * FROM edges ORDER BY id")]

    def test_event_hashes_independent_keccak_and_empty_golden(self):
        self.assertEqual(keccak256(b""), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        for signature, expected in (
            ("Transfer(address,address,uint256)", TRANSFER),
            ("TransferSingle(address,address,address,uint256,uint256)", TRANSFER_SINGLE),
            ("TransferBatch(address,address,address,uint256[],uint256[])", TRANSFER_BATCH)):
            self.assertEqual("0x" + keccak256(signature.encode()), expected)

    def test_native_root_not_double_counted_and_large_integer_exact(self):
        big = (1 << 255) + 117
        self.ingest([block(transactions=[tx(value=big)], traces=[trace(amount=big)])])
        self.assertEqual([e["amount"] for e in self.edges()], [str(big)])
        self.assertEqual(self.edges()[0]["chain_id"], 42161)

    def test_disputed_source_sender_keeps_record_without_financial_edges(self):
        value=tx();value['sender_conflict']=True
        event=log([TRANSFER,h(1),h(2)],data(9))
        self.ingest([block(transactions=[value],logs=[event])])
        self.assertEqual(self.edges(),[])
        self.assertEqual(self.g.db.execute('select count(*) from transactions').fetchone()[0],1)
        self.assertEqual(self.g.db.execute("select count(*) from anomalies where code='SOURCE_SENDER_CONFLICT'").fetchone()[0],1)
        self.assertTrue(self.g.wallet(addr(1))['interactions'])

    def test_internal_call_and_nested_revert(self):
        self.ingest([block(traces=[trace(), trace((0,), amount=7, to=3),
                                  trace((1,), amount=8, to=4, error="Reverted"),
                                  trace((1, 0), amount=9, to=5)])])
        self.assertEqual(sorted(e["amount"] for e in self.edges()), ["13", "7"])
        self.assertEqual(self.g.snapshot()["trace_count"], 4)

    def test_failed_receipt_never_financial_even_success_looking_child(self):
        self.ingest([block(transactions=[tx(status=0)], traces=[trace(), trace((0,))],
                          logs=[log([TRANSFER, h(1), h(2)], data(100))])])
        self.assertEqual(self.edges(), [])
        self.assertGreater(self.g.snapshot()["counts"]["interactions"], 0)
        self.assertIn("FAILED_TX_LOG", self.g.snapshot()["anomaly_counts"])

    def test_unknown_receipt_never_financial(self):
        t = tx(); del t["status"]
        self.ingest([block(transactions=[t])])
        self.assertEqual(self.edges(), [])
        self.assertEqual(self.g.snapshot()["receipt_status_unknown_count"], 1)

    def test_failed_root_conflicts_with_success_receipt(self):
        self.ingest([block(traces=[trace(error="execution reverted"), trace((0,), amount=5)])])
        self.assertEqual(self.edges(), [])
        self.assertEqual(self.g.snapshot()["anomaly_counts"]["RECEIPT_TRACE_CONFLICT"], 1)

    def test_missing_ancestor_does_not_invent_trace_success(self):
        self.ingest([block(traces=[trace(), trace((1, 0), amount=5)])])
        self.assertEqual(len(self.edges()), 1)
        self.assertEqual(self.g.snapshot()["anomaly_counts"]["TRACE_ANCESTOR_MISSING"], 1)

    def test_delegate_callcode_staticcall_not_transfers(self):
        self.ingest([block(traces=[trace()] + [trace((i,), amount=9, call_type=k) for i, k in enumerate(("delegatecall", "callcode", "staticcall"))])])
        self.assertEqual(len(self.edges()), 1)

    def test_null_value_static_delegate_preserve_call_interactions(self):
        child = trace((0,), call_type="staticcall"); child["action"]["value"] = None
        nested = trace((0, 0), call_type="delegatecall"); nested["action"]["value"] = None
        self.ingest([block(traces=[trace(), child, nested])])
        self.assertEqual(len(self.edges()), 1)
        self.assertEqual(self.g.snapshot()["anomaly_counts"], {})
        self.assertEqual(self.g.snapshot()["counts"]["interactions"], 4)

    def test_system_call_not_silently_assumed_native_transfer(self):
        self.ingest([block(traces=[trace(), trace((0,), amount=9, call_type="invalid")])])
        self.assertEqual(len(self.edges()), 1)
        self.assertIn("SYSTEM_TRANSFER_UNCLASSIFIED", self.g.snapshot()["anomaly_counts"])

    def test_create_and_selfdestruct_edges(self):
        creation = trace((0,), amount=7, kind="create")
        creation["result"] = {"address": addr(3)}
        suicide = {"transactionIndex": 0, "traceAddress": [1], "type": "suicide",
                   "action": {"address": addr(3), "refundAddress": addr(4), "balance": "21"}}
        self.ingest([block(traces=[trace(), creation, suicide])])
        self.assertEqual({e["kind"] for e in self.edges()}, {"native_transaction", "native_create", "native_suicide"})

    def test_root_contract_creation_is_once(self):
        creation = trace(kind="create")
        creation["result"] = {"address": addr(8)}
        self.ingest([block(transactions=[tx(to=None)], traces=[creation])])
        self.assertEqual([(e["recipient"], e["amount"]) for e in self.edges()], [(addr(8), "13")])

    def test_receipt_creation_survives_missing_traces_without_double_count(self):
        creation = trace(kind="create")
        creation["result"] = {"address": addr(8)}
        t = tx(to=None); t["contractAddress"] = addr(8)
        self.ingest([block(transactions=[t], traces=[creation])])
        self.assertEqual([(e["recipient"], e["amount"]) for e in self.edges()], [(addr(8), "13")])
        self.assertEqual(self.edges()[0]["kind"], "native_contract_creation")

    def test_erc20_erc721_separate_and_operator_not_owner(self):
        self.ingest([block(transactions=[tx(value=0)], traces=[trace(amount=0)], logs=[
            log([TRANSFER, h(1), h(2)], data((1 << 255) + 7)),
            log([TRANSFER, h(1), h(2), h(33)], "0x", 1),
            log([TRANSFER_SINGLE, h(8), h(1), h(2)], data(2, 9), 2)])])
        edges = self.edges()
        self.assertEqual([e["kind"] for e in edges], ["erc20_transfer", "erc721_transfer", "erc1155_transfer"])
        self.assertEqual([e["sender"] for e in edges], [addr(1)] * 3)
        self.assertEqual(edges[1]["token_id"], "33")

    def test_erc1155_batch_exact_per_token(self):
        self.ingest([block(transactions=[tx(value=0)], traces=[], logs=[
            log([TRANSFER_BATCH, h(8), h(1), h(2)], data(64, 160, 2, 11, 12, 2, 8, 9))])])
        self.assertEqual([(e["token_id"], e["amount"]) for e in self.edges()], [("11", "8"), ("12", "9")])

    def test_erc1155_batch_overlap_or_overflow_rejected_without_money_edge(self):
        for i, payload in enumerate((data(64, 64, 1, 7), data(64, 160, 2**255, 1, 1, 1))):
            b = block(transactions=[tx(value=0)], traces=[], logs=[log([TRANSFER_BATCH, h(8), h(1), h(2)], payload, i)])
            # A separate temporary shard isolates each malformed input.
            g = Graph(Path(self.tmp.name) / f"malformed{i}.sqlite")
            try:
                g.ingest([b], f"{i+1:064x}", 0, 0, "ref")
                self.assertEqual(g.snapshot()["counts"]["edges"], 0)
                self.assertEqual(g.snapshot()["anomaly_counts"]["UNDECODED_TRANSFER_EVENT"], 1)
            finally:
                g.close()

    def test_ambiguous_transfer_and_removed_log_not_money(self):
        removed = log([TRANSFER, h(1), h(2)], data(7), 1); removed["removed"] = True
        self.ingest([block(transactions=[tx(value=0)], traces=[], logs=[
            log([TRANSFER, h(1), h(2), h(3)], data(7)), removed])])
        self.assertEqual(self.edges(), [])
        self.assertEqual(self.g.snapshot()["log_count"], 2)

    def test_unknown_protocol_logs_and_all_interactions_retained(self):
        self.ingest([block(logs=[log([h(9999)], "0x1234")])])
        self.assertEqual(self.g.snapshot()["log_count"], 1)
        self.assertEqual(self.g.wallet(addr(9))["interactions"][0]["kind"], "log_emitter")

    def test_atomic_page_on_invalid_parent_or_receipt_reference(self):
        second = block(1, transactions=[], traces=[]); second["header"]["parentHash"] = h(8)
        with self.assertRaisesRegex(ValueError, "parent hash"):
            self.ingest([block(), second])
        self.assertEqual(self.g.snapshot()["counts"]["blocks"], 0)
        invalid = block(); invalid["traces"][0]["transactionHash"] = h(999)
        with self.assertRaisesRegex(ValueError, "transaction hash"):
            self.ingest([invalid])
        self.assertEqual(self.g.snapshot()["counts"]["pages"], 0)

    def test_idempotent_replay_and_source_conflicts(self):
        b = [block()]
        self.assertFalse(self.ingest(b)["idempotent"])
        self.assertTrue(self.ingest(b)["idempotent"])
        self.assertEqual(len(self.edges()), 1)
        with self.assertRaisesRegex(ValueError, "gap or overlap"):
            self.ingest(b, "a" * 64)
        sha = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
        changed = copy.deepcopy(b); changed[0]["transactions"][0]["value"] = "17"
        with self.assertRaisesRegex(ValueError, "provenance conflict"):
            self.ingest(changed, sha)

    def test_sharding_pins_previous_hash_and_scope(self):
        self.ingest()
        snapshot = self.g.checkpoint()
        g = Graph(Path(self.tmp.name) / "next.sqlite", initial_start=1, previous_block_hash=snapshot["tip_hash"])
        try:
            g.ingest([block(1, transactions=[], traces=[])], "b" * 64, 1, 1, "ref")
            self.assertEqual(g.snapshot()["covered_first_block"], 1)
            self.assertEqual(g.snapshot()["tip_hash"], h(2))
            self.assertFalse(g.snapshot()["whole_history_complete"])
        finally:
            g.close()
        with self.assertRaisesRegex(ValueError, "previous_block_hash"):
            Graph(Path(self.tmp.name) / "bad.sqlite", initial_start=1)

    def test_existing_identity_cannot_change(self):
        self.ingest()
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            Graph(self.g.path, initial_start=5, previous_block_hash=h(5))

    def test_page_gap_duplicate_log_and_trace_rejected_atomically(self):
        for b in (block(traces=[trace(), trace()]), block(logs=[log([], "0x"), log([], "0x")]),
                  block(transactions=[tx(i=1)], traces=[])):
            with self.assertRaises(ValueError):
                self.ingest([b])
            self.assertEqual(self.g.snapshot()["counts"]["blocks"], 0)
        with self.assertRaisesRegex(ValueError, "gap or overlap"):
            self.ingest([block(1, transactions=[], traces=[])])

    def test_empty_genesis_and_reward_sources_no_fake_payer(self):
        self.ingest([block(transactions=[], traces=[{"type": "reward"}, {"type": "reward"}])])
        self.assertEqual(self.edges(), [])
        self.assertEqual(self.g.snapshot()["anomaly_counts"]["UNINDEXED_REWARD_TRACE"], 2)
        self.assertFalse(self.g.snapshot()["finality_verified"])

    def test_invalid_fixed_and_numeric_fields_rejected(self):
        for field, value in (("from", "0x1234"), ("value", 1.5), ("value", -1), ("value", 2**256)):
            b = block(); b["transactions"][0][field] = value
            with self.assertRaises(ValueError):
                self.ingest([b])
        with self.assertRaises(ValueError):
            self.g.wallet(addr(1), 1001)



if __name__ == "__main__":
    unittest.main()
