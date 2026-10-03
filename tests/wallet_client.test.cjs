const {test} = require('node:test');
const assert = require('node:assert/strict');
const {signIn, signPayment, sendDataPassStep, safeIcon, messageHex} = require('../web/wallet.js');
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

function paymentFixture() {
  const before = Math.floor(Date.now() / 1000) + 60, after = before - 65;
  const auth = {from: a, to: b, value: '10000000', validAfter: String(after), validBefore: String(before), nonce: '0x' + 'cc'.repeat(32)};
  const asset = '0x' + '33'.repeat(20);
  return {status: 'CHALLENGE_READY', signature_required: true, amount_atoms: '10000000', payer: a, pay_to: b, asset,
    network: 'eip155:421614', expires: before,
    typed_data: {primaryType: 'TransferWithAuthorization', domain: {name: 'USD Coin', version: '2', chainId: 421614, verifyingContract: asset},
      types: {}, message: {...auth, value: 10000000, validAfter: after, validBefore: before}},
    payment_template: {x402Version: 2, resource: {url: 'https://merchant.example/monthly'}, accepted: {
      scheme: 'exact', network: 'eip155:421614', asset, payTo: b, amount: '10000000'}, payload: {authorization: auth}}};
}
function paymentProvider({account = a, chain = '0x66eee', changed = false, reject = false} = {}) {
  const calls = []; let reads = 0;
  return {calls, async request({method, params}) {
    calls.push(method);
    if (method === 'eth_accounts') return [changed && ++reads > 1 ? b : account];
    if (method === 'eth_chainId') return chain;
    if (method === 'eth_signTypedData_v4') {
      assert.equal(params[0], a);
      assert.equal(JSON.parse(params[1]).message.value, 10000000);
      if (reject) throw Object.assign(new Error('Rejected'), {code:4001});
      return '0x' + 'ab'.repeat(65);
    }
    throw new Error('Unexpected wallet operation: ' + method);
  }};
}
test('x402 review signs exactly one EIP-3009 authorization and never sends a transaction', async () => {
  const wallet = paymentProvider(), payment = paymentFixture();
  const header = await signPayment(wallet, payment);
  const decoded = JSON.parse(Buffer.from(header, 'base64').toString('utf8'));
  assert.equal(decoded.payload.signature, '0x' + 'ab'.repeat(65));
  assert.deepEqual(decoded.payload.authorization, payment.payment_template.payload.authorization);
  assert.equal(wallet.calls.filter(s => s === 'eth_signTypedData_v4').length, 1);
  assert.equal(wallet.calls.includes('eth_sendTransaction'), false);
  assert.equal(payment.payment_template.payload.signature, undefined);
});
test('mismatched price, recipient, domain, chain, nonce or expired challenge stops before wallet signing', async () => {
  for (const change of [p => {p.amount_atoms = '11000000';}, p => {p.pay_to = a;},
    p => {p.typed_data.domain.verifyingContract = a;}, p => {p.typed_data.message.value = 11;},
    p => {p.network = 'eip155:1';}, p => {p.payment_template.payload.authorization.nonce = 'bad';},
    p => {p.expires = 1;}, p => {p.status = 'SUBMITTED';}]) {
    const payment = paymentFixture(), wallet = paymentProvider(); change(payment);
    await assert.rejects(signPayment(wallet, payment));
    assert.equal(wallet.calls.includes('eth_signTypedData_v4'), false);
  }
});
test('different payer account or network cannot approve a purchase', async () => {
  for (const args of [{account:b}, {chain:'0xa4b1'}]) {
    const wallet = paymentProvider(args);
    await assert.rejects(signPayment(wallet, paymentFixture()), /payer wallet/);
    assert.equal(wallet.calls.includes('eth_signTypedData_v4'), false);
  }
});
test('wallet change or rejection during payment signing produces no submission payload', async () => {
  await assert.rejects(signPayment(paymentProvider({changed:true}), paymentFixture()), /wallet changed/);
  await assert.rejects(signPayment(paymentProvider({reject:true}), paymentFixture()), {code:4001});
});

