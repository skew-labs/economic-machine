const {test}=require('node:test');
const assert=require('node:assert/strict');
const {sendLaunch}=require('../web/launch-wallet.js');
const owner='0x'+'11'.repeat(20),hash='0x'+'ab'.repeat(32);
function fixture(){return {schema:'skew-launch-review-1',chain_id:42161,owner,
  transaction:{from:owner,chainId:'0xa4b1',value:'0x0',data:'0x60006000',nonce:'0x0',gas:'0x100',gasPrice:'0x10'},
  maximum_gas_wei:'4096',owner_cap_wei:'500000000000000',initcode_sha256:'a'.repeat(64),expires_at:Math.floor(Date.now()/1000)+100};}
function wallet({balance='0x1000',nonce='0x0',chain='0xa4b1',reject=false,lost=false}={}){
  const calls=[];return {calls,async request({method}){calls.push(method);
    if(method==='eth_accounts')return [owner];if(method==='eth_chainId')return chain;
    if(method==='eth_getBalance')return balance;if(method==='eth_getTransactionCount')return nonce;
    if(method==='eth_gasPrice')return '0x10';if(method==='eth_estimateGas')return '0x100';
    if(method==='eth_sendTransaction'){if(reject)throw Object.assign(new Error('Rejected'),{code:4001});if(lost)throw new Error('Disconnected');return hash;}
    throw new Error(method);}};
}
test('launch preflights owner chain balance fees and nonce before one exact deployment',async()=>{
  const w=wallet(),states=[];const got=await sendLaunch(w,fixture(),s=>states.push(s),async()=> 'a'.repeat(64));
  assert.equal(got,hash);assert.deepEqual(states.map(s=>s.status),['UNKNOWN_RECONCILE_ONLY','SUBMITTED']);
  assert.equal(w.calls.filter(x=>x==='eth_sendTransaction').length,1);
});
test('changed initcode gas cap owner nonce and insufficient gas cannot transmit',async()=>{
  for(const change of [r=>r.transaction.to=owner,r=>r.transaction.value='0x1',r=>r.maximum_gas_wei='1',
    r=>r.expires_at=1,r=>r.owner_cap_wei='500000000000001',r=>r.transaction.from='0x'+'22'.repeat(20),
    r=>r.initcode_sha256='b'.repeat(64)]){
    const r=fixture(),w=wallet();change(r);await assert.rejects(sendLaunch(w,r,()=>{},async()=> 'a'.repeat(64)));
    assert.equal(w.calls.includes('eth_sendTransaction'),false);
  }
  for(const options of [{balance:'0x0'},{nonce:'0x1'},{chain:'0x66eee'}]){
    const w=wallet(options);await assert.rejects(sendLaunch(w,fixture(),()=>{},async()=> 'a'.repeat(64)));
    assert.equal(w.calls.includes('eth_sendTransaction'),false);
  }
});
test('lost deployment remains unknown while explicit wallet rejection can be retried',async()=>{
  for(const options of [{lost:true},{reject:true}]){
    const states=[],w=wallet(options);
    await assert.rejects(sendLaunch(w,fixture(),s=>states.push(s),async()=> 'a'.repeat(64)),e=>e.submission_uncertain===!options.reject);
    assert.equal(states.at(-1).status,options.reject?'USER_REJECTED':'UNKNOWN_RECONCILE_ONLY');
  }
});
