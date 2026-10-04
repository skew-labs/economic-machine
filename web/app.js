'use strict';

const API_PREFIX = document.querySelector('meta[name="machine-api-prefix"]')?.content || '';
const LOCAL_ENGINE = document.querySelector('meta[name="engine-auth"]')?.content === 'LOCAL_OWNER_TOKEN';
let localOwnerToken = '';
const PREVIEW = API_PREFIX === '/commerce' && new URLSearchParams(location.search).get('preview') === '1';
let recordedWorkspace;
const $ = (id) => document.getElementById(id);
const state = {view: 'overview', snapshot: null, keys: [], offers: [], expires: null, busy: null, revoke: null, connected: false, mode: 'development', payments: {mandates: [], payments: [], resource_details: []}};
const names = {'csv-normalize': 'CSV normalization', 'arbitrum-state': 'Arbitrum state data', 'apac-compute-brief': 'Atlas APAC Compute Brief'};
const scopeNames = {read: 'Read', 'demands:write': 'Demand', 'supplies:write': 'Supply', 'orders:write': 'Orders', 'payments:request': 'Payment requests', 'agents:run': 'Bound agent tasks'};
const headings = {
  overview: ['Assistant', 'Your accounts and agents, working under one set of rules.', null],
  connections: ['Connections', 'Connect your APIs. See balances, positions and usage in one place.', null],
  tasks: ['Tasks', 'Save work briefs, budgets and conditions. Keep each task easy to review.', null],
  mining: ['Machine Mining', 'Submit useful work. Verify the result. Earn funded rewards.', null],
  agents: ['Agents & limits', 'Give each agent a job. Choose what it can do and spend.', null],
  execution: ['Order desk', 'Prepare an order, review the plan and track its outcome.', null],
  playground: ['Developer lab', 'Test a program with sample inputs. No account access or transactions.', null],
  usage: ['API usage', 'Reported consumption, with its source attached.', null],
  data: ['Data licenses', 'Version-bound compute intelligence. Wallet-owned access.', null],
  market: ['Buy services', 'Data, compute and services. One budget-controlled checkout.', null],
  subscriptions: ['Subscriptions', 'Choose a plan. Review its price and access period before paying.', null],
  keys: ['API keys', 'Give your agents access. Keep control of what they can spend.', 'Create API key'],
  funds: ['Service payments', 'External data and compute purchases. Limits stay in their own currency.', 'Create policy'],
  activity: ['Activity', 'Track execution, delivery and settlement in your workspace.', null]
};

