import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from machine_commerce.api import create_app
from machine_commerce.wallet_connectors import agent_wallet_status, public_wallet_config, privy_asset, wallet_csp
from machine_commerce.portal import create_portal


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'status.json'
        self.owner = Account.create()
        self.agent = Account.create().address
        self.env = patch.dict(os.environ, {'MACHINE_METAMASK_OWNER_ADDRESS': self.owner.address,
            'MACHINE_METAMASK_STATUS_FILE': str(self.path), 'MACHINE_PRIVY_APP_ID': '', 'MACHINE_PRIVY_CLIENT_ID': ''})
        self.env.start()
        self.status = dict(authenticated=True, initialized=True, mode='guard', address=self.agent, observed_at=1000)
        self.write()

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.status)); self.path.chmod(0o640)

    def read(self, now=1000):
        return agent_wallet_status({'address': self.owner.address}, now)

    def test_owner_only_allowlisted_metadata_and_no_signing(self):
        self.status['cliToken'] = 'do-not-publish'
        self.status['email'] = 'private@example.invalid'; self.write()
        result = self.read()
        self.assertEqual(result['capabilities'], ['READ_BALANCES'])
        self.assertEqual(result['address'], self.agent)
        self.assertNotIn('do-not-publish', json.dumps(result))
        self.assertNotIn('email', result)
        self.assertEqual(agent_wallet_status(None,1000), {'status':'NOT_CONFIGURED'})
        self.assertEqual(agent_wallet_status({'address':Account.create().address},1000), {'status':'NOT_CONFIGURED'})

    def test_stale_future_and_boolean_timestamps_rejected(self):
        self.assertEqual(self.read(1181)['status'], 'STALE')
        self.assertEqual(self.read(999)['status'], 'STALE')
        self.status['observed_at'] = True; self.write()
        self.assertEqual(self.read()['status'], 'STALE')

    def test_unsafe_or_invalid_session_never_ready(self):
        for field, value in [('mode','beast'), ('authenticated',False), ('initialized',False), ('address','0x'+'0'*40), ('address','invalid')]:
            with self.subTest(field=field, value=value):
                old = self.status[field]; self.status[field]=value; self.write()
                self.assertEqual(self.read()['status'],'LOGIN_REQUIRED'); self.status[field]=old

    def test_symlink_writable_and_oversize_snapshots_rejected(self):
        self.path.chmod(0o666)
        self.assertEqual(self.read()['status'],'UNAVAILABLE')
        self.path.unlink(); self.path.symlink_to('/etc/passwd')
        self.assertEqual(self.read()['status'],'UNAVAILABLE')
        self.path.unlink(); self.path.write_text('x'*8193)
        self.assertEqual(self.read()['status'],'UNAVAILABLE')

    def test_public_config_never_contains_secret(self):
        with patch.dict(os.environ, {'MACHINE_PRIVY_APP_ID':'public-app-id', 'PRIVY_APP_SECRET':'secret-value'}):
            self.assertEqual(public_wallet_config(), {'privy':{'app_id':'public-app-id','client_id':None}})
        with patch.dict(os.environ, {'MACHINE_PRIVY_APP_ID':'bad"<script>'}):
            self.assertIsNone(public_wallet_config()['privy'])

    def test_privy_assets_are_bounded(self):
        root=Path(self.temp.name); (root/'privy').mkdir(); (root/'privy'/'entry.js').write_text('test')
        self.assertEqual(privy_asset(root,'entry.js'), root/'privy'/'entry.js')
        for name in ['../status.json','entry.js.map','.env','config.json','entry.js/../status.json']:
            self.assertIsNone(privy_asset(root,name))
        (root/'privy'/'chunk-ABC123.js').symlink_to(self.path)
        self.assertIsNone(privy_asset(root,'chunk-ABC123.js'))

    def test_privy_csp_keeps_script_and_frame_boundaries(self):
        csp=wallet_csp(privy=True)
        scripts=csp.split('script-src ')[1].split(';')[0]
        self.assertNotIn('unsafe-inline',scripts); self.assertNotIn('unsafe-eval',csp)
        self.assertIn("frame-ancestors 'none'",csp)
        self.assertNotIn('privy.io',wallet_csp())

    def login(self, client, wallet):
        challenge=client.post('/api/auth/challenge',json={'address':wallet.address,'chain_id':42161}).json()
        signed=wallet.sign_message(encode_defunct(text=challenge['message']))
        result=client.post('/api/auth/verify',json={'challenge_id':challenge['challenge_id'],'signature':'0x'+signed.signature.hex()})
        self.assertEqual(result.status_code,200)

    def test_connection_requires_owner_proof_and_reuses_existing_account(self):
        from machine_commerce.operations import Settings
        from test_operations import profile
        app=create_app(Path(self.temp.name)/'test.db',lambda:1000,
            settings=Settings('development','https://console.example',{},profile()))
        with TestClient(app,base_url='https://console.example') as client:
            self.assertEqual(client.get('/api/wallet-connectors/metamask').status_code,401)
            self.login(client,Account.create())
            self.assertEqual(client.get('/api/wallet-connectors/metamask').json()['status'],'NOT_CONFIGURED')
            self.assertEqual(client.post('/api/wallet-connectors/metamask/connect',json={}).status_code,409)
            self.login(client,self.owner)
            self.assertEqual(client.get('/api/wallet-connectors/metamask').json()['address'],self.agent)
            first=client.post('/api/wallet-connectors/metamask/connect',json={})
            self.assertEqual(first.status_code,200)
            self.assertEqual(first.json()['id'],client.post('/api/wallet-connectors/metamask/connect',json={}).json()['id'])
            self.assertEqual(client.post('/api/wallet-connectors/metamask/connect',json={'address':Account.create().address}).status_code,409)
            key=client.post('/api/keys',json={'name':'read-only','scopes':['read'],'policy_id':None,'ttl_seconds':60}).json()
            self.assertEqual(client.get('/api/wallet-connectors/metamask',headers={'Authorization':'Bearer '+key['secret']}).status_code,403)

    def test_portal_exposes_only_public_config_and_console_csp(self):
        with patch.dict(os.environ, {'MACHINE_PRIVY_APP_ID':'public-app-id'}), TestClient(create_portal()) as client:
            self.assertEqual(client.get('/wallet-config').json()['privy']['app_id'],'public-app-id')
            html=client.get('/console')
            head=client.head('/console')
            self.assertEqual(head.status_code,200)
            self.assertEqual(head.content,b'')
            self.assertEqual(head.headers['content-security-policy'],html.headers['content-security-policy'])
            self.assertIn('/commerce/wallet-connectors.js',html.text)
            self.assertIn('https://auth.privy.io',html.headers['content-security-policy'])
            self.assertNotIn('https://auth.privy.io',client.get('/launch').headers['content-security-policy'])
            self.assertEqual(client.get('/privy/session.json').status_code,404)
