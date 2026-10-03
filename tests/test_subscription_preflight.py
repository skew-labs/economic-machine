import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account.messages import hash_domain
import test_hosted_merchant as fixtures
from machine_commerce.operations import Operations, Settings, password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_preflight as preflight


class RPC:
    def rpc(self, url, method, params):
        if method == 'eth_chainId': return hex(421614)
        if method == 'eth_getBlockByNumber': return {'number': '0x1'}
        if method == 'eth_getCode': return '0x1234'
        if params[0]['data'] == '0x313ce567': return hex(6)
        return '0x' + hash_domain({'name': 'USD Coin', 'version': '2', 'chainId': 421614,
                                  'verifyingContract': '0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d'}).hex()


class SubscriptionPreflightTests(unittest.TestCase):
    setUp = fixtures.HostedMerchantTests.setUp
    setUpBase = fixtures.HostedMerchantTests.setUpBase
    tearDown = fixtures.HostedMerchantTests.tearDown

    def test_managed_seller_requires_current_configuration_and_active_session(self):
        profile = self.payments.profiles['atlas-monthly']
        profile['seller_owner'] = 'merchant-atlas-monthly'
        self.merchant.refresh_offers()
        config = Path(self.tmp.name) / 'merchants.json'
        output = Path(self.tmp.name) / 'preflight.json'
        config.write_text(json.dumps(self.config))
        settings = Settings(mode='production', origin='https://machine.example',
            operators={'fixture': password_hash('fixture-only-password')}, resources={'atlas-monthly': profile})
        Operations(self.store, settings)
        for approved, expected in [(True, 0), (False, 1)]:
            config.write_text(json.dumps(self.config if approved else {}))
            with patch.dict(os.environ, {'COMMERCE_DB': str(self.path), 'MACHINE_MERCHANTS_FILE': str(config)}), \
                 patch.object(preflight.Settings, 'environment', return_value=settings), \
                 patch.object(preflight, 'now_seconds', side_effect=lambda: self.now), \
                 patch.object(preflight, 'Chain', return_value=RPC()), \
                 patch.object(sys, 'argv', ['preflight', '--output', str(output)]), \
                 patch('builtins.print'), self.assertRaises(SystemExit) as raised:
                preflight.main()
            self.assertEqual(raised.exception.code, expected, output.read_text())
            self.assertEqual(json.loads(output.read_text())['ready'], approved)