function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = text;
  return node;
}
function uiIcon(name) {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('class', 'ui-icon');
  icon.setAttribute('viewBox', '0 0 256 256');
  icon.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `${API_PREFIX}/assets/ui-icons.svg#${name}`);
  icon.append(use);
  return icon;
}
function button(text, className, action) {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function credits(value) {
  return Number(value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 6});
}
function paymentLabel(network, asset) {
  return network === 'eip155:421614' && asset?.toLowerCase() === '0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d' ? 'test USDC' : 'tokens';
}
function date(value) {
  return value ? new Date(value * 1000).toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : 'Never used';
}
function policyLabel(policy) {
  return `${credits(policy.budget_atoms / 1e6)} credits · ${policy.id.slice(-8)}`;
}
function activePolicies() {
  return (state.snapshot?.policies || []).filter((p) => p.expires > Date.now() / 1000);
}
function tag(status) {
  return el('span', `tag ${status.toLowerCase()}`, status.charAt(0).toUpperCase() + status.slice(1).toLowerCase());
}
function notify(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = error ? 'notice error' : 'notice';
  $('notice').hidden = !message;
}
function formError(id, message = '') {
  $(id).textContent = message;
  $(id).hidden = !message;
}
async function api(path, body) {
  if (LOCAL_ENGINE) {
    if (!path.startsWith('/api/engine/')) throw new Error('Commerce is not enabled in this standalone runtime.');
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'omit',
      headers: {'Authorization': 'Bearer ' + localOwnerToken, 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Owner authentication required.');
    return result;
  }
  if (PREVIEW) {
    if (body !== undefined) throw new Error('Recorded workspace is read-only. Sign in to manage your agents.');
    recordedWorkspace ||= fetch(`${API_PREFIX}/demo/workspace`, {credentials: 'omit'}).then(async response => {
      if (!response.ok) throw new Error('Recorded workspace unavailable.');
      return response.json();
    }).catch(error => { recordedWorkspace = null; throw error; });
    const record = await recordedWorkspace;
    const key = {'/api/workspace': 'snapshot', '/api/keys': 'keys', '/healthz': 'health', '/api/payments': 'payments'}[path];
    if (!key) throw new Error('Unavailable in the recorded workspace.');
    return record[key];
  }
  const options = {credentials: 'same-origin', headers: {Accept: 'application/json'}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(API_PREFIX + path, options);
  let result;
  try { result = await response.json(); } catch { throw new Error('The server returned an unreadable response. Refresh and try again.'); }
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error(state.mode === 'production' ? 'Sign in to continue.' : 'This test workspace has expired. Reload to open a new workspace.');
    }
    const detail = result.error || result.detail;
    throw new Error(typeof detail === 'string' ? detail : 'The request was rejected. Check the fields and try again.');
  }
  return result;
}
function connection(ok, label) {
  state.connected = ok;
  $('connection-dot').className = `connection-dot ${ok ? 'connected' : 'failed'}`;
  $('connection-label').textContent = label;
  $('primary-action').disabled = !ok;
}
function setView(view) {
  if (!(view in headings)) return;
  state.view = view;
  document.body.dataset.view = view;
  if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  for (const name of Object.keys(headings)) $(`${name}-view`).hidden = name !== view;
  for (const node of document.querySelectorAll('[data-view]')) {
    if (node.dataset.view === view) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  }
  const [title, description, defaultAction] = headings[view];
  const action = view === 'funds' && state.mode === 'production' ? 'Create payment limit' : defaultAction;
  $('page-title').textContent = title;
  $('breadcrumb-current').textContent = title;
  $('page-description').textContent = description;
  $('primary-action').hidden = !action;
  $('balance-shortcut').hidden = LOCAL_ENGINE || !state.connected || state.mode === 'production' || Boolean(window.EngineConsole?.isView(view));
  $('primary-action').replaceChildren(uiIcon('plus'), document.createTextNode(action || ''));
  window.EngineConsole?.render(view);
  window.DataConsole?.render(view);
  window.CommerceConsole?.render(view);
  document.querySelector('.sidebar').classList.remove('menu-open');
  $('navigation-toggle').setAttribute('aria-expanded', 'false');
  $('navigation-toggle').setAttribute('aria-label', 'Open navigation');
  window.scrollTo({top: 0});
}
function empty(title, description, action, actionText = '', icon = 'key') {
  const node = el('div', 'empty');
  const mark = el('div', 'empty-icon');
  mark.append(uiIcon(icon));
  node.append(mark, el('h3', '', title), el('p', '', description));
  if (action) node.append(button(actionText, 'button primary', action));
  return node;
}
function renderKeys() {
  $('key-count').textContent = state.keys.length;
  const list = $('key-list');
  list.replaceChildren();
  if (!state.keys.length) {
    if (PREVIEW) { list.append(empty('Private agent credentials', 'Sign in to manage API keys. This public record contains no credentials.')); return; }
    list.append(empty('Connect your first agent', 'Create an API key with the permissions your agent needs. Add a spending policy when it needs to buy.', openKey, 'Create API key'));
    return;
  }
  for (const key of state.keys) {
    const row = el('article', 'key-row');
    const main = el('div', 'row-main');
    const identity = el('div', 'key-identity');
    identity.append(el('h3', '', key.name), tag(key.status));
    main.append(identity);
    if (key.status === 'active' && !PREVIEW) main.append(button('Revoke', 'revoke-button', () => openRevoke(key)));
    const meta = el('div', 'key-meta');
    meta.append(el('span', '', `Expires ${date(key.expires)}`), el('span', '', key.last_used ? `Last used ${date(key.last_used)}` : 'Never used'));
    const scopes = el('div', 'scopes');
    for (const scope of key.scopes) scopes.append(el('span', 'scope', scopeNames[scope] || scope));
    row.append(main, el('div', 'key-prefix', `${key.prefix}••••••••`), meta, scopes);
    if (key.policy_id) {
      const policy = state.snapshot.policies.find((p) => p.id === key.policy_id);
      row.append(el('div', 'policy-info', `Spending policy: ${policy ? policyLabel(policy) : key.policy_id}`));
    }
    if (key.engine_agent_id) row.append(el('div', 'policy-info', `Bound agent: ${key.engine_agent_id}`));
    if (key.payment_mandate_id) row.append(el('div', 'policy-info', `Payment limit: ${key.payment_mandate_id.slice(-8)}`));
    list.append(row);
  }
}
function renderFunds() {
  const snapshot = state.snapshot;
  for (const field of ['balance', 'reserved', 'spent']) $(field).textContent = credits(snapshot[field]);
  $('top-balance').textContent = credits(snapshot.balance);
  $('policy-count').textContent = snapshot.policies.length;
  const list = $('policy-list');
  list.replaceChildren();
  if (!snapshot.policies.length) {
    list.append(empty('Define a spending boundary', 'Approve a total budget, a per-order limit and the services an agent may use.', openPolicy, 'Create policy', 'shield-check'));
    return;
  }
  for (const policy of snapshot.policies) {
    const row = el('article', 'policy-row');
    const main = el('div', 'row-main');
    main.append(el('h3', '', `Policy ${policy.id.slice(-8)}`), tag(policy.expires > Date.now() / 1000 ? 'active' : 'expired'));
    const values = el('div', 'policy-values');
    const used = policy.spent_atoms + policy.reserved_atoms;
    for (const [label, value] of [['Budget', policy.budget_atoms], ['Per order', policy.max_order_atoms], ['Remaining', policy.budget_atoms - used]]) {
      const cell = el('div');
      cell.append(el('span', '', label), el('strong', '', `${credits(value / 1e6)} cr`));
      values.append(cell);
    }
    const scopes = el('div', 'scopes');
    for (const id of policy.allowed_offers) scopes.append(el('span', 'scope', names[id] || id));
    const meter = el('div', 'meter');
    const fill = el('div', 'meter-fill');
    fill.style.width = `${Math.min(100, used / policy.budget_atoms * 100)}%`;
    meter.append(fill);
    const linked = state.keys.filter((key) => key.policy_id === policy.id && key.status === 'active').length;
    row.append(main, values, scopes, meter, el('div', 'meter-text', `${credits(used / 1e6)} credits spent or reserved`), el('div', 'policy-info', `${linked} active ${linked === 1 ? 'key' : 'keys'} · Expires ${date(policy.expires)}`));
    list.append(row);
  }
}
function renderActivity() {
  const orders = state.snapshot.orders;
  const paymentList = $('payment-list');
  paymentList.replaceChildren();
  $('order-count').textContent = orders.length + state.payments.payments.length;
  const filter = $('activity-filter').value;
  const visible = orders.filter((order) => filter === 'all' || order.status === filter || (filter === 'pending' && !['SETTLED', 'REFUNDED'].includes(order.status)));
  const list = $('order-list');
  list.replaceChildren();
  if (!visible.length && !state.payments.payments.length) {
    list.append(empty(orders.length ? 'No transactions in this view' : 'Activity starts with your agents', orders.length ? 'Choose another status to see your transactions.' : 'Orders placed through the API appear here with delivery status and settlement records.', null, '', 'receipt'));
    return;
  }
  for (const order of visible) {
    const row = el('article', 'order-row');
    const identity = el('div');
    identity.append(el('h3', '', names[order.offer_id] || order.offer_id), el('p', '', `${date(order.created)} · ${order.id.slice(-8)}`));
    const amount = el('div', 'order-amount', credits(order.amount));
    amount.append(el('small', '', 'test credits'));
    row.append(identity, tag(order.status), amount, button('Details', 'quiet', () => showOrder(order)));
    list.append(row);
  }
  for (const payment of state.payments.payments.filter((p) => filter === 'all' || p.status === filter || (filter === 'pending' && !['SETTLED', 'EXPIRED_UNPAID', 'CANCELLED'].includes(p.status)))) {
    const row = el('article', 'order-row');
    const identity = el('div');
    identity.append(el('h3', '', 'x402 payment'), el('p', '', `${date(payment.created)} · ${payment.id.slice(-8)}`));
    const amount = el('div', 'order-amount', credits(Number(payment.amount_atoms) / 1e6));
    amount.append(el('small', '', paymentLabel(payment.network, payment.asset)));
    row.append(identity, tag(payment.status), amount, button('Details', 'quiet', () => {
      $('order-subtitle').textContent = 'External payment record';
      $('order-detail').replaceChildren(el('pre', 'receipt-json', JSON.stringify(payment, null, 2)));
      $('order-dialog').showModal();
    }));
    if (payment.network === 'eip155:421614' && /^0x[0-9a-fA-F]{64}$/.test(payment.tx_hash || '')) {
      const receiptLink = el('a', 'quiet', 'View receipt');
      receiptLink.href = `https://sepolia.arbiscan.io/tx/${payment.tx_hash}`;
      receiptLink.target = '_blank'; receiptLink.rel = 'noopener';
      row.append(receiptLink);
    }
    paymentList.append(row);
  }
}
function renderMandates() {
  $('mandate-count').textContent = state.payments.mandates.length;
  $('new-mandate').disabled = !state.payments.resource_details.length;
  const list = $('mandate-list');
  list.replaceChildren();
  if (!state.payments.mandates.length) list.append(empty('Set an external payment limit', state.payments.resource_details.length ? 'Choose an approved resource and payer wallet. Link the limit to an agent key.' : 'An operator must configure an approved x402 resource before enabling payments.', null, '', 'shield-check'));
  for (const mandate of state.payments.mandates) {
    const row = el('article', 'policy-row');
    const main = el('div', 'row-main');
    main.append(el('h3', '', `Limit ${mandate.id.slice(-8)}`), tag(mandate.expires > Date.now() / 1000 ? 'active' : 'expired'));
    const values = el('div', 'policy-values');
    for (const [label, value] of [['Budget', mandate.budget], ['Held', mandate.reserved], ['Confirmed spending', mandate.spent]]) {
      const cell = el('div');
      const [network, asset] = mandate.payment_asset.split('/erc20:');
      cell.append(el('span', '', label), el('strong', '', `${credits(value / 1e6)} ${paymentLabel(network, asset)}`));
      values.append(cell);
    }
    row.append(main, values, el('div', 'policy-info', mandate.payer), el('div', 'policy-info', mandate.payment_asset), el('div', 'meter-text', `Expires ${date(mandate.expires)}. Wallet balance is not tracked here.`));
    list.append(row);
  }
}
function render() {
  renderKeys();
  renderFunds();
  renderActivity();
  renderMandates();
  $('test-funds').hidden = state.mode === 'production';
  $('test-policies').hidden = state.mode === 'production';
  $('balance-shortcut').hidden = state.mode === 'production';
  $('mode-badge').textContent = state.mode === 'production' ? 'Production' : 'Test mode';
  $('environment-label').textContent = state.mode === 'production' ? 'Production environment' : 'Test environment';
  $('workspace-expiry').textContent = `Workspace access expires ${date(state.expires)}`;
  $('integrity-status').textContent = state.snapshot.journal_integrity ? 'Ledger integrity verified' : 'Ledger integrity check failed';
  if (PREVIEW) {
    $('mode-badge').textContent = 'Read-only';
    $('environment-label').textContent = 'Recorded Sepolia workspace';
    $('workspace-expiry').textContent = 'Recorded October 2, 2026 · No live wallet access';
    $('integrity-status').textContent = 'Recorded receipt verified';
    $('new-mandate').disabled = true;
  }
}
async function refresh() {
  $('refresh').disabled = true;
  try {
    if (LOCAL_ENGINE) { await window.EngineConsole?.refresh(); setView(state.view); return; }
    const [snapshot, credentials, health, payments] = await Promise.all([api('/api/workspace'), api('/api/keys'), api('/healthz'), api('/api/payments')]);
    state.snapshot = snapshot;
    state.keys = credentials.keys;
    state.expires = credentials.workspace_expires;
    state.payments = payments;
    state.mode = health.mode;
    connection(health.status === 'ok', PREVIEW ? 'Recorded workspace' : health.status === 'ok' ? 'API connected' : 'API degraded');
    render();
    await window.EngineConsole?.refresh();
    setView(state.view);
    if (PREVIEW) $('primary-action').disabled = true;
  } catch (error) {
    connection(false, 'Connection unavailable');
    notify(error.message || 'Connection failed. Refresh to retry.', true);
    throw error;
  } finally { $('refresh').disabled = false; }
}
function closeDialog(id) {
  if (state.busy === id) return;
  $(id).close();
}
function openKey(preset = 'custom') {
  if (!state.connected) return notify('Connect to the API before creating a key.', true);
  $('key-form').reset();
  $('key-preset').value = typeof preset === 'string' ? preset : 'custom';
  applyKeyPreset();
  formError('key-error');
  updateKeyPolicy();
  $('key-dialog').showModal();
}
function applyKeyPreset() {
  const presets = {
    reader: ['read', 'engine:read', 'data:read'],
    buyer: ['read', 'data:read', 'demands:write', 'payments:request'],
    seller: ['read', 'supplies:write']
  };
  const scopes = presets[$('key-preset').value];
  if (scopes) for (const node of document.querySelectorAll('#key-form input[name="scope"]')) node.checked = scopes.includes(node.value);
  updateKeyPolicy();
}
function updateKeyPolicy() {
  const needsPolicy = $('order-scope').checked;
  $('key-policy-field').hidden = !needsPolicy;
  $('key-policy').required = needsPolicy;
  const policies = activePolicies();
  const selected = $('key-policy').value;
  $('key-policy').replaceChildren();
  for (const policy of policies) {
    const option = el('option', '', `${policyLabel(policy)} · ${credits(policy.max_order_atoms / 1e6)} per order`);
    option.value = policy.id;
    $('key-policy').append(option);
  }
  if (!policies.length) {
    const option = el('option', '', 'No active spending policies');
    option.value = '';
    $('key-policy').append(option);
  } else if (policies.some((p) => p.id === selected)) $('key-policy').value = selected;
  $('key-policy-help').textContent = policies.length ? 'All keys on this policy share one budget.' : 'Create a policy in Service payments before enabling test order execution.';
  const needsMandate = $('payment-scope').checked;
  $('key-mandate-field').hidden = !needsMandate;
  $('key-mandate').required = needsMandate;
  const mandates = state.payments.mandates.filter((p) => p.expires > Date.now() / 1000);
  $('key-mandate').replaceChildren();
  for (const mandate of mandates) {
    const option = el('option', '', `${mandate.id.slice(-8)} · ${credits(mandate.budget / 1e6)} token budget`);
    option.value = mandate.id;
    $('key-mandate').append(option);
  }
  if (!mandates.length) $('key-mandate').append(el('option', '', 'No active payment limits'));
  $('key-mandate-help').textContent = mandates.length ? 'Keys linked to this limit share its budget. Wallet signing stays external.' : 'First create a payment limit in Buy services or Service payments, then return to create this buyer key.';
  $('order-scope').disabled = state.mode === 'production';
  $('create-key-submit').disabled = (needsPolicy && !policies.length) || (needsMandate && !mandates.length);
}
async function createKey(event) {
  event.preventDefault();
  const scopes = [...document.querySelectorAll('input[name="scope"]:checked')].map((node) => node.value);
  if (!scopes.length) return formError('key-error', 'Select at least one permission.');
  state.busy = 'key-dialog';
  $('create-key-submit').disabled = true;
  formError('key-error');
  try {
    const result = await api('/api/keys', {name: $('key-name').value.trim(), scopes, policy_id: scopes.includes('orders:write') ? $('key-policy').value : null, payment_mandate_id: scopes.includes('payments:request') ? $('key-mandate').value : null, ttl_seconds: Number($('key-lifetime').value)});
    state.busy = null;
    closeDialog('key-dialog');
    $('key-secret').value = result.secret;
    $('copy-secret').textContent = 'Copy key';
    $('secret-dialog').showModal();
    await refresh().catch(() => {});
  } catch (error) {
    formError('key-error', error.message || 'Key response was not received. Refresh credentials before retrying.');
  } finally {
    state.busy = null;
    $('create-key-submit').disabled = false;
  }
}
function openRevoke(key) {
  state.revoke = key;
  $('revoke-name').textContent = `${key.name} · ${key.prefix}••••`;
  formError('revoke-error');
  $('revoke-dialog').showModal();
}
async function revokeKey() {
  if (!state.revoke || state.busy) return;
  state.busy = 'revoke-dialog';
  $('confirm-revoke').disabled = true;
  try {
    await api(`/api/keys/${encodeURIComponent(state.revoke.id)}/revoke`, {});
    state.busy = null;
    closeDialog('revoke-dialog');
    notify('API key revoked. Future requests with this key will be rejected.');
    await refresh().catch(() => {});
  } catch (error) { formError('revoke-error', error.message); }
  finally { state.busy = null; $('confirm-revoke').disabled = false; }
}
function openPolicy() {
  if (!state.connected) return notify('Connect to the API before creating a policy.', true);
  $('policy-form').reset();
  formError('policy-error');
  const services = $('policy-services');
  services.replaceChildren();
  for (const offer of state.offers) {
    const label = el('label');
    const check = el('input');
    check.type = 'checkbox'; check.name = 'service'; check.value = offer.id; check.checked = true;
    const text = el('span');
    text.append(el('strong', '', names[offer.id] || offer.id), el('small', '', `${credits(offer.price)} credits per order`));
    label.append(check, text);
    services.append(label);
  }
  $('policy-dialog').showModal();
}
async function createPolicy(event) {
  event.preventDefault();
  state.busy = 'policy-dialog';
  $('create-policy-submit').disabled = true;
  formError('policy-error');
  try {
    await api('/api/policies', {budget: $('policy-budget').value.trim(), max_order: $('policy-max').value.trim(), allowed_offers: [...document.querySelectorAll('input[name="service"]:checked')].map((node) => node.value), ttl_seconds: Number($('policy-lifetime').value)});
    state.busy = null;
    closeDialog('policy-dialog');
    notify('Spending policy created. Link it to an agent key to permit orders.');
    await refresh().catch(() => {});
  } catch (error) { formError('policy-error', error.message); }
  finally { state.busy = null; $('create-policy-submit').disabled = false; }
}
function showOrder(order) {
  $('order-subtitle').textContent = names[order.offer_id] || order.offer_id;
  const detail = $('order-detail');
  detail.replaceChildren();
  const grid = el('div', 'detail-grid');
  for (const [label, value] of [['Order ID', order.id], ['Status', order.status], ['Amount', `${credits(order.amount)} test credits`], ['Created', date(order.created)], ['Spending policy', order.policy_id], ['Settlement', order.receipt ? 'Test ledger' : 'Not settled']]) {
    const cell = el('div');
    cell.append(el('span', '', label), el('strong', '', value));
    grid.append(cell);
  }
  detail.append(grid);
  if (order.reason) detail.append(el('p', 'detail-reason', `Reason: ${order.reason}`));
  const links = el('div', 'detail-links');
  for (const [field, label] of [['artifact', 'Download delivery'], ['receipt', 'Download receipt']]) {
    if (!order[field]) continue;
    const link = el('a', '', label);
    link.href = `/api/orders/${encodeURIComponent(order.id)}/${field}`;
    link.download = `${order.id}-${field}.json`;
    links.append(link);
  }
  detail.append(links);
  for (const [label, value] of [['Verification', order.verification], ['Ledger receipt', order.receipt], ['Recorded hashes', {terms_hash: order.terms_hash, input_hash: order.input_hash}]]) {
    if (!value) continue;
    const section = el('details', 'receipt-json');
    section.append(el('summary', '', label), el('pre', '', JSON.stringify(value, null, 2)));
    detail.append(section);
  }
  $('order-dialog').showModal();
}
async function copy(text, success) {
  try { await navigator.clipboard.writeText(text); success(); }
  catch { notify('Clipboard access is unavailable. Select and copy the text manually.', true); }
}

