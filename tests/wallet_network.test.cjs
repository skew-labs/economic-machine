const {test}=require('node:test');const assert=require('node:assert/strict');
const W=require('../web/wallet.js');
const address='0x'+'1'.repeat(40);
function wallet(chain=1,unknown=false){const calls=[];return{calls,request:async x=>{calls.push(x);if(x.method==='eth_requestAccounts'||x.method==='eth_accounts')return[address];if(x.method==='eth_chainId')return '0x'+chain.toString(16);if(x.method==='wallet_switchEthereumChain'){if(unknown){unknown=false;throw {code:4902};}chain=42161;}if(x.method==='wallet_addEthereumChain')chain=42161;}};}
test('MetaMask wrong network switches and resumes with same owner',async()=>{const p=wallet();assert.equal((await W.ensureArbitrum(p)).chain_id,42161);assert(p.calls.some(c=>c.method==='wallet_switchEthereumChain'));assert(!p.calls.some(c=>c.method==='personal_sign'));});
test('missing network adds only fixed Arbitrum chain',async()=>{const p=wallet(1,true);await W.ensureArbitrum(p);const add=p.calls.find(c=>c.method==='wallet_addEthereumChain');assert.equal(add.params[0].chainId,'0xa4b1');assert.equal(add.params[0].rpcUrls[0],'https://arb1.arbitrum.io/rpc');});
test('network rejection never proceeds to signing',async()=>{const p=wallet();p.request=async x=>{if(x.method==='eth_requestAccounts')return[address];if(x.method==='eth_chainId')return'0x1';throw {code:4001};};await assert.rejects(W.ensureArbitrum(p),e=>e.code===4001);});
test('pending request and rejection have actionable errors',()=>{assert.match(W.connectionError({code:-32002}),/already open/);assert.match(W.connectionError({code:4001}),/Nothing was submitted/);});
test('account changed during switching is rejected',async()=>{let first=true;const p={request:async x=>{if(x.method==='eth_requestAccounts')return[address];if(x.method==='eth_accounts')return['0x'+'2'.repeat(40)];if(x.method==='eth_chainId')return'0xa4b1';}};await assert.rejects(W.ensureArbitrum(p),/account changed/);});
test('provider selection survives changed UUID but rejects ambiguous metadata',()=>{
 const wallet={id:'new-uuid',rdns:'io.metamask',name:'MetaMask'};
 const hint={id:'previous-uuid',rdns:'io.metamask',name:'MetaMask'};
 assert.equal(W.rememberedProvider([wallet],hint),wallet);
 assert.equal(W.rememberedProvider([wallet,{...wallet,id:'other-uuid'}],hint),null);
 assert.equal(W.rememberedProvider([wallet],{...hint,rdns:'app.other'}),null);
 assert.equal(W.rememberedProvider([wallet],null),null);
 assert.equal(W.rememberedProvider([wallet],{id:'new-uuid'}),wallet);
});
