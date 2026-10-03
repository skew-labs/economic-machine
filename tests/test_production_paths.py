"""Changed integration boundaries only; fixture settlements are not live money."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_checkout as checkout_fixtures
import test_hosted_merchant as merchant_fixtures
from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain
from fastapi.testclient import TestClient
from test_engine_execution import SPOT, BinanceBroker, VenueFixture

from economic_machine.values import MachineError, canonical
from machine_commerce.api import create_app
from machine_commerce.backup import restore, seal
from machine_commerce.compute import ComputeService, inference_request
from machine_commerce.compute_registry import NETWORK, URL, USDC
from machine_commerce.merchant import encoded
from machine_commerce.operations import Settings
from machine_engine.connections import Connectors
from machine_engine.live import read_market
from machine_engine.workspace import Workspace


class ComputeWire:
    def __init__(self, recipient):
        self.recipient, self.calls, self.fail, self.malformed = recipient, [], False, False

    def call(self, url, body, headers=None):
        self.calls.append((url, body, bool(headers and 'PAYMENT-SIGNATURE' in headers)))
        required = {'x402Version': 2, 'resource': {'url': URL}, 'accepts': [{'scheme': 'exact', 'network': NETWORK,
            'asset': USDC, 'payTo': self.recipient, 'amount': '1000', 'maxTimeoutSeconds': 120,
            'extra': {'name': 'USD Coin', 'version': '2'}}]}
        if not headers or 'PAYMENT-SIGNATURE' not in headers:
            return 402, {'payment-required': encoded(required)}, b'{}'
        if self.fail:
            raise TimeoutError('isolated provider timeout')
        value = {'model': body['model'], 'text': 'OK',
            'usage': {'promptTokens': 5, 'completionTokens': 1, 'totalTokens': 6}}
        if self.malformed:
            value['usage']['completionTokens'] = 10000
        return 200, {'payment-response': encoded({'success': True, 'network': NETWORK, 'transaction': '0x' + 'ab' * 32})}, canonical(value)


class ComputeChain:
    status = 'PAID'

    def rpc(self, url, method, params):
        if method == 'eth_chainId': return hex(42161)
        if method == 'eth_getCode': return '0x1234'
        if method == 'eth_blockNumber': return hex(1000)
        data = params[0]['data']
        if data == '0x313ce567': return hex(6)
        if data == '0x3644e515':
            return '0x' + hash_domain({'name': 'USD Coin', 'version': '2', 'chainId': 42161, 'verifyingContract': USDC}).hex()
        return hex(100000000)

    def observe(self, profile, auth, tx, block):
        return {'status': self.status, 'finality': 'finalized', 'tx_hash': '0x' + 'ab' * 32}


class ComputePathTests(unittest.TestCase):
    setUpBase = checkout_fixtures.CheckoutTests.setUp
    tearDown = checkout_fixtures.CheckoutTests.tearDown

    def setUp(self):
        self.setUpBase()
        self.rid = 'gate402-inference'
        self.profile = self.profile | {'url': URL, 'network': NETWORK, 'asset': USDC,
            'seller_owner': 'compute-' + self.rid, 'data_type': 'compute.inference', 'data_version': 'gate402-v1'}
        self.payments.profiles[self.rid] = self.profile
        self.wire = ComputeWire(self.profile['pay_to'])
        self.payments.transport = self.wire
        self.payments.chain = ComputeChain()
        self.service = ComputeService(self.checkout, self.wire, {self.rid})
        self.account = Account.from_key('0x' + '01' * 32)
        self.request = {'model': 'llama-3.1-8b', 'messages': [{'role': 'user', 'content': 'Return OK.'}], 'max_tokens': 8}
        self.raw = {'resource_id': self.rid, 'request': self.request, 'max_total': '0.01', 'idempotency_key': 'compute-proof'}

    def prepare(self):
        order = self.service.quote(self.buyer, self.raw)
        mandate = self.payments.mandate(self.buyer, {'payer': self.account.address,
            'payment_asset': NETWORK + '/erc20:' + USDC, 'budget': '0.01', 'max_order': '0.01',
            'resources': [self.rid], 'ttl_seconds': 120})
        order = self.checkout.prepare(self.buyer, order['id'], mandate['id'])
        payment = self.payments.challenge(self.buyer, order['payment_id'])
        template = payment['payment_template']
        template['payload']['signature'] = '0x' + self.account.sign_message(encode_typed_data(full_message=payment['typed_data'])).signature.hex()
        return order, encoded(template)

    def test_compute_exact_payment_finality_and_output_share_checkout_and_budget(self):
        order, signature = self.prepare()
        payment = self.payments.submit(self.buyer, order['payment_id'], signature)
        self.assertEqual(payment['status'], 'SETTLED')
        output = self.service.result(self.buyer, order['id'])['output']
        self.assertEqual(output['choices'][0]['message']['content'], 'OK')
        self.assertFalse(output['refund_confirmed'])
        mandate = self.payments.snapshot(self.buyer)['mandates'][0]
        self.assertEqual((mandate['spent'], mandate['reserved']), (1000, 0))
        self.assertTrue(all(call[1] == self.request for call in self.wire.calls))
        with self.assertRaises(MachineError): self.service.result(self.other, order['id'])
        self.payments.submit(self.buyer, order['payment_id'], signature)
        self.assertEqual(sum(call[2] for call in self.wire.calls), 1)

    def test_restart_after_timeout_retains_hold_and_never_resends_or_fabricates_output(self):
        order, signature = self.prepare()
        self.wire.fail = True
        self.payments.chain.status = 'PENDING'
        self.assertEqual(self.payments.submit(self.buyer, order['payment_id'], signature)['status'], 'UNKNOWN')
        restarted = ComputeService(self.checkout, self.wire, {self.rid})
        self.payments.submit(self.buyer, order['payment_id'], signature)
        self.assertEqual(sum(call[2] for call in self.wire.calls), 1)
        with self.assertRaises(MachineError): restarted.result(self.buyer, order['id'])
        self.payments.chain.status = 'PAID'
        self.assertEqual(self.payments.reconcile(self.buyer, order['payment_id'])['status'], 'PAID_DELIVERY_MISSING')
        with self.assertRaises(MachineError): restarted.result(self.buyer, order['id'])

    def test_request_price_and_idempotency_are_bound_and_incomplete_intent_recovers(self):
        original = self.service.quote(self.buyer, self.raw)
        with self.store.connect() as db:
            db.execute('DELETE FROM compute_jobs WHERE checkout_id=?', (original['id'],))
        recovered = self.service.quote(self.buyer, self.raw)
        self.assertEqual(recovered['id'], original['id'])
        with self.assertRaises(MachineError): self.service.quote(self.buyer, self.raw | {'max_total': '1'})
        with self.assertRaises(MachineError): self.service.quote(self.buyer, self.raw | {'max_total': '0.0001', 'idempotency_key': 'too-low'})
        for invalid in [self.request | {'max_tokens': True}, self.request | {'messages': []}, self.request | {'tools': []}]:
            with self.assertRaises(MachineError): inference_request(invalid)

    def test_malformed_usage_cannot_be_delivered_even_if_payment_was_finalized(self):
        order, signature = self.prepare()
        self.wire.malformed = True
        self.assertEqual(self.payments.submit(self.buyer, order['payment_id'], signature)['status'], 'PAID_DELIVERY_MISSING')
        with self.assertRaises(MachineError): self.service.result(self.buyer, order['id'])

    def test_concurrent_agents_reserve_one_shared_payment_limit_without_overspending(self):
        from concurrent.futures import ThreadPoolExecutor
        mandate = self.payments.mandate(self.buyer, {'payer': self.account.address,
            'payment_asset': NETWORK + '/erc20:' + USDC, 'budget': '0.004', 'max_order': '0.001',
            'resources': [self.rid], 'ttl_seconds': 120})
        def prepare(index):
            quote = self.service.quote(self.buyer, self.raw | {'idempotency_key': 'concurrent-' + str(index)})
            try: return self.checkout.prepare(self.buyer, quote['id'], mandate['id'])['payment_id']
            except MachineError: return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            prepared = [pid for pid in pool.map(prepare, range(8)) if pid]
        self.assertEqual(len(prepared), 4)
        observed = self.payments.snapshot(self.buyer)['mandates'][0]
        self.assertEqual((observed['spent'], observed['reserved']), (0, 4000))
        for pid in prepared: self.payments.cancel_unsigned(self.buyer, pid)
        self.assertEqual(self.payments.snapshot(self.buyer)['mandates'][0]['reserved'], 0)
        self.assertFalse(any(call[2] for call in self.wire.calls))

    def test_parallel_same_request_publishes_one_checkout_and_one_publisher(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.service.quote(self.buyer, self.raw)['id'], range(8)))
        self.assertEqual(len(set(ids)), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM compute_publishers').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM compute_jobs').fetchone()[0], 1)


class SubscriptionPathTests(unittest.TestCase):
    setUp = merchant_fixtures.HostedMerchantTests.setUp
    setUpBase = merchant_fixtures.HostedMerchantTests.setUpBase
    tearDown = merchant_fixtures.HostedMerchantTests.tearDown
    signed = merchant_fixtures.HostedMerchantTests.signed

    def test_finalized_merchant_grant_unlocks_owner_scoped_data_delivery_then_expires(self):
        signature = self.signed()
        self.chain.status = 'PAID'
        self.merchant.handle('atlas-monthly', self.pid, self.body['request'], signature)
        self.payments.reconcile(self.buyer, self.pid)
        app = create_app(self.path, lambda: self.now, settings=Settings(resources=self.payments.profiles),
            payment_chain=self.chain, merchant_config=self.config, merchant_transport=self.transport)
        client = TestClient(app)
        header = {'Authorization': 'Bearer ' + self.token}
        report = {'derived': {'source_observation_root': 'fixture-root', 'items': [{'name': 'licensed-derived-fixture'}]},
                  'report_sha256': 'fixture-report'}
        with patch('machine_commerce.atlas.load_report', return_value=report):
            response = client.get('/api/commerce/subscriptions/atlas-monthly/delivery', headers=header)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['report']['items'][0]['name'], 'licensed-derived-fixture')
            self.assertEqual(client.get('/api/commerce/subscriptions/atlas-monthly/delivery',
                headers={'Authorization': 'Bearer ' + self.other_token}).status_code, 409)
            self.now += 2592001
            self.assertNotEqual(client.get('/api/commerce/subscriptions/atlas-monthly/delivery', headers=header).status_code, 200)


class MarketHTTP:
    def __init__(self, now):
        self.now, self.prices, self.gap = now, ['100', '110', '90'], False

    def request(self, url, **kwargs):
        base = self.now - 180
        rows = [[(base + i * 60) * 1000, '0', '0', '0', price, '0', (base + i * 60) * 1000 + 59999]
                for i, price in enumerate(self.prices)]
        if self.gap: rows[1][0] += 60000; rows[1][6] += 60000
        return rows


class ObservedNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = 1800000000
        self.http, self.venue = MarketHTTP(self.now), VenueFixture()
        self.env = patch.dict(os.environ, {'VENUE_KEY': 'isolated-key', 'VENUE_SECRET': 'isolated-secret'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.path = Path(self.temp.name) / 'live.sqlite3'
        self.work = Workspace(self.path, clock=lambda: self.now,
            readers=Connectors(self.http, clock=lambda: self.now),
            broker_factory=lambda body, clock: BinanceBroker(body, http=self.venue, clock=clock), live_enabled=False)
        self.market = self.work.connect({'name': 'Public BTC', 'profile': 'binance-public-market', 'config': {'symbol': 'BTCUSDT'}})
        self.spot = self.work.connect(SPOT)
        vp = self.work.trading.policy({'name': 'LiveProof', 'connection_id': self.spot['id'], 'symbols': ['BTCUSDT'],
            'sides': ['SELL'], 'turnover_limit_usdt': '100', 'max_order_usdt': '20', 'fee_reserve_bps': '10', 'expires_at': self.now + 3600})
        self.watch = self.work.live.policy({'name': 'DrawdownWatch', 'market_connection_id': self.market['id'], 'venue_policy_id': vp['id'],
            'trigger_drawdown_bps': 100, 'quantity': '0.1', 'side': 'SELL', 'reduce_only': False, 'maximum_slippage_bps': 100,
            'max_age_seconds': 120})

    def test_observed_state_calls_actual_cpp_and_produces_same_approval_order(self):
        self.work.sync(self.market['id'])
        decision = self.work.live.evaluate(self.watch['id'])
        self.assertTrue(decision['native']['computed'])
        self.assertEqual(decision['action'], 'PLAN')
        self.assertEqual(decision['language_model_calls'], 0)
        order = self.work.live.plan(decision['id'])
        self.assertEqual(order['status'], 'AWAITING_APPROVAL')
        self.assertEqual(self.work.live.plan(decision['id'])['id'], order['id'])
        self.work.trading.approve(order['id'], order['plan_hash'])
        with self.assertRaises(MachineError): self.work.trading.dispatch(order['id'])
        self.assertIsNone(self.venue.submitted)

    def test_scheduled_sync_survives_restart_and_never_dispatches(self):
        self.work.scheduler.configure(self.market['id'], enabled=True, interval_seconds=15)
        self.assertTrue(self.work.scheduler.run_once())
        restarted = Workspace(self.path, clock=lambda: self.now, readers=self.work.readers, broker_factory=self.work.trading.broker_factory)
        self.assertEqual(len(restarted.live.status()['decisions']), 1)
        self.assertFalse(restarted.live.status()['automatic_dispatch'])
        with restarted.runtime.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM engine_trade_orders').fetchone()[0], 0)

    def test_gap_stale_source_and_expired_plan_cannot_reach_owner_approval(self):
        self.http.gap = True
        with self.assertRaises(MachineError): read_market(self.http, 'BTCUSDT', self.now)
        self.http.gap = False
        self.work.sync(self.market['id'])
        decision = self.work.live.evaluate(self.watch['id'])
        order = self.work.live.plan(decision['id'])
        self.now += 121
        with self.assertRaises(MachineError): self.work.live.evaluate(self.watch['id'])
        with self.assertRaises(MachineError): self.work.trading.approve(order['id'], order['plan_hash'])

    def test_crash_before_link_and_changed_market_cannot_bypass_source_guard(self):
        self.work.sync(self.market['id'])
        decision = self.work.live.evaluate(self.watch['id'])
        order = self.work.live.plan(decision['id'])
        with self.work.runtime.connect() as db:
            db.execute('UPDATE engine_live_decisions SET order_id=NULL WHERE id=?', (decision['id'],))
            import json
            row = db.execute('SELECT snapshot FROM engine_connections WHERE id=?', (self.market['id'],)).fetchone()
            value = json.loads(row[0]); value['window_sha256'] = 'changed-window'
            db.execute('UPDATE engine_connections SET snapshot=? WHERE id=?', (canonical(value).decode(), self.market['id']))
        with self.assertRaises(MachineError): self.work.trading.approve(order['id'], order['plan_hash'])

    def test_terminal_fill_requires_independent_account_readback_and_failure_never_resends(self):
        self.work.sync(self.market['id'])
        order = self.work.live.plan(self.work.live.evaluate(self.watch['id'])['id'])
        self.work.trading.approve(order['id'], order['plan_hash'])
        self.work.trading.live_enabled = True  # Isolated fixture, no network or customer authority.
        self.work.readers = Connectors(self.venue, clock=lambda: self.now)
        original_request = self.venue.request
        def filled_request(url, **options):
            result = original_request(url, **options)
            if options.get('method') == 'POST':
                from decimal import Decimal
                result.update(status='FILLED', executedQty=order['plan']['quantity'],
                    cummulativeQuoteQty=str(Decimal(order['plan']['quantity']) * Decimal(order['plan']['price'])))
                self.venue.result = result
            return result
        with patch.object(self.venue, 'request', side_effect=filled_request):
            result = self.work.trading.dispatch(order['id'])
        self.assertEqual(result['account_readback']['status'], 'OBSERVED')
        self.assertEqual(result['status'], 'FILLED')
        self.assertFalse(result['account_readback']['commissions_reconciled'])
        with patch.object(self.work, 'sync', side_effect=TimeoutError('fixture account outage')):
            result = self.work.trading.readback(order['id'])
        self.assertEqual(result['status'], 'FILLED')
        self.assertEqual(result['account_readback']['status'], 'UNVERIFIED')

    def test_testnet_adapters_pin_account_and_order_reads_without_mainnet_fallback(self):
        from machine_engine.connections import normalize_connection
        for profile, base in [('binance-spot-testnet', 'https://testnet.binance.vision'),
                              ('binance-usdm-testnet', 'https://demo-fapi.binance.com')]:
            connection = normalize_connection(SPOT | {'profile': profile})
            wire = VenueFixture()
            snapshot = Connectors(wire, clock=lambda: self.now).read(connection)
            self.assertEqual(snapshot['network'], profile)
            broker = BinanceBroker(connection, http=wire, clock=lambda: self.now)
            broker.instrument('BTCUSDT')
            self.assertTrue(all(url.startswith(base + '/') for url, _ in wire.calls))

    def test_spot_futures_and_testnet_market_feeds_cannot_authorize_each_other(self):
        from machine_engine.connections import normalize_connection
        for profile in ['binance-testnet-market', 'binance-public-futures', 'binance-testnet-futures']:
            other = self.work.connect({'name': 'Other feed', 'profile': profile, 'config': {'symbol': 'BTCUSDT'}})
            with self.assertRaisesRegex(MachineError, 'MATCHED_FEED'):
                self.work.live.policy(self.watch['policy'] | {'market_connection_id': other['id']})
            source = Connectors(self.http, clock=lambda: self.now).read(normalize_connection({
                'name': 'Other feed', 'profile': profile, 'config': {'symbol': 'BTCUSDT'}}))
            self.assertEqual(source['network'], profile)


class BackupPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from machine_commerce.store import Store
        self.db = Store(self.root / 'original.sqlite3', lambda: 1800000000)
        self.db.create_session()
        self.key = os.urandom(32)

    def test_consistent_encrypted_snapshot_restores_journal_and_private_file(self):
        private = self.root / 'credential'; private.write_text('isolated-secret-sentinel')
        payload, report = seal([self.db.path], self.key, {'credential': private})
        self.assertNotIn(b'isolated-secret-sentinel', payload)
        restored = self.root / 'restored'
        proof = restore(payload, self.key, restored)
        self.assertTrue(proof['restore_verified'])
        self.assertEqual((restored / 'credential').read_text(), 'isolated-secret-sentinel')
        self.assertEqual((restored / 'credential').stat().st_mode & 0o777, 0o600)
        self.assertFalse(report['off_host_verified'])

    def test_tampering_wrong_key_or_existing_destination_never_overwrites(self):
        payload, _ = seal([self.db.path], self.key)
        for value, key in [(payload[:-1] + bytes([payload[-1] ^ 1]), self.key), (payload, os.urandom(32))]:
            target = self.root / 'never-created'
            with self.assertRaises(MachineError): restore(value, key, target)
            self.assertFalse(target.exists())
        with self.assertRaises(MachineError): restore(payload, self.key, self.root)

    def test_corrupted_journal_fails_before_export(self):
        with self.db.connect() as db:
            db.execute("UPDATE events SET event_hash='corrupt'")
        with self.assertRaises(MachineError): seal([self.db.path], self.key)

    def test_offsite_requires_owner_config_host_pin_and_restore_of_returned_cipher(self):
        import hashlib
        import json
        import subprocess

        from machine_commerce.offsite import destination, export_and_restore
        payload, _ = seal([self.db.path], self.key)
        encrypted = self.root / 'backup.encrypted'; encrypted.write_bytes(payload)
        key = self.root / 'ssh-identity'; key.write_text('isolated-ssh-reference'); key.chmod(0o600)
        hosts = self.root / 'known-hosts'; hosts.write_text('isolated-saved-host-key'); hosts.chmod(0o600)
        raw = {'host': 'backup.example', 'user': 'backup', 'directory': '/var/lib/skew-backups/machine',
               'identity_file': str(key), 'known_hosts_file': str(hosts)}
        config = self.root / 'destination.json'; config.write_text(json.dumps(raw)); config.chmod(0o600)
        calls = []
        def runner(args, **options):
            calls.append(args)
            if args[0] == 'cat':
                return subprocess.CompletedProcess(args, 0, b'1' * 32, b'')
            self.assertIn('StrictHostKeyChecking=yes', args)
            if args[-1] == 'cat /etc/machine-id':
                return subprocess.CompletedProcess(args, 0, b'2' * 32, b'')
            if args[0] == 'scp' and args[-2].startswith('backup@'):
                Path(args[-1]).write_bytes(payload)
            stdout = (hashlib.sha256(payload).hexdigest() + ' encrypted-file\n').encode() if args[-1].startswith('sha256sum') else b''
            return subprocess.CompletedProcess(args, 0, stdout, b'')
        result = export_and_restore(encrypted, self.key, config, runner=runner)
        self.assertTrue(result['restore_from_retrieved_ciphertext'])
        self.assertFalse(result['off_host_key_transferred'])
        self.assertTrue(result['distinct_machine_ids_observed'])
        self.assertEqual(sum(c[0] == 'scp' for c in calls), 2)
        self.assertTrue(all(str(key) not in c[-2:] for c in calls))
        with self.assertRaisesRegex(MachineError, 'DISTINCT_BACKUP_HOST_REQUIRED'):
            export_and_restore(encrypted, self.key, config, runner=lambda args, **kwargs:
                subprocess.CompletedProcess(args, 0, b'1' * 32, b''))
        config.write_text(json.dumps(raw | {'host': '-oProxyCommand=anything'}))
        with self.assertRaises(MachineError): destination(config)
        config.write_text(json.dumps(raw | {'directory': '/var/lib/skew-backups/../escape'}))
        with self.assertRaises(MachineError): destination(config)

    def test_offsite_failed_or_changed_retrieval_never_produces_acceptance(self):
        import hashlib
        import json
        import subprocess

        from machine_commerce.offsite import export_and_restore
        payload, _ = seal([self.db.path], self.key)
        encrypted = self.root / 'backup.encrypted'; encrypted.write_bytes(payload)
        key = self.root / 'ssh-identity'; key.write_text('fixture-only'); key.chmod(0o600)
        config = self.root / 'destination.json'; config.write_text(json.dumps({'host': 'backup.example', 'user': 'backup',
            'directory': '/var/lib/skew-backups/machine', 'identity_file': str(key), 'known_hosts_file': str(key)})); config.chmod(0o600)
        def changed(args, **options):
            if args[0] == 'cat': return subprocess.CompletedProcess(args, 0, b'1' * 32, b'')
            if args[-1] == 'cat /etc/machine-id': return subprocess.CompletedProcess(args, 0, b'2' * 32, b'')
            if args[0] == 'scp' and args[-2].startswith('backup@'): Path(args[-1]).write_bytes(b'changed cipher')
            stdout = (hashlib.sha256(payload).hexdigest() + ' file\n').encode() if args[-1].startswith('sha256sum') else b''
            return subprocess.CompletedProcess(args, 0, stdout, b'')
        with self.assertRaisesRegex(MachineError, 'RETRIEVED_BACKUP_HASH_MISMATCH'):
            export_and_restore(encrypted, self.key, config, runner=changed)
        with self.assertRaisesRegex(MachineError, 'TRANSFER'):
            export_and_restore(encrypted, self.key, config, runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('ssh', 60)))


class ConcurrentOwnerTests(unittest.TestCase):
    def test_eight_authenticated_owners_compute_and_keep_workspaces_isolated(self):
        from concurrent.futures import ThreadPoolExecutor

        from economic_machine.journal import verify_journal
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Path(temporary) / 'commerce.sqlite3', clock=lambda: 1000)
            clients = [TestClient(app) for _ in range(8)]
            try:
                for client in clients:
                    self.assertEqual(client.post('/api/sessions', json={}).status_code, 200)
                def run(index):
                    client = clients[index]
                    prefix = client.get('/api/engine/profiles').json()['credential_namespace']
                    config = {name: prefix + name.upper() for name in SPOT['config']}
                    added = client.post('/api/engine/connections', json=SPOT | {'name': f'Owner {index}', 'config': config})
                    self.assertEqual(added.status_code, 200)
                    result = client.post('/api/engine/economics/evaluate', json={'operation': 'RETURN_STATISTICS', 'input': {
                        'samples': [{'timestamp_ns': 60000000000, 'price': 100000000},
                                    {'timestamp_ns': 120000000000, 'price': 90000000}], 'count': 2,
                        'expected_interval_ns': 60000000000, 'interval_tolerance_ns': 0}})
                    self.assertEqual(result.status_code, 200)
                    self.assertTrue(result.json()['computed'])
                    overview = client.get('/api/engine/overview').json()
                    self.assertEqual([row['name'] for row in overview['connections']], [f'Owner {index}'])
                    self.assertFalse(overview['trading']['live_transmission_enabled'])
                    return prefix
                with ThreadPoolExecutor(max_workers=8) as pool:
                    prefixes = list(pool.map(run, range(8)))
                self.assertEqual(len(set(prefixes)), 8)
                self.assertEqual(clients[0].post('/api/engine/connections', json=SPOT | {'config': {
                    name: prefixes[1] + name.upper() for name in SPOT['config']}}).status_code, 409)
                for path in (Path(temporary) / 'engine-workspaces').glob('*.sqlite3'):
                    import sqlite3
                    with sqlite3.connect(path) as db:
                        db.row_factory = sqlite3.Row
                        self.assertTrue(verify_journal(db))
            finally:
                for client in clients:
                    client.close()


class JournalFingerprintTests(unittest.TestCase):
    def test_verified_history_reuses_decoding_but_every_old_field_change_is_detected(self):
        from economic_machine.journal import verify_journal
        with tempfile.TemporaryDirectory() as t:
            work = Workspace(Path(t) / 'fingerprint.sqlite3')
            with patch('machine_engine.workspace.verify_journal', wraps=verify_journal) as verifier:
                for index in range(4):
                    with work.runtime.connect() as db:
                        db.execute('BEGIN IMMEDIATE'); work.event(db, 'OBSERVE', {'sequence': index})
                self.assertEqual(verifier.call_count, 1)
            for column, value in [('kind', 'changed-kind'), ('event_json', '{}'), ('input_hash', 'changed-input'),
                                  ('output_hash', 'changed-output'), ('previous_hash', 'changed-previous'), ('event_hash', 'changed-hash')]:
                with work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    original = db.execute('SELECT ' + column + ' FROM events WHERE ordinal=1').fetchone()[0]
                    db.execute('UPDATE events SET ' + column + '=? WHERE ordinal=1', (value,))
                with self.assertRaisesRegex(MachineError, 'JOURNAL_INTEGRITY_FAILED'), work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE'); work.event(db, 'OBSERVE', {'sequence': 5})
                with work.runtime.connect() as db:
                    db.execute('UPDATE events SET ' + column + '=? WHERE ordinal=1', (original,))
            with work.runtime.connect() as db:
                db.execute('BEGIN IMMEDIATE'); work.event(db, 'OBSERVE', {'sequence': 6})
                self.assertTrue(verify_journal(db))

    def test_restart_and_rolled_back_append_require_revalidation(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'fingerprint.sqlite3'
            work = Workspace(path)
            with self.assertRaises(RuntimeError), work.runtime.connect() as db:
                db.execute('BEGIN IMMEDIATE'); work.event(db, 'OBSERVE', {'rolled_back': True})
                raise RuntimeError('isolated rollback')
            from economic_machine.journal import verify_journal
            with patch('machine_engine.workspace.verify_journal', wraps=verify_journal) as verifier:
                with work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE'); work.event(db, 'OBSERVE', {'committed': True})
                self.assertEqual(verifier.call_count, 1)
            restarted = Workspace(path)
            with patch('machine_engine.workspace.verify_journal', wraps=verify_journal) as verifier:
                with restarted.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE'); restarted.event(db, 'OBSERVE', {'after_restart': True})
                self.assertEqual(verifier.call_count, 1)