for (const node of document.querySelectorAll('[data-view]')) node.addEventListener('click', () => setView(node.dataset.view));
$('navigation-toggle').addEventListener('click', () => {
  const open = document.querySelector('.sidebar').classList.toggle('menu-open');
  $('navigation-toggle').setAttribute('aria-expanded', String(open));
  $('navigation-toggle').setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
});
$('help-shortcut').addEventListener('click', () => $('guide-dialog').showModal());
for (const node of document.querySelectorAll('[data-close]')) node.addEventListener('click', () => closeDialog(node.dataset.close));
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel', (event) => { if (state.busy === dialog.id) event.preventDefault(); });
$('secret-dialog').addEventListener('close', () => { $('key-secret').value = ''; });
window.addEventListener('pagehide', () => { $('key-secret').value = ''; });
$('refresh').addEventListener('click', () => window.EngineConsole?.isView(state.view) ? window.EngineConsole.refresh() : refresh().catch(() => {}));
$('balance-shortcut').addEventListener('click', () => setView('funds'));
$('primary-action').addEventListener('click', () => state.view === 'keys' ? openKey() : state.mode === 'production' ? openMandate() : openPolicy());
$('key-form').addEventListener('submit', createKey);
$('key-preset').addEventListener('change', applyKeyPreset);
$('key-limit-setup').addEventListener('click', () => { closeDialog('key-dialog'); setView('funds'); });
$('order-scope').addEventListener('change', updateKeyPolicy);
$('payment-scope').addEventListener('change', updateKeyPolicy);
$('policy-form').addEventListener('submit', createPolicy);
$('confirm-revoke').addEventListener('click', revokeKey);
$('activity-filter').addEventListener('change', renderActivity);
$('copy-secret').addEventListener('click', () => copy($('key-secret').value, () => { $('copy-secret').textContent = 'Copied'; }));
$('copy-workspace').addEventListener('click', () => { if (state.snapshot) copy(state.snapshot.buyer_id, () => notify('Workspace ID copied.')); });
const example = `curl ${window.location.origin}${API_PREFIX}/api/workspace \\\n  -H "Authorization: Bearer $MACHINE_API_KEY"`;
$('api-example').textContent = example;
$('api-endpoint').textContent = window.location.origin + API_PREFIX;
$('copy-example').addEventListener('click', () => copy(example, () => { $('copy-example').textContent = 'Copied'; }));

