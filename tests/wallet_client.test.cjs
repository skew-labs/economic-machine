const {test} = require('node:test');
const assert = require('node:assert/strict');
const {signIn, safeIcon, messageHex} = require('../web/wallet.js');
const a = '0x' + '11'.repeat(20), b = '0x' + '22'.repeat(20);
function provider({reject = false, changed = false} = {}) {
  let reads = 0;
  return {async request({method, params}) {
    if (method === 'eth_requestAccounts') return [a];
    if (method === 'eth_accounts') return [changed && ++reads > 1 ? b : a];
    if (method === 'eth_chainId') return '0x66eee';
    if (method === 'personal_sign') {
      assert.equal(params[1], a);
      assert.equal(params[0], messageHex('Sign in.'));
      if (reject) throw Object.assign(new Error('Rejected'), {code: 4001});
      return '0xfixture';
    }
    throw new Error('Unexpected wallet operation: ' + method);
  }};
}
test('wallet flow requests only account access and personal signature, then verifies', async () => {
  const paths = [];
  const result = await signIn(provider(), async (path, body) => {
    paths.push(path);
    if (path.endsWith('challenge')) { assert.equal(body.chain_id, 421614); return {challenge_id: 'id', message: 'Sign in.'}; }
    assert.equal(body.signature, '0xfixture'); return {identity: {address: a}};
  });
  assert.equal(result.identity.address, a);
  assert.deepEqual(paths, ['/api/auth/challenge', '/api/auth/verify']);
});
test('rejected signature never reaches verification', async () => {
  const paths = [];
  await assert.rejects(signIn(provider({reject: true}), async path => { paths.push(path); return {message: 'Sign in.'}; }), {code: 4001});
  assert.deepEqual(paths, ['/api/auth/challenge']);
});
test('account change during signing cannot log into the previous wallet', async () => {
  const paths = [];
  await assert.rejects(signIn(provider({changed: true}), async path => { paths.push(path); return {message: 'Sign in.'}; }), /wallet changed/);
  assert.deepEqual(paths, ['/api/auth/challenge']);
});
test('untrusted icons are data images rendered separately, not markup or remote tracking', () => {
  assert.equal(safeIcon('https://tracker.example/icon.svg'), null);
  assert.equal(safeIcon('javascript:alert(1)'), null);
  assert.equal(safeIcon('<svg onload="alert(1)">'), null);
  assert.equal(safeIcon('data:text/html,<script/>'), null);
  assert.equal(safeIcon('data:image/png;base64,AAAA'), 'data:image/png;base64,AAAA');
  assert.equal(safeIcon('data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E'), 'data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E');
  assert.equal(messageHex('é'), '0xc3a9');
});
