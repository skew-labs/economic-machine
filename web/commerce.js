'use strict';

(() => {
  const C = window.MachineConsole;
  const $ = id => document.getElementById(id);
  let catalog, orders = [], filter = 'all', busy = false, selected = null, current = null;
  let quoteKey = null;
  const uncertainPayments = new Set();
  const text = C.el, action = C.button;
  const cost = value => Number(value).toLocaleString('en-US', {maximumFractionDigits: 6});
  function atoms(value) {
    const raw = String(value);
    if (!/^\d+(\.\d{1,6})?$/.test(raw)) throw new Error('Use a token amount with at most six decimal places.');
    const [whole, part = ''] = raw.split('.');
    return BigInt(whole) * 1000000n + BigInt(part.padEnd(6, '0'));
  }
  function amount(value) {
    const part = String(value % 1000000n).padStart(6, '0').replace(/0+$/, '');
    return String(value / 1000000n) + (part ? '.' + part : '');
  }
  const network = value => value === 'eip155:421614' ? 'Arbitrum Sepolia' : value === 'eip155:42161' ? 'Arbitrum One' : value;
  const unit = p => p.network === 'eip155:421614' ? 'test tokens' : p.token_name;
  const stamp = value => new Date(value * 1000).toLocaleString('en-US');
  const link = (label, href) => { const a = text('a', 'button secondary', label); a.href = href; return a; };
  const toolsURL = name => C.API_PREFIX ? `${C.API_PREFIX}/${name}` : `https://machine.148-113-153-116.nip.io/commerce/${name}`;
  const mark = name => { const img = text('img', 'commerce-mark'); img.src = `${C.API_PREFIX}/assets/app-${name}.svg`; img.alt = ''; img.width = 40; img.height = 40; return img; };
  const panel = (heading, detail) => {
    const node = text('section', 'commerce-panel');
    if (heading) node.append(text('h2', '', heading));
    if (detail) node.append(text('p', 'muted', detail));
    return node;
  };
  function signIn() { C.signIn(); }
  function requireOwner() {
    if (C.PREVIEW || C.LOCAL_ENGINE) { C.notify('Open a signed-in commerce workspace to purchase services.', true); return false; }
    if (!C.state.connected) { signIn(); return false; }
    return true;
  }
  async function run(fn) {
    if (busy) return;
    busy = true;
    $('commerce-error').textContent = '';
    for (const node of document.querySelectorAll('#commerce-dialog button')) node.disabled = true;
    try { await fn(); }
    catch (error) { $('commerce-error').textContent = Number(error.code) === 4001 ? 'Wallet approval cancelled. Nothing was submitted.' : error.message; }
    finally { busy = false; if ($('commerce-dialog').open) paintCheckout(); }
  }
  async function refresh() {
    if (C.LOCAL_ENGINE) { paint(); return; }
    const response = await fetch(`${C.API_PREFIX}/api/commerce/catalog`, {credentials: 'same-origin', headers: {Accept: 'application/json'}});
    if (!response.ok) throw new Error('Service catalog unavailable. Refresh to try again.');
    catalog = await response.json();
    orders = C.state.connected && !C.PREVIEW ? (await C.api('/api/commerce/checkouts')).checkouts : [];
    paint();
  }
  function setup() {
    const box = panel('Give your agent a purchasing route', 'Connect once. Every purchase follows the same limits and receipt trail.');
    const row = text('div', 'commerce-setup');
    row.append(action('1. Connect wallet', 'button secondary', signIn),
      action('2. Set payment limit', 'button secondary', () => { C.setView('funds'); }),
      action('3. Create buyer key', 'button secondary', () => { if (requireOwner()) { C.setView('keys'); C.openKey('buyer'); } }),
      action('4. View purchases', 'button secondary', () => { $('commerce-history')?.scrollIntoView({block: 'start', behavior: 'smooth'}); }));
    box.append(row);
    return box;
  }
  function productCard(product, plan = null) {
    const available = product.status === 'AVAILABLE' && (!plan || plan.status === 'AVAILABLE');
    const box = panel(); box.classList.add('commerce-product');
    const head = text('div', 'commerce-product-head');
    head.append(mark(product.category === 'data' || product.category === 'subscription' ? 'atlas' : 'engine'),
      text('span', available ? 'commerce-status available' : 'commerce-status', available ? 'Available' : 'Unavailable'));
    const titles = {'arbitrum.finalized-block': 'Arbitrum block data', 'compute.gpu-hour': 'GPU compute hours'};
    box.append(head, text('h2', '', plan?.name || product.offers[0]?.name || titles[product.data_type] || product.data_type.replaceAll('.', ' ')),
      text('p', 'muted', plan?.description || `${product.category === 'compute' ? 'Provider compute service' : 'Version-bound resource'} · ${product.version}`));
    const price = plan?.price || product.offers[0]?.unit_price;
    box.append(text('div', 'commerce-price', price ? `${cost(price)} ${unit(product)}` : 'No current quote'),
      text('p', 'commerce-caption', plan ? `${cost(plan.duration_seconds / 86400)} days · renew manually` : `${network(product.network)} · pay per purchase`));
    box.append(action(available ? 'Review purchase' : 'View availability', 'button ' + (available ? 'primary' : 'secondary'), () => openPurchase(product, plan)));
    return box;
  }
  function history(root, subscriptionsOnly = false) {
    const box = panel(subscriptionsOnly ? 'Your access periods' : 'Your purchases', C.state.connected ? 'Payment, delivery and access stay linked to the same purchase.' : 'Sign in to see your own purchases and receipts.');
    box.id = 'commerce-history';
    const rows = orders.filter(o => !subscriptionsOnly || o.plan);
    if (!rows.length) box.append(text('div', 'commerce-empty', subscriptionsOnly ? 'No paid subscription periods.' : 'No purchases in this workspace.'));
    for (const order of rows) {
      const row = text('div', 'commerce-history-row');
      const identity = text('div'); identity.append(text('strong', '', order.plan?.name || order.resource_id),
        text('small', '', `${order.agreement.terms.total_price} · ${order.id.slice(-8)}`));
      row.append(identity, text('span', 'commerce-status', order.entitlement?.status || order.status),
        action('View purchase', 'button secondary', () => openExisting(order)));
      box.append(row);
    }
    root.append(box);
  }
  function paint() {
    if (!['market', 'subscriptions'].includes(C.state.view)) return;
    const root = $(C.state.view === 'market' ? 'commerce-market' : 'commerce-subscriptions');
    root.replaceChildren();
    if (C.LOCAL_ENGINE) { root.append(panel('Connect a commerce service', 'This standalone engine manages local accounts. Run the commerce API alongside it to register providers and purchase external resources.')); return; }
    if (!catalog) { root.append(text('p', 'muted', 'Loading registered services…')); return; }
    if (C.PREVIEW) root.append(panel('Public catalog · private purchases', 'Recorded evidence does not authorize purchases. Open your wallet workspace to buy.'));
    if (C.state.view === 'market') {
      root.append(setup());
      const bar = text('div', 'commerce-toolbar'), tabs = text('div', 'commerce-filters');
      for (const [id, label] of [['all', 'All services'], ['data', 'Data'], ['compute', 'Compute']]) {
        const b = action(label, 'button secondary', () => { filter = id; paint(); }); b.setAttribute('aria-pressed', String(filter === id)); tabs.append(b);
      }
      bar.append(tabs, action('Refresh catalog', 'button secondary', () => refresh().catch(e => C.notify(e.message, true)))); root.append(bar);
      const grid = text('div', 'commerce-grid');
      for (const p of catalog.products.filter(p => p.category !== 'subscription' && (filter === 'all' || p.category === filter))) grid.append(productCard(p));
      if (filter !== 'compute') {
        const atlas = panel('Atlas APAC compute data', 'Explore source-bound price research and transferable DataPass licenses.');
        atlas.classList.add('commerce-product'); atlas.prepend(mark('atlas'));
        atlas.append(action('View data licenses', 'button secondary', () => C.setView('data'))); grid.append(atlas);
      }
      if (filter !== 'data' && !catalog.products.some(p => p.category === 'compute')) {
        const compute = panel('Compute providers', 'No GPU or compute seller is connected to this deployment.');
        compute.classList.add('commerce-product'); compute.prepend(mark('engine'));
        compute.append(text('span', 'commerce-status', 'No providers connected'),
          link('Provider setup guide', 'https://github.com/skew-labs/economic-machine/blob/main/docs/SERVICE_COMMERCE.md')); grid.append(compute);
      }
      root.append(grid);
      const developer = panel('Machine-to-machine trading', 'Your agents find sellers, agree on price and pay within your limits. Every purchase has a receipt.');
      developer.append(action('Create buyer key', 'button secondary', () => { if (requireOwner()) { C.setView('keys'); C.openKey('buyer'); } }),
        action('Create seller key', 'button secondary', () => { if (requireOwner()) { C.setView('keys'); C.openKey('seller'); } }),
        link('Agent integration', 'https://github.com/skew-labs/economic-machine/blob/main/docs/SERVICE_COMMERCE.md'));
      root.append(developer); history(root);
    } else {
      const grid = text('div', 'commerce-grid');
      const free = panel('Engine Community', 'Run your own open-source engine. Keep API keys and execution policies in your environment.');
      free.classList.add('commerce-product'); free.prepend(mark('engine'));
      free.append(text('div', 'commerce-price', 'Free'), text('p', 'commerce-caption', 'MIT license · self-hosted'),
        link('Get the open-source engine', 'https://github.com/skew-labs/economic-machine')); grid.append(free);
      for (const plan of catalog.plans) {
        if (plan.product) grid.append(productCard(plan.product, plan));
        else {
          const item = panel(plan.name, plan.description); item.classList.add('commerce-product'); item.prepend(mark('atlas'));
          item.append(text('div', 'commerce-price', `${cost(plan.price)} USDC / month`),
            text('p', 'commerce-caption', `${cost(plan.duration_seconds / 86400)} days · prepaid · no automatic charge`),
            text('span', 'commerce-status', 'Payment provider not connected'),
            action('View subscription', 'button secondary', () => openPlan(plan))); grid.append(item);
        }
      }
      if (!catalog.plans.length) {
        for (const [name, label, description] of [['atlas', 'Atlas Data', 'Compute intelligence delivered through your agent API.'], ['site-lens', 'SiteLens', 'APAC site and deployable-capacity research.']]) {
          const item = panel(label, description); item.classList.add('commerce-product'); item.prepend(mark(name));
          item.append(text('div', 'commerce-price', 'Pricing not published'), text('p', 'commerce-caption', 'Paid subscriptions are not open yet.'),
            link('Explore the tool', toolsURL(name === 'atlas' ? 'atlas.html' : 'site-lens.html'))); grid.append(item);
        }
      }
      root.append(grid, panel('Approve one period at a time', 'Paid plans use prepaid access. Review the exact price and expiry before signing. Expiry stops access; it never triggers an automatic charge.'));
      history(root, true);
    }
  }
  function ensureDialog() {
    if ($('commerce-dialog')) return;
    const dialog = text('dialog', 'commerce-dialog'); dialog.id = 'commerce-dialog';
    dialog.setAttribute('aria-labelledby', 'commerce-dialog-title');
    const head = text('div', 'commerce-dialog-head');
    const title = text('h2', '', 'Review purchase'); title.id = 'commerce-dialog-title';
    head.append(title, action('Close', 'quiet', () => { if (!busy) dialog.close(); }));
    const body = text('div'); body.id = 'commerce-dialog-body';
    const error = text('p', 'form-error'); error.id = 'commerce-error'; error.setAttribute('role', 'alert');
    dialog.append(head, body, error); document.body.append(dialog);
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => { selected = null; current = null; });
  }
  function openPurchase(product, plan) {
    ensureDialog(); selected = {product, plan}; current = null; quoteKey = 'quote-' + crypto.randomUUID();
    $('commerce-dialog-title').textContent = plan?.name || product.offers[0]?.name || product.data_type;
    $('commerce-error').textContent = ''; paintCheckout(); $('commerce-dialog').showModal();
  }
  function openPlan(plan) {
    ensureDialog(); selected = null; current = null;
    $('commerce-dialog-title').textContent = plan.name; $('commerce-error').textContent = '';
    const root = $('commerce-dialog-body'); root.replaceChildren();
    summary(root, [['Price', `${cost(plan.price)} USDC`], ['Access period', `${cost(plan.duration_seconds / 86400)} days`], ['Renewal', 'Manual · approve each period']]);
    root.append(text('p', '', 'The price is published. Payment opens when an approved USDC merchant and current subscription offer are connected.'),
      text('p', 'commerce-caption', 'No charge or subscription is created by viewing this plan.'),
      link('Payment setup guide', 'https://github.com/skew-labs/economic-machine/blob/main/docs/SERVICE_COMMERCE.md'));
    $('commerce-dialog').showModal();
  }
  function openExisting(order) {
    ensureDialog(); selected = {product: catalog.products.find(p => p.id === order.resource_id), plan: order.plan}; current = order;
    $('commerce-dialog-title').textContent = order.plan?.name || order.resource_id; $('commerce-error').textContent = '';
    paintCheckout(); $('commerce-dialog').showModal();
  }
  function field(root, id, label, value, options) {
    const wrap = text('div', 'commerce-field'), caption = text('label', '', label); caption.htmlFor = id;
    const input = text(options ? 'select' : 'input'); input.id = id;
    if (options) for (const [id, name] of options) { const option = text('option', '', name); option.value = id; input.append(option); }
    else { input.value = value; input.inputMode = 'decimal'; }
    wrap.append(caption, input); root.append(wrap); return input;
  }
  function summary(root, entries) {
    const dl = text('dl', 'commerce-summary');
    for (const [label, value] of entries) dl.append(text('dt', '', label), text('dd', '', String(value)));
    root.append(dl);
  }
  function eligibleLimits(p, amount) {
    const asset = p.network + '/erc20:' + p.asset.toLowerCase();
    return C.state.payments.mandates.filter(m => m.expires > Date.now() / 1000 + 5 && m.resources.includes(p.id) &&
      m.payment_asset === asset && BigInt(m.max_order) >= amount && BigInt(m.budget) - BigInt(m.reserved) - BigInt(m.spent) >= amount &&
      (!C.state.identity || m.payer.toLowerCase() === C.state.identity.address.toLowerCase()));
  }
  function paintCheckout() {
    const root = $('commerce-dialog-body'); root.replaceChildren();
    const p = selected?.product;
    if (!p && !current?.payment) { root.append(text('p', '', 'This provider is no longer registered. Refresh the catalog before requesting another quote.')); return; }
    if (current?.payment) { paymentScreen(root); return; }
    if (current) {
      const t = current.agreement.terms;
      summary(root, [['Total', `${cost(t.total_price)} ${unit(p)}`], ['Units', t.units], ['Usage rights', t.license],
        ['Network', network(p.network)], ['Recipient', p.pay_to], ['Quote expires', stamp(current.agreement.expires)]]);
      if (current.plan) root.append(text('p', '', `${cost(current.plan.duration_seconds / 86400)} days of prepaid access. No automatic renewal.`));
      const limits = eligibleLimits(p, atoms(t.total_price));
      if (limits.length) {
        field(root, 'checkout-limit', 'Use a shared payment limit', null, limits.map(m => [m.id, `${cost(m.budget / 1e6)} cap · ${m.id.slice(-8)}`]));
        root.append(action('Prepare x402 payment', 'button primary', () => run(async () => {
          current = await C.api(`/api/commerce/checkouts/${current.id}/prepare`, {mandate_id: $('checkout-limit').value});
          await C.refresh();
        })));
      } else {
        root.append(text('p', '', 'Create a payment limit for this resource and your payer wallet. Your funds remain in your wallet.'));
        field(root, 'checkout-budget', 'Shared budget · tokens', String(t.total_price));
        root.append(action('Create purchase limit', 'button primary', () => run(async () => {
          if (!C.state.identity) throw new Error('Sign in with the wallet that will pay.');
          await C.api('/api/payment-mandates', {payer: C.state.identity.address, payment_asset: p.network + '/erc20:' + p.asset.toLowerCase(),
            budget: $('checkout-budget').value, max_order: t.total_price, resources: [p.id], ttl_seconds: 3600});
          await C.refresh();
        })));
      }
      return;
    }
    if (!p.offers.length) {
      root.append(text('p', '', 'This endpoint is registered, but its seller has no current offer. You cannot purchase an expired quote.'),
        text('p', 'muted', `${network(p.network)} · ${p.version}`),
        link('View provider setup', 'https://github.com/skew-labs/economic-machine/blob/main/docs/SERVICE_COMMERCE.md')); return;
    }
    const form = text('form', 'commerce-form');
    field(form, 'checkout-offer', 'Seller offer', null, p.offers.map(o => [o.supply_id, `${o.name} · ${cost(o.unit_price)} ${unit(p)}/unit`]));
    field(form, 'checkout-units', 'Units', String(p.offers[0].min_units));
    field(form, 'checkout-maximum', 'Maximum total · tokens', selected.plan?.price || amount(atoms(p.offers[0].unit_price) * BigInt(p.offers[0].min_units)));
    const first = p.offers[0];
    field(form, 'checkout-purpose', 'Purpose', null, first.purposes.map(s => [s, s]));
    field(form, 'checkout-license', 'Usage rights', null, first.licenses.map(s => [s, s]));
    summary(form, [['Network', network(p.network)], ['Version', p.version], ['Recipient', p.pay_to]]);
    const btn = text('button', 'button primary', C.state.connected ? 'Get purchase quote' : 'Sign in to continue'); btn.type = 'submit'; form.append(btn);
    form.addEventListener('submit', event => {
      event.preventDefault(); if (!requireOwner()) return;
      const offer = p.offers.find(o => o.supply_id === $('checkout-offer').value);
      const raw = {resource_id: p.id, supply_id: offer.supply_id, plan_id: selected.plan?.id || null,
        units: Number($('checkout-units').value), purpose: $('checkout-purpose').value, license: $('checkout-license').value,
        max_total: $('checkout-maximum').value, max_age_seconds: 86400, max_refresh_seconds: offer.refresh_seconds,
        response_seconds: offer.response_seconds, idempotency_key: quoteKey};
      run(async () => { current = await C.api('/api/commerce/checkouts', raw); });
    });
    root.append(form, text('p', 'commerce-caption', 'A quote does not charge your wallet. Review and sign a separate payment authorization.'));
  }
  function paymentScreen(root) {
    const payment = current.payment, p = selected.product;
    summary(root, [['Status', payment.status.replaceAll('_', ' ')], ['Amount', `${cost(Number(payment.amount_atoms) / 1e6)} ${p ? unit(p) : 'tokens'}`],
      ['Payer', payment.payer], ['Recipient', payment.pay_to], ['Network', network(payment.network)]]);
    if (payment.reason) root.append(text('p', 'muted', payment.reason));
    if (payment.status === 'PREPARED') root.append(action('Validate payment request', 'button primary', () => run(async () => {
      await C.api(`/api/payments/${payment.id}/challenge`, {}); current = await C.api(`/api/commerce/checkouts/${current.id}`);
    })));
    if (payment.status === 'CHALLENGE_READY' && !uncertainPayments.has(payment.id)) root.append(text('p', '', 'Approve this exact payment in your wallet. Signing authorizes the displayed amount and recipient.'),
      action(C.getWallet() ? 'Approve payment in wallet' : 'Reconnect signing wallet', 'button primary', () => {
        if (!C.getWallet()) { $('commerce-dialog').close(); C.signIn(); return; }
        run(async () => {
          const signature = await WalletBridge.signPayment(C.getWallet(), payment);
          uncertainPayments.add(payment.id);
          // A lost response is ambiguous. Never resubmit the signed authorization.
          try { await C.api(`/api/payments/${payment.id}/submit`, {payment_signature: signature}); }
          catch { C.notify('Submission outcome unavailable. Refresh this purchase to reconcile it; do not pay again.', true); }
          current = await C.api(`/api/commerce/checkouts/${current.id}`);
          await C.refresh();
        });
      }));
    if (uncertainPayments.has(payment.id) && !['SETTLED', 'EXPIRED_UNPAID'].includes(payment.status)) root.append(text('p', '', 'A signed payment may have been submitted. Check its existing record; another approval is disabled.'));
    if (['PREPARED', 'CHALLENGE_READY'].includes(payment.status) && !uncertainPayments.has(payment.id)) root.append(action('Cancel unsigned purchase', 'button secondary', () => run(async () => {
      await C.api(`/api/payments/${payment.id}/cancel`, {}); current = await C.api(`/api/commerce/checkouts/${current.id}`); await C.refresh();
    })));
    if (!['SETTLED', 'EXPIRED_UNPAID', 'CANCELLED'].includes(payment.status)) root.append(action('Check payment and delivery', 'button secondary', () => run(async () => {
      await C.api(`/api/payments/${payment.id}/reconcile`, {}); current = await C.api(`/api/commerce/checkouts/${current.id}`); await C.refresh();
    })));
    if (payment.status === 'PAID_DELIVERY_MISSING') root.append(text('p', '', 'Payment is confirmed; delivery is missing. Keep this receipt and contact the provider. Do not purchase again.'));
    if (current.entitlement) root.append(text('p', '', `Access ${current.entitlement.status.toLowerCase()} until ${stamp(current.entitlement.expires)}. Renew with a new approved purchase.`));
    if (current.entitlement_error) root.append(text('p', '', 'Payment is recorded, but subscription access was not verified. Keep the receipt and contact the provider; do not pay again.'));
    if (current.entitlement?.status === 'ACTIVE' && current.plan?.id === 'atlas-monthly') root.append(action('Open subscribed data', 'button secondary', () => run(async () => {
      const data = await C.api(`/api/commerce/subscriptions/${current.plan.id}/delivery`);
      current.subscribed_data = data;
    })));
    if (current.subscribed_data) root.append(text('pre', 'receipt-json', JSON.stringify(current.subscribed_data, null, 2)));
    if (payment.tx_hash && payment.network === 'eip155:421614') root.append(link('View testnet transaction', `https://sepolia.arbiscan.io/tx/${payment.tx_hash}`));
    if (payment.delivery && payment.status === 'SETTLED') {
      const details = text('details', 'commerce-delivery'); details.append(text('summary', '', 'View delivered resource'),
        text('pre', 'receipt-json', JSON.stringify(payment.delivery.artifact, null, 2))); root.append(details);
    }
    const receipt = text('details', 'commerce-delivery'); receipt.append(text('summary', '', 'Purchase receipt'),
      text('pre', 'receipt-json', JSON.stringify({checkout_id: current.id, terms_hash: payment.terms_hash,
        status: payment.status, amount_atoms: payment.amount_atoms, tx_hash: payment.tx_hash,
        observation: payment.observation, delivery_hash: payment.delivery?.artifact_hash,
        entitlement: current.entitlement, safe_to_retry_payment: false}, null, 2))); root.append(receipt);
  }
  window.CommerceConsole = {render(view) {
    if (!['market', 'subscriptions'].includes(view)) return;
    paint(); refresh().catch(e => C.notify(e.message, true));
  }};
})();
