'use strict';
(() => {
  const C = window.MachineConsole;
  const config = C.LOCAL_ENGINE || C.PREVIEW ? Promise.resolve({})
    : fetch(C.API_PREFIX + '/wallet-config', {credentials: 'omit'})
      .then(r => r.ok ? r.json() : {}).catch(() => ({}));
  let sdk;
  const version = new URL(document.currentScript.src).searchParams.get('v') || '1';
  async function load() {
    const settings = (await config).privy;
    if (!settings?.app_id) throw new Error('Privy login is not configured. Use your browser wallet.');
    if (!sdk) sdk = import(C.API_PREFIX + '/privy/entry.js?v=' + encodeURIComponent(version)).catch(error => {sdk = null; throw error;});
    const module = await sdk;
    await module.initialize(settings);
    return module;
  }
  const option = document.getElementById('privy-login');
  config.then(value => {option.hidden = !value.privy?.app_id;});
  option.addEventListener('click', async () => {
    if (C.state.busy) return;
    option.disabled = true;
    const dialog = document.getElementById('login-dialog');
    const error = document.getElementById('login-error'); error.hidden = true;
    // Native modal dialogs make a body-mounted SDK modal inert. Close ours first.
    dialog.close();
    try {
      const wallet = await (await load()).connect();
      await C.connectWallet(wallet);
      if (!C.state.identity && !dialog.open) dialog.showModal();
    } catch (_) {
      error.textContent = 'Wallet login did not finish. Retry, or choose your browser wallet.';
      error.hidden = false;
      if (!dialog.open) dialog.showModal();
    } finally {option.disabled = false;}
  });
  window.ManagedWallets = {
    async restore(hint) {return hint?.rdns === 'io.privy' ? (await load()).restore(hint) : null;},
    async logout() {if (sdk) await (await sdk).logout();},
    async agentPanel(root, refresh) {
      if (C.PREVIEW || C.LOCAL_ENGINE || !C.state.identity) return;
      const panel = C.el('section', 'ops-section connection-wallet');
      const head = C.el('div', 'ops-section-head');
      head.append(C.el('h2', '', 'MetaMask Agent Wallet'));
      const body = C.el('div', 'connection-wallet-body');
      const detail = C.el('p', 'ops-note', 'Checking your agent wallet…');
      body.append(detail); panel.append(head, body); root.prepend(panel);
      try {
        const result = await C.api('/api/wallet-connectors/metamask');
        if (!panel.isConnected) return;
        if (result.status === 'NOT_CONFIGURED') {panel.remove(); return;}
        if (result.status !== 'READY') {detail.textContent = 'Agent Wallet needs a fresh CLI login. Your browser wallet is still available.'; return;}
        detail.textContent = result.address + ' · Guard mode · Account reads';
        detail.classList.add('wallet-address-wrap');
        const add = C.button('Add to my accounts', 'button secondary', async () => {
          add.disabled = true;
          try {
            const added = await C.api('/api/wallet-connectors/metamask/connect', {});
            const synced = await C.api('/api/engine/connections/' + added.id + '/sync', {});
            await refresh();
            C.notify(synced.status === 'CONNECTED' ? 'Agent wallet connected. Balances refreshed.' : 'Account added. Balance refresh failed; use Sync to retry.', synced.status !== 'CONNECTED');
          }
          catch (error) {C.notify(error.message, true); add.disabled = false;}
        });
        body.append(add, C.el('p', 'ops-note connection-wallet-note', 'Signing remains in your MetaMask Agent Wallet. Connecting this account grants no payment approval.'));
      } catch (_) {panel.remove();}
    },
  };
  C.restoreSigningWallet();
})();
