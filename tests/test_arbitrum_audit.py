import hashlib
import io
import json
import unittest
import urllib.error

from economic_machine.arbitrum_research.audit import AuditClient


def h(n):
    return "0x" + format(n, "064x")


def block(n, count=0):
    return {"header": {"number": n, "hash": h(n + 1), "parentHash": h(n)},
            "transactions": [{"transactionIndex": i, "hash": h(100 + i), "status": 1}
                             for i in range(count)], "logs": [], "traces": []}


def page(blocks):
    return {"blocks": blocks, "sha256": "a" * 64}


def rpc_block(b):
    return {"number": hex(b["header"]["number"]), "hash": b["header"]["hash"],
            "parentHash": b["header"]["parentHash"],
            "transactions": [t["hash"] for t in b["transactions"]]}


def receipt(b, t):
    return {"blockNumber": hex(b["header"]["number"]), "blockHash": b["header"]["hash"],
            "transactionHash": t["hash"], "transactionIndex": hex(t["transactionIndex"]),
            "status": hex(t["status"] or 1), "logs": []}


class Response(io.BytesIO):
    status = 200
    headers = {}


class Opener:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def open(self, req, timeout):
        body = json.loads(req.data)
        self.calls.append(body)
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return Response(value)
        envelope = {"jsonrpc": "2.0", "id": body["id"]}
        if isinstance(value, tuple):
            envelope["error"] = value[1]
        else:
            envelope["result"] = value
        return Response(json.dumps(envelope).encode())


