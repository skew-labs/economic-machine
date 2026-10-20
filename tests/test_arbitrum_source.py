import copy
import http.client
import io
import json
import unittest
import urllib.error
from email.message import Message

from economic_machine.arbitrum_research.source import (
    MAX_BLOCKS, PublicSource, SourceBackoff, SourceHTTPError, SourceIntegrityError,
    SourceLimit, parse_page, query, retry_after,
)


def h(number):
    return "0x" + format(number, "064x")


def block(number, transactions=None):
    b = {"header": {"number": number, "hash": h(number + 1),
                    "parentHash": h(number), "timestamp": 0}}
    if transactions is not None:
        b["transactions"] = transactions
    return b


def tx(index=0, status=1):
    return {"transactionIndex": index, "hash": h(index + 100), "status": status,
            "from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "value": "0x1"}


def raw(blocks):
    return b"".join(json.dumps(b).encode() + b"\n" for b in blocks)


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}
        self.read_limits = []

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SourceTests(unittest.TestCase):
    def test_full_unfiltered_fields_and_genesis_query(self):
        q = query(0, 99)
        self.assertTrue(q["includeAllBlocks"])
        for table in ("transactions", "logs", "traces"):
            self.assertEqual(q[table], [{}])
        self.assertTrue(q["fields"]["transaction"]["status"])
        self.assertTrue(q["fields"]["trace"]["callCallType"])
        self.assertTrue(q["fields"]["trace"]["suicideRefundAddress"])
        q["fields"]["block"]["hash"] = False
        self.assertTrue(query(0, 0)["fields"]["block"]["hash"])

    def test_range_bound_and_bool_rejected(self):
        for start, end in ((True, 1), (0, False), (-1, 1), (1, 0), (0, 2**53)):
            with self.assertRaises(ValueError):
                query(start, end)
        with self.assertRaises(SourceLimit):
            query(0, MAX_BLOCKS)

    def test_empty_genesis_is_real_header_not_fabricated(self):
        page = parse_page(raw([block(0), block(1)]), 0, 1)
        self.assertEqual(page["next_block"], 2)
        self.assertEqual(page["blocks"][0]["transactions"], [])
        self.assertEqual(page["coverage"]["classic_blocks"], 2)
        self.assertFalse(page["independent_canonical_verification"])
        self.assertFalse(page["coverage"]["table_completeness_verified"])

    def test_short_batch_advances_only_returned_prefix(self):
        page = parse_page(raw([block(0)]), 0, 99)
        self.assertEqual(page["next_block"], 1)
        self.assertFalse(page["range_complete"])

    def test_zero_bytes_and_empty_array_do_not_advance(self):
        for data in (b"", b"\n", b"[]"):
            with self.assertRaises(SourceBackoff):
                parse_page(data, 0, 2)

    def test_missing_start_gap_order_duplicate_and_extra_block_rejected(self):
        for values in ([block(1)], [block(0), block(2)], [block(0), block(0)],
                       [block(1), block(0)], [block(0), block(1), block(2)]):
            with self.assertRaises(SourceIntegrityError):
                parse_page(raw(values), 0, 1)

    def test_parent_link_and_hash_rejected(self):
        b = block(1)
        b["header"]["parentHash"] = h(9)
        with self.assertRaises(SourceIntegrityError):
            parse_page(raw([block(0), b]), 0, 1)
        b["header"]["hash"] = "not a hash"
        with self.assertRaises(SourceIntegrityError):
            parse_page(raw([b]), 1, 1)

    def test_truncated_ndjson_retains_complete_records_only(self):
        prefix = raw([block(0)])
        page = parse_page(prefix + b'{"header":', 0, 3, truncated=True)
        self.assertEqual(page["raw"], prefix)
        self.assertEqual(page["next_block"], 1)
        self.assertTrue(page["response_truncated"])

    def test_first_oversized_block_and_partial_array_rejected(self):
        for data in (b'{"header":', b'[{"header":'):
            with self.assertRaises(SourceLimit):
                parse_page(data, 0, 3, truncated=True)

    def test_unexpected_truncated_json_never_commits_prefix(self):
        with self.assertRaises(SourceIntegrityError):
            parse_page(raw([block(0)]) + b'{"header":', 0, 1)

    def test_json_array_supported(self):
        page = parse_page(json.dumps([block(0), block(1)]).encode(), 0, 1)
        self.assertEqual(page["next_block"], 2)

    def test_duplicate_keys_nan_and_nonblock_rejected(self):
        for data in (b'{"header":{},"header":{}}', b'{"header":NaN}', b'42', b'{"error":"oops"}'):
            with self.assertRaises(SourceIntegrityError):
                parse_page(data, 0, 1)

    def test_missing_receipt_status_remains_unknown(self):
        page = parse_page(raw([block(0, [tx(status=None)])]), 0, 0)
        self.assertEqual(page["coverage"]["transactions_missing_receipt_status"], 1)
        self.assertEqual(page["coverage"]["transactions_without_trace_rows"], 1)
        self.assertIsNone(page["blocks"][0]["transactions"][0]["status"])

    def test_invalid_status_or_transaction_index_rejected(self):
        for t in (tx(1), tx(status=True), tx(status=3)):
            with self.assertRaises(SourceIntegrityError):
                parse_page(raw([block(0, [t])]), 0, 0)

    def test_log_membership_hash_and_duplicate_checked(self):
        b = block(0, [tx()])
        good = {"logIndex": 0, "transactionIndex": 0, "transactionHash": h(100)}
        b["logs"] = [good]
        self.assertEqual(parse_page(raw([b]), 0, 0)["coverage"]["logs"], 1)
        for logs in ([good, good], [dict(good, logIndex=1)], [dict(good, transactionIndex=1)], [dict(good, transactionHash=h(101))]):
            b["logs"] = logs
            with self.assertRaises(SourceIntegrityError):
                parse_page(raw([b]), 0, 0)

    def test_nested_trace_and_errors_preserved(self):
        b = block(0, [tx()])
        t = {"transactionIndex": 0, "traceAddress": [], "type": "call", "error": "Reverted",
             "action": {"from": "0x" + "1" * 40, "to": "0x" + "2" * 40,
                        "callType": "invalid", "value": "0x1"}}
        b["traces"] = [t]
        page = parse_page(raw([b]), 0, 0)
        self.assertEqual(page["blocks"][0]["traces"][0], t)
        self.assertEqual(page["coverage"]["transactions_without_trace_rows"], 0)

    def test_duplicate_trace_and_bad_membership_rejected(self):
        b = block(0, [tx()])
        t = {"transactionIndex": 0, "traceAddress": [], "type": "call"}
        for traces in ([t, t], [dict(t, transactionIndex=1)], [dict(t, traceAddress=[True])]):
            b["traces"] = traces
            with self.assertRaises(SourceIntegrityError):
                parse_page(raw([b]), 0, 0)

    def test_reward_without_transaction_not_discarded(self):
        b = block(0)
        b["traces"] = [{"type": "reward", "traceAddress": [], "action": {"value": "0x1"}}]
        self.assertEqual(parse_page(raw([b]), 0, 0)["coverage"]["traces"], 1)

    def test_fetch_limits_reads_and_uses_finalized_endpoint(self):
        response = Response(raw([block(0)]))
        opener = Opener(response)
        page = PublicSource(max_response=1024, opener=opener).fetch(0, 10)
        self.assertEqual(page["next_block"], 1)
        self.assertEqual(response.read_limits, [1025])
        request = opener.requests[0][0]
        self.assertTrue(request.full_url.endswith("/finalized-stream"))
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertIsNone(request.get_header("Authorization"))

    def test_metadata_requires_genesis(self):
        for value in ({"dataset": "arbitrum-one", "start_block": 1},
                      {"dataset": "other", "start_block": 0}):
            with self.assertRaises(SourceIntegrityError):
                PublicSource(opener=Opener(Response(json.dumps(value).encode()))).metadata()

    def test_head_is_provider_number(self):
        self.assertEqual(PublicSource(opener=Opener(Response(json.dumps(
            {"number": 500, "hash": h(500)}).encode()))).head(), 500)

    def test_retry_after_seconds_and_date_not_shortened(self):
        self.assertEqual(retry_after("7200", now=0), 7200)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 01:00:00 GMT", now=0), 3600)
        self.assertEqual(retry_after("nan", now=0), 60)

    def test_http_backoff_without_automatic_retry(self):
        headers = Message()
        headers["Retry-After"] = "7200"
        opener = Opener(urllib.error.HTTPError("https://portal.sqd.dev", 429, "busy", headers, None))
        with self.assertRaises(SourceBackoff) as cm:
            PublicSource(opener=opener).fetch(0, 0)
        self.assertEqual(cm.exception.retry_after_seconds, 7200)
        self.assertEqual(len(opener.requests), 1)

    def test_auth_error_not_retried_or_hidden(self):
        opener = Opener(urllib.error.HTTPError("https://portal.sqd.dev", 403, "denied", {}, None))
        with self.assertRaises(SourceHTTPError) as cm:
            PublicSource(opener=opener).fetch(0, 0)
        self.assertEqual(cm.exception.status, 403)

    def test_unexpected_compression_rejected(self):
        with self.assertRaises(SourceIntegrityError):
            PublicSource(opener=Opener(Response(b"gzipbytes", headers={"Content-Encoding": "gzip"}))).fetch(0, 0)

    def test_incomplete_network_response_retains_no_progress(self):
        class Broken(Response):
            def read(self, size=-1):
                raise http.client.IncompleteRead(raw([block(0)]))
        with self.assertRaises(SourceBackoff):
            PublicSource(opener=Opener(Broken(b""))).fetch(0, 1)


if __name__ == "__main__":
    unittest.main()