async function initialize() {
  try {
    setView(headings[location.hash.slice(1)] ? location.hash.slice(1) : 'overview');
    if (LOCAL_ENGINE) {
      document.body.classList.add('local-engine');
      $('connection-label').textContent = 'Owner token required';
      $('mode-badge').textContent = 'Self-hosted';
      $('copy-workspace').hidden = true;
      $('workspace-expiry').textContent = 'Self-hosted engine';
      $('integrity-status').textContent = 'Unlock to inspect journal';
      $('local-owner-access').hidden = false;
      return;
    }
    state.mode = (await api('/healthz')).mode;
    if (PREVIEW) { await refresh(); return; }
    try {
      if (state.mode === 'production') {
        const session = await api('/api/auth/session');
        showWalletIdentity(session.identity);
      } else await api('/api/sessions', {});
    }
    catch (error) {
      if (state.mode !== 'production') throw error;
      connection(false, 'Sign in required');
      $('mode-badge').textContent = 'Wallet login';
      $('environment-label').textContent = 'Sign-in required';
      $('balance-shortcut').hidden = true;
      $('key-list').replaceChildren(empty('Connect your wallet', 'Sign in to manage agent access and payment limits.', null, '', 'wallet'));
      await window.EngineConsole?.refresh();
      setView(state.view);
      return;
    }
    state.offers = (await api('/api/catalog')).offers;
    await refresh();
  } catch (error) {
    connection(false, 'Connection unavailable');
    notify(error.message || 'Could not connect. Reload to retry.', true);
  }
}
function openMandate() {
  if (!state.payments.resource_details.length) return notify('An operator must configure an approved payment resource.', true);
  $('mandate-form').reset();
  formError('mandate-error');
  $('mandate-resource').replaceChildren();
  for (const resource of state.payments.resource_details) {
    const option = el('option', '', `${resource.id} · ${resource.network}`);
    option.value = resource.id;
    $('mandate-resource').append(option);
  }
  $('mandate-dialog').showModal();
}
$('new-mandate').addEventListener('click', openMandate);
$('mandate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.busy = 'mandate-dialog';
  $('mandate-submit').disabled = true;
  try {
    const resource = state.payments.resource_details.find((r) => r.id === $('mandate-resource').value);
    await api('/api/payment-mandates', {payer: $('mandate-payer').value, payment_asset: resource.payment_asset, resources: [resource.id], budget: $('mandate-budget').value, max_order: $('mandate-max').value, ttl_seconds: Number($('mandate-lifetime').value)});
    state.busy = null;
    closeDialog('mandate-dialog');
    await refresh();
  } catch (error) { formError('mandate-error', error.message); }
  finally { state.busy = null; $('mandate-submit').disabled = false; }
});
let activeWallet = null;
let availableWallets = [];
async function restoreSigningWallet() {
  if (activeWallet || !state.identity) return;
  try {
    const hint = JSON.parse(sessionStorage.getItem('skew-wallet-hint') || 'null') || {id:sessionStorage.getItem('skew-wallet-provider')};
    const item = WalletBridge.rememberedProvider(availableWallets, hint);
    if (!item) return;
    const selected = await WalletBridge.accountState(item.provider);
    if (selected.address.toLowerCase() !== state.identity?.address?.toLowerCase()) return;
    if (selected.chain_id !== state.identity.chain_id || activeWallet) return;
    activeWallet = item.provider;
    if (activeWallet.on) { activeWallet.on('accountsChanged', changedWallet); activeWallet.on('chainChanged', changedWallet); }
  } catch { /* A locked wallet is reconnected explicitly, never requested on load. */ }
}
function showWalletIdentity(identity) {
  state.identity = identity;
  $('wallet-account-label').textContent = PREVIEW ? 'Recorded workspace' : identity ? `${identity.address.slice(0, 6)}…${identity.address.slice(-4)}` : 'Connect wallet';
  $('wallet-account').disabled = PREVIEW;
  $('wallet-logout').hidden = PREVIEW || !identity;
  if (identity) $('mandate-payer').value = identity.address;
  if (identity) restoreSigningWallet();
}
function walletError(error) {
  return WalletBridge.connectionError(error);
}
async function walletRequest(path, body) {
  const response = await fetch(API_PREFIX + path, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof (result.error || result.detail) === 'string' ? result.error || result.detail : 'Sign-in could not be verified. Try again.');
  return result;
}
function renderWallets(providers) {
  if (state.busy === 'login-dialog') return;
  $('wallet-options').replaceChildren();
  $('wallet-empty').hidden = providers.length > 0;
  for (const item of [...providers].sort((a, b) => a.name.localeCompare(b.name))) {
    const option = button('', 'wallet-option', () => connectWallet(item));
    const logo = el('span', 'wallet-provider-logo');
    if (item.icon || item.name === 'Phantom') {
      const image = el('img'); image.src = item.icon || API_PREFIX + '/assets/phantom-wallet.png'; image.alt = ''; logo.append(image);
    } else {
      logo.append(uiIcon('wallet'));
    }
    const arrow = el('span', 'wallet-option-arrow'); arrow.append(uiIcon('arrow-right'));
    option.append(logo, el('span', '', item.name), arrow);
    $('wallet-options').append(option);
  }
}
async function changedWallet() {
  if (!state.identity || state.busy === 'login-dialog') return;
  try { await walletRequest('/api/auth/logout', {}); }
  finally { location.reload(); }
}
async function connectWallet(item) {
  if (state.busy) return;
  state.busy = 'login-dialog'; formError('login-error');
  for (const option of $('wallet-options').querySelectorAll('button')) option.disabled = true;
  try {
    // Keep the existing Sepolia DataPass path usable; other networks switch to One.
    const sepolia = Number(await item.provider.request({method:'eth_chainId'})) === 421614;
    const result = await WalletBridge.signIn(item.provider, walletRequest, message => { $('wallet-progress').textContent = message; }, {arbitrum:!sepolia});
    if (activeWallet?.removeListener) { activeWallet.removeListener('accountsChanged', changedWallet); activeWallet.removeListener('chainChanged', changedWallet); }
    activeWallet = item.provider;
    sessionStorage.setItem('skew-wallet-provider', item.id);
    sessionStorage.setItem('skew-wallet-hint', JSON.stringify({id:item.id,rdns:item.rdns,name:item.name}));
    if (activeWallet.on) { activeWallet.on('accountsChanged', changedWallet); activeWallet.on('chainChanged', changedWallet); }
    showWalletIdentity(result.identity);
    state.busy = null; closeDialog('login-dialog'); notify('');
    state.offers = (await api('/api/catalog')).offers; await refresh();
    window.AssistantConsole?.connected();
  } catch (error) { formError('login-error', walletError(error)); }
  finally {
    state.busy = null; $('wallet-progress').textContent = '';
    for (const option of $('wallet-options').querySelectorAll('button')) option.disabled = false;
  }
}
$('wallet-account').addEventListener('click', () => { if (!PREVIEW && !$('login-dialog').open) { formError('login-error'); $('login-dialog').showModal(); } });
$('wallet-logout').addEventListener('click', async () => {
  $('wallet-logout').disabled = true;
  try { await walletRequest('/api/auth/logout', {}); location.reload(); }
  catch (error) { notify(error.message, true); $('wallet-logout').disabled = false; }
});
WalletBridge.subscribe(providers => { availableWallets = providers; renderWallets(providers); restoreSigningWallet(); });
showWalletIdentity(null);
window.MachineConsole = {api, state, notify, el, uiIcon, button, setView, API_PREFIX, PREVIEW, LOCAL_ENGINE,
  refresh, openKey, signIn: () => $('wallet-account').click(),
  engineStatus: record => {
    if (LOCAL_ENGINE) {
      connection(true, 'Owner connected');
      $('workspace-expiry').textContent = 'Self-hosted engine';
      $('integrity-status').textContent = record.runtime?.journal_integrity ? 'Engine journal verified' : 'Engine journal verification failed';
    } else if (record.read_only) {
      $('workspace-expiry').textContent = 'Historical payment evidence';
      $('integrity-status').textContent = 'No live agent workspace connected';
    }
  },
  showSecret: secret => { $('key-secret').value = secret; $('copy-secret').textContent = 'Copy key'; $('secret-dialog').showModal(); },
  getWallet: () => activeWallet,
  unlock: async token => {localOwnerToken = token; await api('/api/engine/overview'); await refresh();}};
window.addEventListener('DOMContentLoaded', initialize, {once: true});
