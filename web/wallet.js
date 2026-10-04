'use strict';
// Provider discovery uses the announced provider, never brand flags as identity.
(function (root) {
  const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
  function safeIcon(value) {
    return typeof value === 'string' && value.length <= 100000 &&
      /^data:image\/(png|webp|jpeg|svg\+xml)(?:;charset=[a-z0-9-]+|;utf8)?(?:;base64)?,/i.test(value) ? value : null;
  }
  function messageHex(message) {
    return '0x' + Array.from(new TextEncoder().encode(message), b => b.toString(16).padStart(2, '0')).join('');
  }
  async function accountState(provider, prompt = false) {
    const accounts = await provider.request({method: prompt ? 'eth_requestAccounts' : 'eth_accounts'});
    if (!Array.isArray(accounts) || !ADDRESS.test(accounts[0])) throw new Error('No Ethereum account was selected.');
    const rawChain = await provider.request({method: 'eth_chainId'});
    if (typeof rawChain !== 'string' || !/^0x[0-9a-f]+$/i.test(rawChain)) throw new Error('Wallet network unavailable.');
    const chain = Number.parseInt(rawChain, 16);
    if (![1, 42161, 421614].includes(chain)) throw new Error('Choose Ethereum, Arbitrum One or Arbitrum Sepolia in your wallet.');
    return {address: accounts[0], chain_id: chain};
  }
  function sameAccount(a, b) { return a.address.toLowerCase() === b.address.toLowerCase() && a.chain_id === b.chain_id; }
  async function ensureArbitrum(provider, progress = () => {}) {
    if (!provider || typeof provider.request !== 'function') throw new Error('Select your wallet first.');
    const accounts = await provider.request({method: 'eth_requestAccounts'});
    if (!Array.isArray(accounts) || !ADDRESS.test(accounts[0])) throw new Error('No account selected.');
    if (Number(await provider.request({method: 'eth_chainId'})) !== 42161) {
      progress('Approve the switch to Arbitrum One in your wallet.');
      try { await provider.request({method: 'wallet_switchEthereumChain', params: [{chainId: '0xa4b1'}]}); }
      catch (error) {
        if (Number(error.code) !== 4902) throw error;
        await provider.request({method: 'wallet_addEthereumChain', params: [{chainId: '0xa4b1', chainName: 'Arbitrum One',
          nativeCurrency: {name: 'Ether', symbol: 'ETH', decimals: 18}, rpcUrls: ['https://arb1.arbitrum.io/rpc'], blockExplorerUrls: ['https://arbiscan.io']} ]});
        await provider.request({method: 'wallet_switchEthereumChain', params: [{chainId: '0xa4b1'}]});
      }
    }
    const selected = await accountState(provider);
    if (selected.chain_id !== 42161 || selected.address.toLowerCase() !== accounts[0].toLowerCase())
      throw new Error('Wallet account changed during network selection. Connect again.');
    return selected;
  }
  function connectionError(error) {
    if (Number(error?.code) === -32002) return 'A wallet request is already open. Open MetaMask and finish or cancel it, then try again.';
    if (Number(error?.code) === 4001) return 'Wallet request cancelled. Nothing was submitted. You can try again.';
    return error?.message || 'Wallet connection failed. Open your wallet and try again.';
  }
  function rememberedProvider(providers, hint) {
    if (!hint || typeof hint !== 'object') return null;
    const exact = providers.filter(p => p.id === hint.id);
    if (exact.length === 1) return exact[0];
    // EIP-6963 UUIDs may change across page loads. Metadata is only a selection
    // hint: the caller must still verify the selected wallet's current account.
    if (!hint.rdns || !hint.name) return null;
    const stable = providers.filter(p => p.rdns === hint.rdns && p.name === hint.name);
    return stable.length === 1 ? stable[0] : null;
  }
  async function signIn(provider, request, progress = () => {}, options = {}) {
    progress('Choose an account in your wallet…');
    const selected = options.arbitrum ? await ensureArbitrum(provider, progress) : await accountState(provider, true);
    const challenge = await request('/api/auth/challenge', selected);
    if (!sameAccount(selected, await accountState(provider))) throw new Error('Your wallet changed. Connect again.');
    progress('Confirm the sign-in message in your wallet.');
    const signature = await provider.request({method: 'personal_sign', params: [messageHex(challenge.message), selected.address]});
    if (!sameAccount(selected, await accountState(provider))) throw new Error('Your wallet changed. Connect again.');
    progress('Verifying your signature…');
    const result = await request('/api/auth/verify', {challenge_id: challenge.challenge_id, signature});
    if (!sameAccount(selected, await accountState(provider))) {
      await request('/api/auth/logout', {});
      throw new Error('Your wallet changed. Connect again.');
    }
    return result;
  }
  async function signPayment(provider, payment) {
    if (!provider || typeof provider.request !== 'function') throw new Error('Connect your signing wallet first.');
    const typed = payment?.typed_data, template = payment?.payment_template;
    if (payment?.status !== 'CHALLENGE_READY' || payment.signature_required !== true || !typed || !template)
      throw new Error('A current, validated payment challenge is required.');
    const chain = Number(payment.network?.split(':')[1]);
    const auth = template.payload?.authorization, accepted = template.accepted;
    const addr = value => typeof value === 'string' && ADDRESS.test(value);
    const eq = (a, b) => addr(a) && addr(b) && a.toLowerCase() === b.toLowerCase();
    if (!Number.isSafeInteger(chain) || ![42161, 421614].includes(chain) ||
        template.x402Version !== 2 || accepted?.scheme !== 'exact' || accepted.network !== payment.network ||
        typed.primaryType !== 'TransferWithAuthorization' || Number(typed.domain?.chainId) !== chain ||
        !eq(typed.domain?.verifyingContract, payment.asset) || !eq(accepted.asset, payment.asset) ||
        !eq(accepted.payTo, payment.pay_to) || !eq(auth?.from, payment.payer) || !eq(auth?.to, payment.pay_to) ||
        accepted.amount !== payment.amount_atoms || auth.value !== payment.amount_atoms ||
        !/^\d+$/.test(payment.amount_atoms) || !/^0x[0-9a-fA-F]{64}$/.test(auth.nonce || '') ||
        !eq(typed.message?.from, auth.from) || !eq(typed.message?.to, auth.to) || typed.message?.nonce !== auth.nonce ||
        ['value', 'validAfter', 'validBefore'].some(field => String(typed.message?.[field]) !== auth[field]) ||
        Number(auth.validBefore) !== payment.expires ||
        Number(auth.validBefore) <= Math.floor(Date.now() / 1000) ||
        Number(auth.validAfter) >= Math.floor(Date.now() / 1000))
      throw new Error('Payment amount, recipient, network or authorization does not match the reviewed purchase.');
    const selected = await accountState(provider);
    if (selected.chain_id !== chain || !eq(selected.address, payment.payer))
      throw new Error(`Choose the payer wallet on ${chain === 421614 ? 'Arbitrum Sepolia' : 'Arbitrum One'}.`);
    const signature = await provider.request({method: 'eth_signTypedData_v4', params: [selected.address, JSON.stringify(typed)]});
    if (!sameAccount(selected, await accountState(provider))) throw new Error('Your wallet changed. The payment was not submitted.');
    if (!/^0x[0-9a-fA-F]{130}$/.test(signature || '')) throw new Error('Wallet returned an invalid payment signature.');
    const payload = {...template, payload: {...template.payload, signature}};
    const text = JSON.stringify(payload);
    if (typeof Buffer !== 'undefined') return Buffer.from(text, 'utf8').toString('base64');
    return btoa(Array.from(new TextEncoder().encode(text), c => String.fromCharCode(c)).join(''));
  }
  async function sendDataPassStep(provider, plan, contract, step) {
    let attempted = false;
    try {
      if (!provider || typeof provider.request !== 'function') throw new Error('Reconnect your signing wallet.');
      const hash = value => typeof value === 'string' && /^0x[0-9a-fA-F]{64}$/.test(value);
      const usdc = {421614:'0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d',42161:'0xaf88d065e77c8cc2239327c5edb3a432268e5831'}[plan?.chain_id];
      if (![0,1].includes(step) || typeof contract !== 'string' || !ADDRESS.test(contract) || /^0x0{40}$/i.test(contract) ||
          !usdc || plan.x402_payment_required !== false || plan.broadcasts !== 0 ||
          plan.signing_authority !== 'CUSTOMER_WALLET_ONLY' || plan.asset?.toLowerCase() !== usdc ||
          !/^\d+$/.test(plan.amount_atoms || '') || BigInt(plan.amount_atoms) <= 0n || BigInt(plan.amount_atoms) > 1000000n ||
          !hash(plan.purchase_id) || !hash(plan.report_sha256 && '0x' + plan.report_sha256) ||
          !hash(plan.terms_sha256 && '0x' + plan.terms_sha256) || !hash(plan.release_id) ||
          !Number.isSafeInteger(plan.expires_at) || plan.expires_at <= Math.floor(Date.now()/1000) || plan.transactions?.length !== 2)
        throw new Error('A current, version-bound DataPass purchase is required.');
      const price = BigInt(plan.amount_atoms).toString(16).padStart(64,'0');
      const approve = '0x095ea7b3' + contract.slice(2).toLowerCase().padStart(64,'0') + price;
      const purchase = '0x9e25f4a8' + plan.release_id.slice(2) + plan.report_sha256 + plan.terms_sha256 + price + plan.purchase_id.slice(2);
      for (const [index,data] of [[0,approve],[1,purchase]]) {
        const tx = plan.transactions[index];
        if (tx.value !== '0x0' || tx.to?.toLowerCase() !== (index ? contract.toLowerCase() : usdc) || tx.data?.toLowerCase() !== data.toLowerCase())
          throw new Error('DataPass transaction differs from its reviewed asset, allowance, version or purchase ID.');
      }
      const selected = await accountState(provider);
      if (selected.chain_id !== plan.chain_id || selected.address.toLowerCase() !== plan.from?.toLowerCase())
        throw new Error('Choose the buyer wallet on the purchase plan’s Arbitrum network.');
      if (step === 1) {
        const allowance = await provider.request({method:'eth_call',params:[{to:usdc,data:'0xdd62ed3e' + selected.address.slice(2).toLowerCase().padStart(64,'0') + contract.slice(2).toLowerCase().padStart(64,'0')},'latest']});
        if (!/^0x[0-9a-fA-F]{1,64}$/.test(allowance || '') || BigInt(allowance) < BigInt(plan.amount_atoms))
          throw new Error('The token approval is not confirmed yet. Check it before buying.');
      }
      if (!sameAccount(selected, await accountState(provider))) throw new Error('Your wallet changed before submission.');
      const tx = plan.transactions[step];
      attempted = true;
      const txHash = await provider.request({method:'eth_sendTransaction',params:[{from:selected.address,to:tx.to,data:tx.data,value:tx.value}]});
      if (!hash(txHash)) throw new Error('The wallet did not return a transaction hash. Check this purchase before retrying.');
      return {tx_hash:txHash,step};
    } catch (cause) {
      const error = cause instanceof Error ? cause : new Error('Wallet submission failed. Check this purchase before retrying.');
      error.submission_uncertain = attempted && Number(error.code) !== 4001;
      throw error;
    }
  }
  const exported = {safeIcon, messageHex, accountState, signIn, signPayment, sendDataPassStep, ensureArbitrum, connectionError, rememberedProvider};
  if (typeof module !== 'undefined' && module.exports) { module.exports = exported; return; }
  const providers = new Map(), subscribers = new Set();
  function publish() { for (const fn of subscribers) fn([...providers.values()]); }
  function announce(event) {
    const detail = event.detail;
    if (!detail || !detail.info || typeof detail.provider?.request !== 'function') return;
    const {uuid, name, icon, rdns} = detail.info;
    if (typeof uuid !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(uuid)) return;
    if (typeof name !== 'string' || !name.trim() || name.length > 60 || /[\x00-\x1f]/.test(name)) return;
    if ([...providers.values()].some(p => p.provider === detail.provider)) return;
    const stableId = typeof rdns === 'string' && /^[a-zA-Z0-9.-]{1,120}$/.test(rdns) ? rdns : null;
    providers.set(uuid, {id: uuid, rdns: stableId, name: name.trim(), icon: safeIcon(icon), provider: detail.provider});
    publish();
  }
  root.addEventListener('eip6963:announceProvider', announce);
  root.dispatchEvent(new Event('eip6963:requestProvider'));
  root.setTimeout(() => {
    const legacy = root.ethereum?.providers || (root.ethereum ? [root.ethereum] : []);
    for (const [index, provider] of legacy.entries()) {
      if (typeof provider?.request !== 'function' || [...providers.values()].some(p => p.provider === provider)) continue;
      providers.set('legacy-' + index, {id: 'legacy-' + index, name: 'Browser wallet' + (legacy.length > 1 ? ' ' + (index + 1) : ''), icon: null, provider});
    }
    publish();
  }, 350);
  root.WalletBridge = {...exported, subscribe(fn) { subscribers.add(fn); fn([...providers.values()]); return () => subscribers.delete(fn); }};
})(typeof window !== 'undefined' ? window : globalThis);
