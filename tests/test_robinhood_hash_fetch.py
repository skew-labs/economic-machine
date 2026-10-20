import copy
import json
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.source import SourceBackoff, SourceIntegrityError
from economic_machine.robinhood_research.source import PublicSource, RPC, GENESIS, normalize, parse_page
from test_robinhood_research import fixture, envelope, A, B


def block_entry(n):
    e = fixture()
    h = GENESIS if n == 0 else '0x' + format(n, '064x')
    parent = GENESIS if n == 1 else '0x' + format(max(0, n-1), '064x')
    tx_hash = '0x' + format(1000+n, '064x')
    block = e['block']['response']['result']
    block.update(number=hex(n), hash=h, parentHash=parent)
    e['block']['request']['params'] = [hex(n), True]
    e['receipts']['request']['params'] = [hex(n)]
    if n == 0:
        block['transactions'] = []
        e['receipts']['response']['result'] = []
    else:
        block['transactions'][0].update(hash=tx_hash, blockHash=h, blockNumber=hex(n))
        e['receipts']['response']['result'][0].update(transactionHash=tx_hash, blockHash=h, blockNumber=hex(n))
    return e


class Network:
    def __init__(self):
        self.calls = []
        self.trace_error = False
        self.fail_at_batch = None
        self.bad_receipt_hash = False

    def exchange(self, requests):
        self.calls.append(copy.deepcopy(requests))
        if len(self.calls) == self.fail_at_batch:
            raise SourceBackoff(180, 429)
        out = []
        for r in requests:
            arg = r['params'][0]
            n = 0 if arg == GENESIS else int(arg, 16)
            e = block_entry(n)
            method = r['method']
            if method == 'eth_getBlockByNumber':
                value = e['block']['response']['result']
                if r['params'][1] is False:
                    value['transactions'] = [tx['hash'] for tx in value['transactions']]
            elif method == 'eth_getBlockReceipts':
                value = e['receipts']['response']['result']
                if value and self.bad_receipt_hash:
                    value[0]['blockHash'] = '0x' + 'ff'*32
            else:
                if self.trace_error:
                    out.append(dict(jsonrpc='2.0', id=r['id'], error=dict(code=-32601, message='unavailable')))
                    continue
                value = [dict(txHash=e['block']['response']['result']['transactions'][0]['hash'],
                    result=dict(type='CALL', value='0x1', **{'from': A, 'to': B}))]
            out.append(dict(jsonrpc='2.0', id=r['id'], result=value))
        return list(reversed(out))


def source(mode=True):
    s = PublicSource(hash_bound=mode)
    s.identity = envelope('eth_chainId', [], '0x1237')
    return s


class HashFetchTests(unittest.TestCase):
    def test_same_records_with_fewer_calls_and_round_trips(self):
        pages = []
        networks = []
        for mode in (False, True):
            s = source(mode); net = Network()
            with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
                pages.append(s.fetch(1, 16))
            networks.append(net)
        self.assertEqual(pages[0]['blocks'], pages[1]['blocks'])
        self.assertEqual([len(n.calls) for n in networks], [6, 4])
        self.assertEqual([sum(map(len, n.calls)) for n in networks], [64, 48])
        self.assertTrue(all(len(batch) <= 16 for n in networks for batch in n.calls))
        self.assertEqual(pages[1]['response']['rpc_units'], 48)
        self.assertEqual(parse_page(pages[1]['raw'], 1, 16)['blocks'], pages[0]['blocks'])

    def test_empty_genesis_and_next_block_traces_preserved(self):
        s = source(); net = Network()
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            page = s.fetch(0, 1)
        self.assertEqual(page['blocks'][0]['trace_boundary'], 'NO_TRANSACTIONS')
        self.assertEqual(len(page['blocks'][1]['traces']), 1)
        self.assertEqual(len(net.calls), 2)

    def test_wrong_receipt_block_is_not_accepted(self):
        s = source(); net = Network(); net.bad_receipt_hash = True
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            with self.assertRaises(SourceIntegrityError):
                s.fetch(1, 2)

    def test_partial_page_preserves_retry_and_cursor_boundary(self):
        s = source(); net = Network(); net.fail_at_batch = 3
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            page = s.fetch(1, 16)
        self.assertEqual(page['last_block'], 8)
        self.assertEqual(page['retry_after_seconds'], 180)
        self.assertEqual(page['requested_end'], 16)

    def test_error_before_first_group_does_not_create_a_page(self):
        s = source(); net = Network(); net.fail_at_batch = 2
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            with self.assertRaises(SourceBackoff):
                s.fetch(1, 16)

    def test_unsupported_tracing_retains_raw_error_and_gap(self):
        s = source(); net = Network(); net.trace_error = True
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            page = s.fetch(1, 16)
        self.assertEqual(len(page['blocks']), 16)
        self.assertTrue(all(b['trace_boundary'] == 'TRACE_UNAVAILABLE' for b in page['blocks']))
        entries = json.loads(page['raw'])['entries']
        self.assertEqual(entries[0]['traces']['response']['error']['code'], -32601)
        self.assertNotIn('traces', entries[8])

    def test_number_receipt_cannot_be_relabelled_as_hash_bound(self):
        e = block_entry(1); e['binding'] = 'block_hash'
        with self.assertRaises(SourceIntegrityError):
            normalize(e, 1)

    def test_hash_trace_requires_the_selected_header_hash(self):
        s = source(); net = Network()
        with patch.object(s.rpc, 'exchange', side_effect=net.exchange):
            entry = json.loads(s.fetch(1, 1)['raw'])['entries'][0]
        for change in ('hash', 'method', 'transaction'):
            e = copy.deepcopy(entry)
            if change == 'hash':e['traces']['request']['params'][0] = '0x' + 'ff'*32
            elif change == 'method':e['traces']['request']['method'] = 'debug_traceBlockByNumber'
            else:e['traces']['response']['result'][0]['txHash'] = '0x' + 'ff'*32
            with self.assertRaises(SourceIntegrityError):normalize(e, 1)

    def test_unknown_binding_is_rejected(self):
        e = block_entry(1); e['binding'] = 'optimistic'
        with self.assertRaises(SourceIntegrityError):normalize(e, 1)

    def test_number_path_still_requires_post_trace_header(self):
        e = block_entry(1)
        e['traces'] = envelope('debug_traceBlockByNumber', ['0x1', {'tracer':'callTracer'}],
            [dict(txHash=e['block']['response']['result']['transactions'][0]['hash'],
                result=dict(type='CALL', **{'from':A,'to':B,'value':'0x1'}))])
        with self.assertRaises(KeyError):normalize(e, 1)

    def test_read_only_allowlist_preserved(self):
        for method in ('eth_sendTransaction', 'eth_sendRawTransaction', 'personal_unlockAccount'):
            with self.assertRaises(ValueError):RPC().call(method, [])


if __name__ == '__main__':
    unittest.main()
