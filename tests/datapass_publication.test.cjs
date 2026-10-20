'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {sendDataPassRelease,publicationData,dataPassStorageKey}=require('../web/wallet.js');
// Python eth_abi encoded this synthetic fixture; JS must independently agree.
const fixture=require('./fixtures/datapass-publication.json');
Date.now=()=>fixture.now*1000;
const plan=()=>structuredClone(fixture.plan);
function provider({chain='0xa4b1',changed=false,fail=false,reject=false,simulateFail=false}={}) {
  const calls=[];let accounts=0;
  return {calls,async request({method,params}) {
    calls.push({method,params});
    if(method==='eth_accounts') return [changed && accounts++ ? '0x'+'4'.repeat(40):fixture.plan.from];
    if(method==='eth_chainId') return chain;
    if(method==='eth_estimateGas') {if(simulateFail)throw new Error('revert');return '0x40000';}
    if(method==='eth_sendTransaction') {
      if(reject)throw Object.assign(new Error('rejected'),{code:4001});
      if(fail)throw new Error('connection lost');
      return '0x'+'1'.repeat(64);
    }
    throw new Error(method);
  }};
}
test('publication ABI matches the Python encoder; persists uncertainty before wallet request',async()=>{
  const p=plan(),w=provider(),records=[];
  assert.equal(publicationData(p),p.data);
  const result=await sendDataPassRelease(w,p,p.to,v=>{
    if(v.status==='UNKNOWN_RECONCILE_ONLY')assert(!w.calls.some(c=>c.method==='eth_sendTransaction'));
    records.push(v);
  });
  assert.deepEqual(records.map(r=>r.status),['UNKNOWN_RECONCILE_ONLY','SUBMITTED']);
  assert.equal(result.tx_hash,records[1].tx_hash);
  assert.equal(w.calls.at(-1).params[0].data,p.data);
  assert.equal(w.calls.at(-1).params[0].value,'0x0');
});
test('altered seller, token, price, contents, metadata, terms and expiry fail before wallet submission',async()=>{
  for(const change of [p=>p.from='0x'+'5'.repeat(40),p=>p.asset='0x'+'6'.repeat(40),
    p=>p.price_atoms='20000',p=>p.report_sha256='1'.repeat(64),p=>p.terms_sha256='2'.repeat(64),
    p=>p.metadata_uri='https://evil.example',p=>p.expires_at=1,p=>p.data+='00',p=>p.value='0x1',
    p=>p.duration_seconds=3600,p=>p.transferable=false]) {
    const p=plan(),w=provider();change(p);
    await assert.rejects(sendDataPassRelease(w,p,p.to,()=>{}));
    assert.equal(w.calls.length,0);
  }
});
test('wrong network, changed account or reverted simulation cannot send',async()=>{
  for(const options of [{chain:'0x66eee'},{changed:true},{simulateFail:true}]) {
    const w=provider(options),p=plan();
    await assert.rejects(sendDataPassRelease(w,p,p.to,()=>{}));
    assert(!w.calls.some(c=>c.method==='eth_sendTransaction'));
  }
});
test('disconnect keeps reconciliation-only record; explicit rejection is retryable',async()=>{
  for(const options of [{fail:true},{reject:true}]) {
    const w=provider(options),p=plan(),records=[];
    await assert.rejects(sendDataPassRelease(w,p,p.to,v=>records.push(v)),e=>{
      assert.equal(e.submission_uncertain,!!options.fail);return true;
    });
    assert.equal(records.at(-1).status,options.fail?'UNKNOWN_RECONCILE_ONLY':'USER_REJECTED');
  }
});
test('journal persistence failure prevents any wallet submission',async()=>{
  const p=plan(),w=provider();
  await assert.rejects(sendDataPassRelease(w,p,p.to,()=>{throw new Error('storage full');}));
  assert(!w.calls.some(c=>c.method==='eth_sendTransaction'));
});
test('DataPass journals isolate network, contract and buyer',()=>{
  const p=plan(),key=dataPassStorageKey(p.from,42161,p.to);
  assert.notEqual(key,dataPassStorageKey(p.from,421614,p.to));
  assert.notEqual(key,dataPassStorageKey(p.from,42161,'0x'+'4'.repeat(40)));
  assert.notEqual(key,dataPassStorageKey('0x'+'5'.repeat(40),42161,p.to));
});