function nativeFixture() {
  const usdc='0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d', contract=b;
  const p={chain_id:421614, from:a, asset:usdc, amount_atoms:'10000',
    purchase_id:'0x'+'cc'.repeat(32), release_id:'0x'+'dd'.repeat(32),
    report_sha256:'ee'.repeat(32), terms_sha256:'ff'.repeat(32),
    expires_at:Math.floor(Date.now()/1000)+60, broadcasts:0, x402_payment_required:false,
    signing_authority:'CUSTOMER_WALLET_ONLY'};
  const price=BigInt(p.amount_atoms).toString(16).padStart(64,'0');
  p.transactions=[{to:usdc,value:'0x0',data:'0x095ea7b3'+contract.slice(2).padStart(64,'0')+price},
    {to:contract,value:'0x0',data:'0x9e25f4a8'+p.release_id.slice(2)+p.report_sha256+p.terms_sha256+price+p.purchase_id.slice(2)}];
  return p;
}
function nativeProvider({allowance='0x2710', reject=false, missing=false, fail=false, account=a}={}) {
  const calls=[];
  return {calls,async request({method,params}) {
    calls.push({method,params});
    if(method==='eth_accounts') return [account];
    if(method==='eth_chainId') return '0x66eee';
    if(method==='eth_call') return allowance;
    if(method==='eth_sendTransaction') {
      if(reject) throw Object.assign(new Error('Rejected'),{code:4001});
      if(fail) throw new Error('Network disconnected');
      return missing ? null : '0x'+'aa'.repeat(32);
    }
    throw new Error('Unexpected wallet operation: '+method);
  }};
}
test('native DataPass approval sends exact allowance and purchase sends separately after allowance read',async()=>{
  const p=nativeFixture(),wallet=nativeProvider();
  await sendDataPassStep(wallet,p,b,0);
  assert.equal(wallet.calls.filter(c=>c.method==='eth_sendTransaction').length,1);
  assert.equal(wallet.calls.at(-1).params[0].data,p.transactions[0].data);
  wallet.calls.length=0;
  await sendDataPassStep(wallet,p,b,1);
  const read=wallet.calls.find(c=>c.method==='eth_call');
  assert.equal(read.params[0].data,'0xdd62ed3e'+a.slice(2).padStart(64,'0')+b.slice(2).padStart(64,'0'));
  assert.equal(wallet.calls.at(-1).params[0].data,p.transactions[1].data);
  assert.equal(wallet.calls.filter(c=>c.method==='eth_sendTransaction').length,1);
});
test('native DataPass rejects changed asset, ABI, version, price, expiry or double x402 payment before sending',async()=>{
  for(const change of [p=>{p.asset=a;},p=>{p.transactions[0].to=a;},p=>{p.transactions[1].data+='00';},
    p=>{p.report_sha256='11'.repeat(32);},p=>{p.amount_atoms='1000001';},p=>{p.expires_at=1;},
    p=>{delete p.expires_at;},p=>{p.x402_payment_required=true;}]) {
    const p=nativeFixture(),wallet=nativeProvider();change(p);
    await assert.rejects(sendDataPassStep(wallet,p,b,0));
    assert.equal(wallet.calls.some(c=>c.method==='eth_sendTransaction'),false);
  }
});
test('unconfirmed allowance or changed buyer prevents license purchase',async()=>{
  for(const args of [{allowance:'0x0'},{account:b}]) {
    const wallet=nativeProvider(args);
    await assert.rejects(sendDataPassStep(wallet,nativeFixture(),b,1));
    assert.equal(wallet.calls.some(c=>c.method==='eth_sendTransaction'),false);
  }
});
test('wallet rejection is retryable but lost native submission or missing hash remains uncertain',async()=>{
  for(const args of [{reject:true},{missing:true},{fail:true}]) {
    await assert.rejects(sendDataPassStep(nativeProvider(args),nativeFixture(),b,0),error=>{
      assert.equal(error.submission_uncertain,!args.reject);return true;
    });
  }
});
