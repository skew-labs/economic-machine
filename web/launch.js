(async function(){
  'use strict';
  const status=document.getElementById('status'), receipt=document.getElementById('receipt'), button=document.getElementById('launch');
  let review, provider, deployed=false;
  const key=()=>`skew_launch_${review.owner}_${review.transaction.nonce}`;
  function held(){try{return JSON.parse(localStorage.getItem(key())||'null');}catch{return {status:'UNKNOWN_RECONCILE_ONLY'};}}
  function ready(){if(deployed){button.disabled=true;document.getElementById('check-wallet').disabled=true;return;}const h=review && held();button.disabled=!review || !provider || review.expires_at<=Date.now()/1000 || (h && h.status!=='USER_REJECTED');document.getElementById('check-wallet').disabled=!review||!provider;if(h)receipt.textContent=JSON.stringify(h,null,2);}
  async function checkWallet(){
    const out=document.getElementById('wallet-check');
    out.textContent='Reading wallet RPC. No signature or transaction is requested.';
    const result=await SkewLaunchWallet.inspectWallet(provider,review.owner);
    const observations=review.observations || [];
    const observed=observations.map(row=>/^\d+$/.test(row.eth_wei)?SkewLaunchWallet.formatEth(row.eth_wei):'unavailable');
    out.textContent=`Wallet RPC: ${SkewLaunchWallet.formatEth(result.balance)} ETH. Reviewed RPC balances: ${observed.join(' / ')} ETH (at ${new Date(review.created_at*1000).toLocaleTimeString()}). Wallet nonce: ${BigInt(result.latest_nonce)} / pending ${BigInt(result.pending_nonce)}. Gas price: ${BigInt(result.gas_price)} wei. Read only; no transaction sent.`;
  }
  async function load(){
    button.disabled=true;
    // Reconcile the published deployment before offering any new wallet action.
    const marketResponse=await fetch('/commerce/market/skew',{cache:'no-store',credentials:'omit'});
    if(!marketResponse.ok)throw new Error('Cannot check the existing deployment. No new deployment is enabled.');
    const market=await marketResponse.json(), publication=market.deployment;
    if(publication?.status==='MAINNET_DEPLOYED_FINALIZED' && publication.chain_id===42161){
      deployed=true;review=null;document.getElementById('wallets').hidden=true;
      document.getElementById('check-wallet').hidden=true;button.hidden=true;
      document.getElementById('refresh').textContent='Refresh deployment';
      status.textContent='Deployed and finalized on Arbitrum One. Receipt, runtime code and contract configuration were reconciled through two RPCs. No new signature is needed.';
      const dl=document.getElementById('review');dl.replaceChildren();
      for(const [label,value] of Object.entries(publication.contracts)){
        const dt=document.createElement('dt'),dd=document.createElement('dd'),a=document.createElement('a');dt.textContent=label;a.textContent=value;a.href='https://arbiscan.io/address/'+value;a.target='_blank';a.rel='noopener noreferrer';dd.append(a);dl.append(dt,dd);
      }
      const dt=document.createElement('dt'),dd=document.createElement('dd'),a=document.createElement('a');dt.textContent='Deployment transaction';a.textContent=publication.transaction_hash;a.href='https://arbiscan.io/tx/'+publication.transaction_hash;a.target='_blank';a.rel='noopener noreferrer';dd.append(a);dl.append(dt,dd);
      receipt.textContent=JSON.stringify({status:publication.status,chain_id:publication.chain_id,transaction_hash:publication.transaction_hash,token:publication.token,verification:publication.verification,scope:publication.scope},null,2);ready();return;
    }
    const response=await fetch('/commerce/demo/mainnet-launch',{cache:'no-store',credentials:'omit'});
    if(!response.ok)throw new Error('No reviewed mainnet launch is published yet.');
    review=await response.json(); const dl=document.getElementById('review');dl.replaceChildren();
    for(const [label,value] of [['Network','Arbitrum One · 42161'],['Owner',review.owner],['Maximum deployment fee',`${Number(review.maximum_gas_wei)/1e18} ETH`],['SKEW supply','0 initial · 160,000 maximum · 1 per accepted job'],['Initcode SHA-256',review.initcode_sha256]]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;dl.append(dt,dd);
    }
    status.textContent=review.expires_at<=Date.now()/1000?'Review expired. The operator must refresh the on-chain quote.':review.status==='NEEDS_NATIVE_ETH'?'The last quote found no ETH for gas. The wallet is checked again before signing.':provider?'Wallet selected. Deployment requires your review and signature.':'Select the reviewed MetaMask account to continue.';ready();
  }
  WalletBridge.subscribe(wallets=>{
    if(deployed)return;
    const box=document.getElementById('wallets');box.replaceChildren();
    for(const wallet of wallets){const b=document.createElement('button');b.textContent=`Connect ${wallet.name}`;b.onclick=async()=>{try{await wallet.provider.request({method:'eth_requestAccounts'});provider=wallet.provider;status.textContent='Wallet selected. Deployment requires your review and signature.';ready();}catch(e){status.textContent=e.message;}};box.append(b);}
  });
  document.getElementById('refresh').onclick=()=>load().catch(e=>status.textContent=e.message);
  document.getElementById('check-wallet').onclick=()=>checkWallet().catch(e=>document.getElementById('wallet-check').textContent=e.message);
  button.onclick=async()=>{
    if(deployed)return;
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
