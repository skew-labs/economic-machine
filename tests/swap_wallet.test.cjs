const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {Wallet,TypedDataEncoder}=require('../tools/gas-browser/node_modules/ethers');
const gate=require('../web/swap-wallet.js');
const cryptoScope={};vm.runInNewContext(fs.readFileSync(require.resolve('../web/swap-crypto.js'),'utf8'),cryptoScope);
const crypto=cryptoScope.SkewSwapCrypto;
const USD='0xaf88d065e77c8cc2239327c5edb3a432268e5831',ETH='0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',RELAYER='0xc92e8bdf79f0507f65a392b0ab4667716bfe0110';
const wallet=new Wallet('0x'+'1'.padStart(64,'0'));
const fields=list=>list.map(([name,type])=>({name,type}));
const domain=fields([['name','string'],['version','string'],['chainId','uint256'],['verifyingContract','address']]);
async function fixture(amount='2000000'){
 const now=Math.floor(Date.now()/1000),owner=wallet.address,valid=now+600;
 const p={schema:'skew-gas-swap-1',id:'12'.repeat(24),chain_id:42161,owner,amount_atoms:amount,valid_to:valid};
 const pt=fields([['owner','address'],['spender','address'],['value','uint256'],['nonce','uint256'],['deadline','uint256']]);
 p.permit={types:{EIP712Domain:domain,Permit:pt},primaryType:'Permit',domain:{name:'USD Coin',version:'2',chainId:42161,verifyingContract:USD},message:{owner,spender:RELAYER,value:p.amount_atoms,nonce:'0',deadline:valid}};
 const sig=await wallet.signTypedData(p.permit.domain,{Permit:pt},p.permit.message);
 p.verified_quote=true;p.quote_expires=now+60;p.quoted_buy_wei=(BigInt(amount)*370000000n).toString();p.estimated_fee_atoms='10000';p.protocol_fee_bps='2';p.minimum_buy_wei=(BigInt(p.quoted_buy_wei)*9998n/10000n*9950n/10000n).toString();
 p.app_data=JSON.stringify({appCode:'SKEW Gas Router',version:'1.3.0',metadata:{orderClass:{orderClass:'market'},quote:{slippageBips:50},hooks:{pre:[{target:USD,callData:crypto.permitCall(p.permit,sig),gasLimit:'80000'}],post:[]}}});
 const ot=fields([['sellToken','address'],['buyToken','address'],['receiver','address'],['sellAmount','uint256'],['buyAmount','uint256'],['validTo','uint32'],['appData','bytes32'],['feeAmount','uint256'],['kind','string'],['partiallyFillable','bool'],['sellTokenBalance','string'],['buyTokenBalance','string']]);
 p.order={types:{EIP712Domain:domain,Order:ot},primaryType:'Order',domain:{name:'Gnosis Protocol',version:'v2',chainId:42161,verifyingContract:'0x9008d19f58aabd9ed0d60971565aa8510560ab41'},message:{sellToken:USD,buyToken:ETH,receiver:owner,sellAmount:p.amount_atoms,buyAmount:p.minimum_buy_wei,validTo:valid,appData:crypto.hashText(p.app_data),feeAmount:'0',kind:'sell',partiallyFillable:false,sellTokenBalance:'erc20',buyTokenBalance:'erc20'}};
 p.order_uid=crypto.typedHash(p.order)+owner.slice(2).toLowerCase()+valid.toString(16).padStart(8,'0');
 let count=0;const provider={request:async({method,params})=>{if(method==='eth_accounts')return[owner];if(method==='eth_chainId')return'0xa4b1';if(method==='eth_call')return'0x0';if(method==='eth_signTypedData_v4'){count++;const t=JSON.parse(params[1]),types={...t.types};delete types.EIP712Domain;return wallet.signTypedData(t.domain,types,t.message);}throw Error('Unexpected '+method);}};
 return{p,sig,provider,getCount:()=>count};
}
test('real EIP-712 signatures and permit ABI match the bounded review',async()=>{const{p,sig,provider,getCount}=await fixture();gate.validateOrder(p,p.owner,'2000000',sig,crypto);assert.equal(crypto.signatureFromHook(p.app_data),sig);const signed=await gate.signPermit(provider,p,p.owner,'2000000',crypto);assert.equal(signed,sig);const states=[];const orderSig=await gate.signOrder(provider,p,p.owner,'2000000',sig,crypto,s=>states.push(s.status));assert.equal(crypto.signer(p.order,orderSig),p.owner);assert.deepEqual(states,['SIGNATURE_PENDING','UNKNOWN_RECONCILE_ONLY']);assert.equal(getCount(),2);assert.equal(crypto.typedHash(p.order),TypedDataEncoder.hash(p.order.domain,{Order:p.order.types.Order},p.order.message));});
test('receiver amount spender hooks chain expiry and domain tampering never signs',async()=>{const f=await fixture();const changes=[p=>p.order.message.receiver=RELAYER,p=>p.amount_atoms='3000001',p=>p.permit.message.value='99999999',p=>p.permit.message.spender=USD,p=>p.permit.domain.chainId=1,p=>p.order.domain.verifyingContract=USD,p=>p.order_uid='0x'+'ab'.repeat(56),p=>p.app_data=p.app_data.replace('80000','1000000'),p=>p.quote_expires=0,p=>p.order.message.buyToken=USD,p=>p.verified_quote=false];for(const mutate of changes){const p=structuredClone(f.p);mutate(p);await assert.rejects(gate.signOrder(f.provider,p,p.owner,'2000000',f.sig,crypto,()=>{}));}assert.equal(f.getCount(),0);});
test('storage failure prevents requesting an order signature',async()=>{const f=await fixture();await assert.rejects(gate.signOrder(f.provider,f.p,f.p.owner,'2000000',f.sig,crypto,()=>{throw Error('storage full');}));assert.equal(f.getCount(),0);});
test('wallet nonce and network changes block permit signatures',async()=>{const f=await fixture();for(const method of ['eth_call','eth_chainId']){const provider={request:p=>p.method===method?Promise.resolve('0x1'):f.provider.request(p)};await assert.rejects(gate.signPermit(provider,f.p,f.p.owner,'2000000',crypto));}assert.equal(f.getCount(),0);});
test('decimal amounts preserve every atom without a three-dollar cap',()=>{
 for(const [input,atoms] of [['0.000001','1'],['0.5','500000'],['3.1','3100000'],['10','10000000'],['9007199254.740993','9007199254740993']]){
  assert.equal(gate.usdcAtoms(input),atoms);assert.equal(gate.formatUnits(atoms),input);
 }
 const max=((1n<<256n)-1n).toString();assert.equal(gate.usdcAtoms(gate.formatUnits(max)),max);
 for(const input of ['0','-1','01','1e3','1.0000001','Infinity',' 10',3,gate.formatUnits((1n<<256n).toString())])assert.throws(()=>gate.usdcAtoms(input));
});
test('larger amounts retain exact permit and order signature binding',async()=>{
 for(const amount of ['10000000','9007199254740993']){
  const{p,sig,provider}=await fixture(amount);
  assert.equal(p.permit.message.value,amount);assert.equal(p.order.message.sellAmount,amount);
  assert.equal(await gate.signPermit(provider,p,p.owner,amount,crypto),sig);
  const signed=await gate.signOrder(provider,p,p.owner,amount,sig,crypto,()=>{});
  assert.equal(crypto.signer(p.order,signed),p.owner);
  await assert.rejects(gate.signOrder(provider,p,p.owner,(BigInt(amount)-1n).toString(),sig,crypto,()=>{}));
 }
});
