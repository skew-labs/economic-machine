(function(root){
  'use strict';
  const USDC='0xaf88d065e77c8cc2239327c5edb3a432268e5831', ETH='0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
  const RELAYER='0xc92e8bdf79f0507f65a392b0ab4667716bfe0110', SETTLEMENT='0x9008d19f58aabd9ed0d60971565aa8510560ab41';
  const eq=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.toLowerCase()===b.toLowerCase();
  const fail=()=>{throw new Error('Swap differs from the reviewed amount, recipient or Arbitrum contracts.');};
  const fields=list=>list.map(([name,type])=>({name,type}));
  const domainFields=fields([['name','string'],['version','string'],['chainId','uint256'],['verifyingContract','address']]);
  const permitFields=fields([['owner','address'],['spender','address'],['value','uint256'],['nonce','uint256'],['deadline','uint256']]);
  const orderFields=fields([['sellToken','address'],['buyToken','address'],['receiver','address'],['sellAmount','uint256'],['buyAmount','uint256'],['validTo','uint32'],['appData','bytes32'],['feeAmount','uint256'],['kind','string'],['partiallyFillable','bool'],['sellTokenBalance','string'],['buyTokenBalance','string']]);
  function exact(a,b){if(JSON.stringify(a)!==JSON.stringify(b))fail();}
  function validBase(p,owner,amount){
    const now=Math.floor(Date.now()/1000);
    if(p.schema!=='skew-gas-swap-1'||p.chain_id!==42161||!eq(owner,p.owner)||
       !/^[0-9]{7}$/.test(p.amount_atoms)||p.amount_atoms!==String(amount)||BigInt(p.amount_atoms)<1000000n||BigInt(p.amount_atoms)>3000000n||
       !Number.isSafeInteger(p.valid_to)||p.valid_to<=now||p.valid_to>now+600||
       !/^[0-9a-f]{48}$/.test(p.id))fail();
    const t=p.permit;
    exact(t?.types,{EIP712Domain:domainFields,Permit:permitFields});
    exact(t?.domain,{name:'USD Coin',version:'2',chainId:42161,verifyingContract:USDC});
    if(t?.primaryType!=='Permit'||!/^\d{1,78}$/.test(t?.message?.nonce||''))fail();
    exact(t.message,{owner:p.owner,spender:RELAYER,value:p.amount_atoms,nonce:t.message.nonce,deadline:p.valid_to});
  }
  async function account(provider,owner){
    const accounts=await provider.request({method:'eth_accounts'});
    if(!eq(accounts[0],owner)||Number(await provider.request({method:'eth_chainId'}))!==42161)
      throw new Error('Select the reviewed account on Arbitrum One.');
  }
  async function signPermit(provider,p,owner,amount,crypto){
    validBase(p,owner,amount);await account(provider,owner);
    const data='0x7ecebe00'+owner.slice(2).toLowerCase().padStart(64,'0');
    const nonce=await provider.request({method:'eth_call',params:[{to:USDC,data},'latest']});
    if(!/^0x[0-9a-f]+$/i.test(nonce)||BigInt(nonce)!==BigInt(p.permit.message.nonce))throw new Error('USDC permit changed. Request a fresh quote.');
    const signature=await provider.request({method:'eth_signTypedData_v4',params:[owner,JSON.stringify(p.permit)]});
    await account(provider,owner);
    if(!/^0x[0-9a-f]{130}$/i.test(signature)||!eq(crypto.signer(p.permit,signature),owner))fail();
    return signature;
  }
  function validateOrder(p,owner,amount,permitSignature,crypto){
    validBase(p,owner,amount);
    if(p.verified_quote!==true||!Number.isSafeInteger(p.quote_expires)||p.quote_expires<=Date.now()/1000||
       !/^[0-9]{1,78}$/.test(p.quoted_buy_wei)||!/^\d{1,6}$/.test(p.estimated_fee_atoms)||BigInt(p.estimated_fee_atoms)>100000n||
       !/^\d{1,3}$/.test(String(p.protocol_fee_bps))||Number(p.protocol_fee_bps)>100)fail();
    const minimum=BigInt(p.quoted_buy_wei)*(10000n-BigInt(p.protocol_fee_bps))/10000n*9950n/10000n;
    if(minimum<=0n||p.minimum_buy_wei!==minimum.toString())fail();
    const app={appCode:'SKEW Gas Router',version:'1.3.0',metadata:{orderClass:{orderClass:'market'},quote:{slippageBips:50},hooks:{pre:[{target:USDC,callData:crypto.permitCall(p.permit,permitSignature),gasLimit:'80000'}],post:[]}}};
    exact(p.app_data,JSON.stringify(app));
    exact(p.order?.types,{EIP712Domain:domainFields,Order:orderFields});
    exact(p.order?.domain,{name:'Gnosis Protocol',version:'v2',chainId:42161,verifyingContract:SETTLEMENT});
    if(p.order.primaryType!=='Order')fail();
    exact(p.order.message,{sellToken:USDC,buyToken:ETH,receiver:p.owner,sellAmount:p.amount_atoms,buyAmount:p.minimum_buy_wei,
      validTo:p.valid_to,appData:crypto.hashText(p.app_data),feeAmount:'0',kind:'sell',partiallyFillable:false,sellTokenBalance:'erc20',buyTokenBalance:'erc20'});
    const uid=crypto.typedHash(p.order)+owner.slice(2).toLowerCase()+p.valid_to.toString(16).padStart(8,'0');
    if(p.order_uid!==uid)fail();
  }
  async function signOrder(provider,p,owner,amount,permitSignature,crypto,persist){
    validateOrder(p,owner,amount,permitSignature,crypto);await account(provider,owner);
    persist({id:p.id,owner,order_uid:p.order_uid,status:'SIGNATURE_PENDING',valid_to:p.valid_to});
    let signature;
    try{signature=await provider.request({method:'eth_signTypedData_v4',params:[owner,JSON.stringify(p.order)]});}
    catch(e){if(Number(e.code)===4001)persist({id:p.id,owner,status:'USER_REJECTED'});throw e;}
    await account(provider,owner);
    if(!/^0x[0-9a-f]{130}$/i.test(signature)||!eq(crypto.signer(p.order,signature),owner))fail();
    // A slow wallet review must not submit an expired quote.
    validateOrder(p,owner,amount,permitSignature,crypto);
    persist({id:p.id,owner,order_uid:p.order_uid,status:'UNKNOWN_RECONCILE_ONLY',valid_to:p.valid_to});
    return signature;
  }
  const api={signPermit,signOrder,validateOrder,validBase};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.SkewSwapWallet=api;
})(typeof window!=='undefined'?window:globalThis);
