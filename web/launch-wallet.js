(function(root) {
  'use strict';
  const ADDRESS=/^0x[0-9a-fA-F]{40}$/, HASH=/^0x[0-9a-fA-F]{64}$/, HEX=/^0x[0-9a-fA-F]+$/;
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
      const accounts=await provider.request({method:'eth_accounts'});
      const chain=await provider.request({method:'eth_chainId'});
      if (Number(chain)!==42161 || accounts[0]?.toLowerCase()!==review.owner.toLowerCase())
        throw new Error('Select the reviewed owner wallet on Arbitrum One.');
      const [balance,pending,latest,price]=await Promise.all([
        provider.request({method:'eth_getBalance',params:[review.owner,'latest']}),
        provider.request({method:'eth_getTransactionCount',params:[review.owner,'pending']}),
        provider.request({method:'eth_getTransactionCount',params:[review.owner,'latest']}),
        provider.request({method:'eth_gasPrice'})]);
      if (![balance,pending,latest,price].every(x=>HEX.test(x)) || BigInt(balance)<BigInt(review.maximum_gas_wei))
        throw new Error('Native ETH for the reviewed gas envelope is still required.');
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
  if (typeof module!=='undefined' && module.exports) {module.exports={sendLaunch};return;}
  root.SkewLaunchWallet={sendLaunch};
})(typeof window!=='undefined'?window:globalThis);
