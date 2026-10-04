(async function(){
  'use strict';
  const status=document.getElementById('status'), receipt=document.getElementById('receipt'), button=document.getElementById('launch');
  let review, provider;
  const key=()=>`skew_launch_${review.owner}_${review.transaction.nonce}`;
  function held(){try{return JSON.parse(localStorage.getItem(key())||'null');}catch{return {status:'UNKNOWN_RECONCILE_ONLY'};}}
  function ready(){const h=review && held();button.disabled=!review || !provider || review.expires_at<=Date.now()/1000 || (h && h.status!=='USER_REJECTED');if(h)receipt.textContent=JSON.stringify(h,null,2);}
  async function load(){
    button.disabled=true;
    const response=await fetch('/commerce/demo/mainnet-launch',{cache:'no-store',credentials:'omit'});
    if(!response.ok)throw new Error('No reviewed mainnet launch is published yet.');
    review=await response.json(); const dl=document.getElementById('review');dl.replaceChildren();
    for(const [label,value] of [['Network','Arbitrum One · 42161'],['Owner',review.owner],['Maximum deployment fee',`${Number(review.maximum_gas_wei)/1e18} ETH`],['SKEW supply','0 initial · 160,000 maximum · 1 per accepted job'],['Initcode SHA-256',review.initcode_sha256]]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;dl.append(dt,dd);
    }
    status.textContent=review.expires_at<=Date.now()/1000?'Review expired. The operator must refresh the on-chain quote.':review.status==='NEEDS_NATIVE_ETH'?'The last quote found no ETH for gas. The wallet is checked again before signing.':'Select the reviewed MetaMask account to continue.';ready();
  }
  WalletBridge.subscribe(wallets=>{
    const box=document.getElementById('wallets');box.replaceChildren();
    for(const wallet of wallets){const b=document.createElement('button');b.textContent=`Connect ${wallet.name}`;b.onclick=async()=>{try{await wallet.provider.request({method:'eth_requestAccounts'});provider=wallet.provider;status.textContent='Wallet selected. Deployment requires your review and signature.';ready();}catch(e){status.textContent=e.message;}};box.append(b);}
  });
  document.getElementById('refresh').onclick=()=>load().catch(e=>status.textContent=e.message);
  button.onclick=async()=>{
    button.disabled=true;
    try{
      const h=held();if(h && h.status!=='USER_REJECTED')throw new Error('An earlier attempt must be reconciled before any retry.');
      const sha=async hex=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',Uint8Array.from(hex.slice(2).match(/../g),s=>parseInt(s,16))))).map(b=>b.toString(16).padStart(2,'0')).join('');
      const hash=await SkewLaunchWallet.sendLaunch(provider,review,value=>{localStorage.setItem(key(),JSON.stringify(value));receipt.textContent=JSON.stringify(value,null,2);},sha);
      status.textContent=`Submitted ${hash}. Independent contract and finality verification is still required.`;
    }catch(e){status.textContent=e.message;}ready();
  };
  try{await load();}catch(e){status.textContent=e.message;}
})();
