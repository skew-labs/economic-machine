(function(){
  'use strict';
  const $=id=>document.getElementById(id), candidate=new URLSearchParams(location.search).get('fuel');
  const fuelId=/^fuel-[0-9a-f]{24}$/.test(candidate||'')?candidate:null, KEY='skew_gas_swap_v1'+(fuelId||'');
  let provider,owner,intent,permitSignature,busy=false;
  const text=value=>{$('status').textContent=value;};
  function stored(){try{return JSON.parse(localStorage.getItem(KEY)||'null');}catch{return {status:'UNKNOWN_RECONCILE_ONLY'};}}
  function persist(value){localStorage.setItem(KEY,JSON.stringify(value));$('tracking').hidden=false;}
  function held(){const x=stored();return x&&!['USER_REJECTED','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(x.status);}
  function controls(){ $('quote').disabled=busy||!provider||held();$('permit').disabled=busy;$('trade').disabled=busy;$('check').disabled=busy;$('amount').disabled=busy||!!fuelId||held();}
  $('amount').oninput=()=>{if(!busy&&!held()&&!fuelId){intent=null;permitSignature=null;$('review').hidden=true;$('permit').hidden=true;$('trade').hidden=true;}};
  async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin'});const p=await r.json();if(!r.ok)throw new Error(p.error||p.detail||'Request failed.');return p;}
  async function request(action,body){
    if(!fuelId)return api('/commerce/swap-api/'+action,body);
    const result=await api('/commerce/api/engine/fuel/requests/'+fuelId+'/'+(action==='status'?'reconcile':action),body.signature?{signature:body.signature}:{});
    return action==='status'?result.settlement:result;
  }
  function rows(values){$('details').replaceChildren();for(const[k,v]of values){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;$('details').append(dt,dd);}$('review').hidden=false;}
  async function run(fn){if(busy)return;busy=true;controls();try{await fn();}catch(e){text(e.message||'Wallet request failed.');}finally{busy=false;controls();}}
  WalletBridge.subscribe(wallets=>{const box=$('wallets');box.replaceChildren();for(const wallet of wallets){const b=document.createElement('button');b.textContent='Connect '+wallet.name;b.onclick=()=>run(async()=>{const accounts=await wallet.provider.request({method:'eth_requestAccounts'});if(Number(await wallet.provider.request({method:'eth_chainId'}))!==42161)throw new Error('Select Arbitrum One in your wallet, then connect again.');provider=wallet.provider;owner=accounts[0];intent=null;$('account').textContent=owner;text('Choose an amount to get a live quote.');
      if(fuelId){
        await WalletBridge.signIn(provider,(path,body)=>api('/commerce'+path,body),text);
        const bound=await api('/commerce/api/engine/fuel/requests/'+fuelId);intent=bound.swap;
        $('amount').value=String(Number(intent.amount_atoms)/1e6);$('quote').hidden=true;
        rows([['Reserved USDC',String(bound.held_atoms/1e6)],['Gas acquisition',String(Number(intent.amount_atoms)/1e6)+' USDC'],['Parent purchase',String(bound.request.purchase_atoms/1e6)+' USDC'],['Policy','Shared engine budget']]);
        if(['SUBMITTED','UNKNOWN_RECONCILE_ONLY','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(intent.status)){
          persist({id:intent.id,owner,order_uid:intent.order_uid,status:intent.status,valid_to:intent.valid_to});$('trade').hidden=true;$('permit').hidden=true;text('Existing fuel request loaded. Check the original settlement.');return;
        }
        SkewSwapWallet.validBase(intent,owner,intent.amount_atoms);
        if(intent.order){permitSignature=SkewSwapCrypto.signatureFromHook(intent.app_data);$('trade').hidden=false;$('permit').hidden=true;}else{$('permit').hidden=false;}
        text('Agent request loaded. Your wallet still controls both spending signatures.');
      }
    });box.append(b);}});
  $('quote').onclick=()=>run(async()=>{
    if(held())throw new Error('Reconcile the existing order first.');
    const raw=$('amount').value.trim();if(!/^[1-3](?:\.\d{1,6})?$/.test(raw))throw new Error('Enter 1 to 3 USDC.');
    const [whole,fraction='']=raw.split('.'),atoms=(BigInt(whole)*1000000n+BigInt(fraction.padEnd(6,'0'))).toString();
    text('Checking two Arbitrum RPCs and requesting a CoW route…');
    intent=await request('quote',{owner,amount_atoms:atoms});SkewSwapWallet.validBase(intent,owner,atoms);
    rows([['You spend',`${Number(atoms)/1e6} USDC`],['Estimated ETH',`${(Number(intent.preview_buy_wei)/1e18).toFixed(8)} ETH`],['Estimated routing cost',`${Number(intent.preview_fee_atoms)/1e6} USDC`],['Network','Arbitrum One']]);
    $('permit').hidden=false;$('trade').hidden=true;text('Quote received. First sign a short-lived approval for exactly this USDC amount.');
  });
  $('permit').onclick=()=>run(async()=>{
    text('Review the bounded USDC permit in your wallet.');
    permitSignature=await SkewSwapWallet.signPermit(provider,intent,owner,intent.amount_atoms,SkewSwapCrypto);
    text('Checking the signed permit and simulating the final route…');
    intent=await request('order',{id:intent.id,signature:permitSignature});
    SkewSwapWallet.validateOrder(intent,owner,intent.amount_atoms,permitSignature,SkewSwapCrypto);
    rows([['Total USDC limit',`${Number(intent.amount_atoms)/1e6} USDC`],['Minimum you receive',`${(Number(intent.minimum_buy_wei)/1e18).toFixed(8)} ETH`],['Recipient',owner],['Final route','Simulation verified']]);
    $('permit').hidden=true;$('trade').hidden=false;text('Review the minimum ETH above. The next signature authorizes and submits this swap.');
  });
  $('trade').onclick=()=>run(async()=>{
    text('Review the trade in your wallet.');
    const signature=await SkewSwapWallet.signOrder(provider,intent,owner,intent.amount_atoms,permitSignature,SkewSwapCrypto,persist);
    const result=await request('submit',{id:intent.id,signature});
    persist({...stored(),status:result.status});$('trade').hidden=true;text('Order recorded. Check settlement; do not submit a replacement.');
  });
  $('check').onclick=()=>run(async()=>{
    const previous=stored();if(!previous?.id)throw new Error('No recoverable swap ID. Keep this page and contact the operator.');
    const result=await request('status',{id:previous.id});
    $('tracking-text').textContent=result.status;
    $('links').replaceChildren();for(const tx of result.tx_hashes||[]){if(!/^0x[0-9a-f]{64}$/i.test(tx))continue;const a=document.createElement('a');a.href='https://arbiscan.io/tx/'+tx;a.target='_blank';a.rel='noopener noreferrer';a.textContent='View settlement transaction';$('links').append(a);}
    if(result.chain_verified){persist({...previous,status:'FILLED_FINALIZED'});text('Swap finalized. Two RPCs verified the trade and ETH received by your wallet.');}
    else if(result.safe_to_retry){persist({...previous,status:'EXPIRED_UNFILLED'});intent=null;text('Order expired without a fill. A fresh quote is now allowed.');}
    else text('Status: '+result.status+'. Keep tracking this order; no replacement has been sent.');
  });
  if(stored()){$('tracking').hidden=false;text('A previous attempt is saved. Check settlement before another swap.');}controls();
})();
