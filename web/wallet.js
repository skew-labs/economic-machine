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
  async function signIn(provider, request, progress = () => {}) {
    progress('Choose an account in your wallet…');
    const selected = await accountState(provider, true);
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
  const exported = {safeIcon, messageHex, accountState, signIn};
  if (typeof module !== 'undefined' && module.exports) { module.exports = exported; return; }
  const providers = new Map(), subscribers = new Set();
  function publish() { for (const fn of subscribers) fn([...providers.values()]); }
  function announce(event) {
    const detail = event.detail;
    if (!detail || !detail.info || typeof detail.provider?.request !== 'function') return;
    const {uuid, name, icon} = detail.info;
    if (typeof uuid !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(uuid)) return;
    if (typeof name !== 'string' || !name.trim() || name.length > 60 || /[\x00-\x1f]/.test(name)) return;
    if ([...providers.values()].some(p => p.provider === detail.provider)) return;
    providers.set(uuid, {id: uuid, name: name.trim(), icon: safeIcon(icon), provider: detail.provider});
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