class AuditTests(unittest.TestCase):
    def test_empty_block_sample_correspondence_and_raw_witness(self):
        b = block(0)
        result = AuditClient(opener=Opener(["0xa4b1", rpc_block(b)])).audit_page(page([b]))
        self.assertEqual(result["outcome"], "CORROBORATED")
        self.assertEqual(result["request_count"], 2)
        self.assertFalse(result["independent_consensus_verification"])
        self.assertFalse(result["graph_apply_allowed"])
        self.assertEqual(result["sha256"], hashlib.sha256(result["raw"]).hexdigest())

    def test_mismatching_chain_stops_before_other_queries(self):
        opener = Opener(["0x1"])
        result = AuditClient(opener=opener).audit_page(page([block(0)]))
        self.assertEqual(result["outcome"], "MISMATCH")
        self.assertEqual(len(opener.calls), 1)

    def test_rate_limit_respected_without_retry(self):
        opener = Opener([urllib.error.HTTPError("public", 429, "busy", {"Retry-After": "1800"}, io.BytesIO(b"busy"))])
        result = AuditClient(opener=opener).audit_page(page([block(0)]))
        self.assertEqual(result["retry_after_seconds"], 1800)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["outcome"], "UNAVAILABLE")

    def test_rate_limit_during_headers_stops_receipts(self):
        opener = Opener(["0xa4b1", urllib.error.HTTPError("public", 429, "busy", {}, io.BytesIO(b"busy"))])
        result = AuditClient(opener=opener).audit_page(page([block(0), block(1, 1)]))
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["transaction_checks"][0]["receipt_outcome"], "UNAVAILABLE")
        self.assertGreater(result["retry_after_seconds"], 0)

    def test_header_hash_or_transaction_hash_mismatch(self):
        b = block(0, 1)
        for changed in (dict(rpc_block(b), hash=h(999)), dict(rpc_block(b), transactions=[])):
            result = AuditClient(opener=Opener(["0xa4b1", changed])).audit_page(page([b]), 0)
            self.assertEqual(result["outcome"], "MISMATCH")

    def test_missing_trace_public_method_unsupported_remains_gap(self):
        b = block(0, 1)
        opener = Opener(["0xa4b1", rpc_block(b), receipt(b, b["transactions"][0]),
                         ("error", {"code": -32601, "message": "method not available"})])
        result = AuditClient(opener=opener).audit_page(page([b]), 1)
        self.assertEqual(result["outcome"], "UNAVAILABLE")
        self.assertEqual(result["transaction_checks"][0]["receipt_outcome"], "CORROBORATED")
        self.assertEqual(result["transaction_checks"][0]["trace_reason"], "RPC_ERROR")
        self.assertEqual(result["gaps"][0]["kind"], "TRACE_RECOVERY_UNAVAILABLE")
        self.assertFalse(result["gaps"][0]["original_gap_resolved"])

    def test_successful_trace_stays_candidate_and_does_not_mutate_source(self):
        b = block(0, 1)
        opener = Opener(["0xa4b1", rpc_block(b), receipt(b, b["transactions"][0]),
                         {"type": "CALL", "from": "0x" + "1" * 40, "error": "execution reverted"}])
        result = AuditClient(opener=opener).audit_page(page([b]), 1)
        self.assertEqual(result["outcome"], "CANDIDATE")
        self.assertFalse(result["all_original_gaps_resolved"])
        self.assertEqual(b["traces"], [])
        self.assertEqual(result["transaction_checks"][0]["trace_kind"], "UNVERIFIED_RPC_CALL_TRACE")

    def test_missing_receipt_recovery_does_not_assume_success(self):
        b = block(0, 1)
        b["transactions"][0]["status"] = None
        b["traces"] = [{"transactionIndex": 0}]
        r = receipt(b, b["transactions"][0]); r["status"] = "0x0"
        result = AuditClient(opener=Opener(["0xa4b1", rpc_block(b), r])).audit_page(page([b]))
        self.assertEqual(result["transaction_checks"][0]["receipt_outcome"], "CANDIDATE")
        self.assertEqual(result["transaction_checks"][0]["observed_receipt_status"], 0)
        self.assertIsNone(b["transactions"][0]["status"])

    def test_receipt_wrong_block_never_fetches_trace(self):
        b = block(0, 1)
        r = receipt(b, b["transactions"][0]); r["blockHash"] = h(500)
        result = AuditClient(opener=Opener(["0xa4b1", rpc_block(b), r])).audit_page(page([b]))
        self.assertEqual(result["outcome"], "MISMATCH")
        self.assertEqual(result["request_count"], 3)

    def test_transaction_limit_preserves_unsampled_gaps(self):
        b = block(0, 3)
        opener = Opener(["0xa4b1", rpc_block(b)])
        result = AuditClient(opener=opener).audit_page(page([b]), 0)
        self.assertEqual(len(result["original_gaps"]), 3)
        self.assertEqual(result["unattempted_gap_count"], 3)
        self.assertEqual(result["request_count"], 2)

    def test_maximum_is_seven_read_only_calls(self):
        b0, b1 = block(0, 2), block(1)
        values = ["0xa4b1", rpc_block(b0), rpc_block(b1)]
        for t in b0["transactions"]:
            values += [receipt(b0, t), {"type": "CALL"}]
        opener = Opener(values)
        result = AuditClient(opener=opener).audit_page(page([b0, b1]))
        self.assertEqual(result["request_count"], 7)
        self.assertEqual([x["method"] for x in opener.calls].count("debug_traceTransaction"), 2)

    def test_envelope_mismatch_and_non_json_not_trusted(self):
        for value in (b"html", b'{"jsonrpc":"2.0","id":2,"result":"0xa4b1"}'):
            result = AuditClient(opener=Opener([value])).audit_page(page([block(0)]))
            self.assertEqual(result["outcome"], "UNAVAILABLE")

    def test_oversized_response_is_bounded_and_recorded(self):
        result = AuditClient(opener=Opener([b"x" * 2048]), max_response=1024).audit_page(page([block(0)]))
        self.assertEqual(result["records"][0]["raw_response_bytes"], 1024)
        self.assertTrue(result["records"][0]["raw_response_truncated"])

    def test_argument_limits_and_no_client_reuse(self):
        for limit in (True, -1, 3):
            with self.assertRaises(ValueError):
                AuditClient().audit_page(page([block(0)]), limit)
        client = AuditClient(opener=Opener(["0xa4b1", rpc_block(block(0))]))
        client.audit_page(page([block(0)]))
        with self.assertRaises(ValueError):
            client.audit_page(page([block(0)]))


if __name__ == "__main__":
    unittest.main()
