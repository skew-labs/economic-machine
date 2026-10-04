(function(root) {
  'use strict';
  const ADDRESS=/^0x[0-9a-fA-F]{40}$/, HASH=/^0x[0-9a-fA-F]{64}$/, HEX=/^0x[0-9a-fA-F]+$/;
  function eth(value) {
    const atoms=BigInt(value), whole=atoms/1000000000000000000n;
    const fraction=(atoms%1000000000000000000n).toString().padStart(18,'0').replace(/0+$/,'');
    return whole.toString()+(fraction?'.'+fraction:'');
  }
  function quantity(value, field) {
    // Some MetaMask versions return the pending nonce as a JS number, unlike
    // the hexadecimal quantities returned by the chain RPC. Never round it.
    if (['pending_nonce','latest_nonce'].includes(field) && typeof value==='number'
        && Number.isSafeInteger(value) && value>=0) return '0x'+BigInt(value).toString(16);
    if (typeof value==='string' && HEX.test(value)) return value;
    const detail=typeof value==='string'?JSON.stringify(value.slice(0,64)):
      typeof value==='number'||value===null?String(value):typeof value;
    throw new Error(`Wallet RPC returned an invalid ${field} (${detail}). No transaction was sent. Reconnect or check the wallet RPC.`);
  }
  async function inspectWallet(provider, owner) {
    if (!provider || typeof provider.request!=='function' || !ADDRESS.test(owner || ''))
      throw new Error('Connect the reviewed owner wallet first.');
    const accounts=await provider.request({method:'eth_accounts',params:[]});
    const chain=await provider.request({method:'eth_chainId',params:[]});
    if (Number(chain)!==42161 || accounts?.[0]?.toLowerCase()!==owner.toLowerCase())
      throw new Error('Select the reviewed owner wallet on Arbitrum One.');
    const methods=[['balance','eth_getBalance',[owner,'latest']],
      ['pending_nonce','eth_getTransactionCount',[owner,'pending']],
      ['latest_nonce','eth_getTransactionCount',[owner,'latest']],['gas_price','eth_gasPrice',[]]];
    const values=await Promise.all(methods.map(async ([field,method,params])=>{
      const value=await provider.request({method,params});
      return [field,quantity(value,field)];
    }));
    return {owner,chain_id:42161,...Object.fromEntries(values)};
  }
  async function sendLaunch(provider, review, persist, sha256) {
    let attempted=false;
    try {
      const tx=review?.transaction, now=Math.floor(Date.now()/1000);
      if (!tx || review.schema!=='skew-launch-review-1' || review.chain_id!==42161 ||
          !ADDRESS.test(review.owner) || tx.from?.toLowerCase()!==review.owner.toLowerCase() ||
          tx.chainId!=='0xa4b1' || tx.value!=='0x0' || tx.to!==undefined ||
          !/^0x(?:[0-9a-fA-F]{2}){1,49152}$/.test(tx.data) ||
          ![tx.gas,tx.gasPrice,tx.nonce].every(x=>HEX.test(x)) ||
          !/^[0-9a-f]{64}$/.test(review.initcode_sha256) ||
          !Number.isSafeInteger(review.expires_at) || review.expires_at<=now ||
          !/^\d+$/.test(review.maximum_gas_wei) || !/^\d+$/.test(review.owner_cap_wei) ||
          BigInt(review.owner_cap_wei)>500000000000000n ||
          BigInt(tx.gas)*BigInt(tx.gasPrice)!==BigInt(review.maximum_gas_wei) ||
          BigInt(review.maximum_gas_wei)>BigInt(review.owner_cap_wei) || BigInt(tx.gasPrice)<=0n ||
          await sha256(tx.data)!==review.initcode_sha256)
        throw new Error('A current source-bound Arbitrum deployment review is required.');
      const {balance,pending_nonce:pending,latest_nonce:latest,gas_price:price}=await inspectWallet(provider,review.owner);
      if (BigInt(balance)<BigInt(review.maximum_gas_wei))
        throw new Error(`Wallet RPC reports ${eth(balance)} ETH; this deployment needs up to ${eth(review.maximum_gas_wei)} ETH. If the verified balance is higher, refresh the wallet RPC before retrying. No transaction was sent.`);
      if (BigInt(pending)!==BigInt(tx.nonce) || BigInt(latest)!==BigInt(tx.nonce) || BigInt(price)>BigInt(tx.gasPrice))
        throw new Error('Nonce or fees changed. Refresh the deployment review.');
      const estimate=await provider.request({method:'eth_estimateGas',params:[tx]});
      if (!HEX.test(estimate) || BigInt(estimate)>BigInt(tx.gas)) throw new Error('Deployment estimate exceeds the reviewed gas limit.');
      const current=await provider.request({method:'eth_accounts'});
      if (current[0]?.toLowerCase()!==review.owner.toLowerCase() || Number(await provider.request({method:'eth_chainId'}))!==42161)
        throw new Error('Wallet changed before submission.');
      // Persist before requesting a wallet submission. Missing hashes never authorize a retry.
      persist({status:'UNKNOWN_RECONCILE_ONLY',owner:review.owner,nonce:tx.nonce,initcode_sha256:review.initcode_sha256});
      attempted=true;
      const hash=await provider.request({method:'eth_sendTransaction',params:[tx]});
      if (!HASH.test(hash || '')) throw new Error('No transaction hash returned. Reconcile this wallet before retrying.');
      persist({status:'SUBMITTED',owner:review.owner,nonce:tx.nonce,tx_hash:hash,initcode_sha256:review.initcode_sha256});
      return hash;
    } catch(cause) {
      const error=cause instanceof Error?cause:new Error('Wallet action failed.');
      if(attempted && Number(error.code)===4001) persist({status:'USER_REJECTED'});
      error.submission_uncertain=attempted && Number(error.code)!==4001;
      throw error;
    }
  }
  if (typeof module!=='undefined' && module.exports) {module.exports={sendLaunch,inspectWallet,formatEth:eth};return;}
  root.SkewLaunchWallet={sendLaunch,inspectWallet,formatEth:eth};
})(typeof window!=='undefined'?window:globalThis);
