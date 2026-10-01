'use strict';

const $ = (id) => document.getElementById(id);
const state = {view: 'keys', snapshot: null, keys: [], offers: [], expires: null, busy: null, revoke: null, connected: false};
const names = {'csv-normalize': 'CSV normalization', 'arbitrum-state': 'Arbitrum state data'};
const scopeNames = {read: 'Read', 'demands:write': 'Demand', 'supplies:write': 'Supply', 'orders:write': 'Orders', 'payments:request': 'Payment requests'};
const headings = {
  keys: ['API keys', 'Give your agents access. Keep control of what they can spend.', 'Create API key'],
  funds: ['Funds & limits', 'Set the boundaries. Your agents operate within them.', 'Create policy'],
  activity: ['Activity', 'Track execution, delivery and settlement in your workspace.', null]
};

function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = text;
  return node;
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
  const options = {credentials: 'same-origin', headers: {Accept: 'application/json'}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); } catch { throw new Error('The server returned an unreadable response. Refresh and try again.'); }
  if (!response.ok) {
    if (response.status === 401) throw new Error('This test workspace has expired. Reload to open a new workspace.');
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
  for (const name of Object.keys(headings)) $(`${name}-view`).hidden = name !== view;
  for (const node of document.querySelectorAll('[data-view]')) {
    if (node.dataset.view === view) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  }
  const [title, description, action] = headings[view];
  $('page-title').textContent = title;
  $('breadcrumb-current').textContent = title;
  $('page-description').textContent = description;
  $('primary-action').hidden = !action;
  $('primary-action').replaceChildren(el('span', '', '+'), document.createTextNode(action || ''));
}
function empty(title, description, action, actionText = '') {
  const node = el('div', 'empty');
  node.append(el('div', 'empty-icon', '{ }'), el('h3', '', title), el('p', '', description));
  if (action) node.append(button(actionText, 'button primary', action));
  return node;
}
function renderKeys() {
  $('key-count').textContent = state.keys.length;
  const list = $('key-list');
  list.replaceChildren();
  if (!state.keys.length) {
    list.append(empty('Connect your first agent', 'Create an API key with the permissions your agent needs. Add a spending policy when it needs to buy.', openKey, 'Create API key'));
    return;
  }
  for (const key of state.keys) {
    const row = el('article', 'key-row');
    const main = el('div', 'row-main');
    const identity = el('div', 'key-identity');
    identity.append(el('h3', '', key.name), tag(key.status));
    main.append(identity);
    if (key.status === 'active') main.append(button('Revoke', 'revoke-button', () => openRevoke(key)));
    const meta = el('div', 'key-meta');
    meta.append(el('span', '', `Expires ${date(key.expires)}`), el('span', '', key.last_used ? `Last used ${date(key.last_used)}` : 'Never used'));
    const scopes = el('div', 'scopes');
    for (const scope of key.scopes) scopes.append(el('span', 'scope', scopeNames[scope] || scope));
    row.append(main, el('div', 'key-prefix', `${key.prefix}••••••••`), meta, scopes);
    if (key.policy_id) {
      const policy = state.snapshot.policies.find((p) => p.id === key.policy_id);
      row.append(el('div', 'policy-info', `Spending policy: ${policy ? policyLabel(policy) : key.policy_id}`));
    }
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
    list.append(empty('Define a spending boundary', 'Approve a total budget, a per-order limit and the services an agent may use.', openPolicy, 'Create policy'));
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
  $('order-count').textContent = orders.length;
  const filter = $('activity-filter').value;
  const visible = orders.filter((order) => filter === 'all' || order.status === filter || (filter === 'pending' && !['SETTLED', 'REFUNDED'].includes(order.status)));
  const list = $('order-list');
  list.replaceChildren();
  if (!visible.length) {
    list.append(empty(orders.length ? 'No transactions in this view' : 'Activity starts with your agents', orders.length ? 'Choose another status to see your transactions.' : 'Orders placed through the API appear here with delivery status and settlement records.'));
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
}
function render() {
  renderKeys();
  renderFunds();
  renderActivity();
  $('workspace-expiry').textContent = `Test workspace expires ${date(state.expires)}`;
  $('integrity-status').textContent = state.snapshot.journal_integrity ? 'Ledger integrity verified' : 'Ledger integrity check failed';
}
async function refresh() {
  $('refresh').disabled = true;
  try {
    const [snapshot, credentials, health] = await Promise.all([api('/api/workspace'), api('/api/keys'), api('/healthz')]);
    state.snapshot = snapshot;
    state.keys = credentials.keys;
    state.expires = credentials.workspace_expires;
    connection(health.status === 'ok', health.status === 'ok' ? 'API connected' : 'API degraded');
    render();
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
function openKey() {
  if (!state.connected) return notify('Connect to the API before creating a key.', true);
  $('key-form').reset();
  formError('key-error');
  updateKeyPolicy();
  $('key-dialog').showModal();
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
  $('key-policy-help').textContent = policies.length ? 'All keys on this policy share one budget.' : 'Create a policy in Funds & limits before enabling order execution.';
  $('create-key-submit').disabled = needsPolicy && !policies.length;
}
async function createKey(event) {
  event.preventDefault();
  const scopes = [...document.querySelectorAll('input[name="scope"]:checked')].map((node) => node.value);
  if (!scopes.length) return formError('key-error', 'Select at least one permission.');
  state.busy = 'key-dialog';
  $('create-key-submit').disabled = true;
  formError('key-error');
  try {
    const result = await api('/api/keys', {name: $('key-name').value.trim(), scopes, policy_id: scopes.includes('orders:write') ? $('key-policy').value : null, ttl_seconds: Number($('key-lifetime').value)});
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
for (const node of document.querySelectorAll('[data-close]')) node.addEventListener('click', () => closeDialog(node.dataset.close));
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel', (event) => { if (state.busy === dialog.id) event.preventDefault(); });
$('secret-dialog').addEventListener('close', () => { $('key-secret').value = ''; });
window.addEventListener('pagehide', () => { $('key-secret').value = ''; });
$('refresh').addEventListener('click', () => refresh().catch(() => {}));
$('balance-shortcut').addEventListener('click', () => setView('funds'));
$('primary-action').addEventListener('click', () => state.view === 'keys' ? openKey() : openPolicy());
$('key-form').addEventListener('submit', createKey);
$('order-scope').addEventListener('change', updateKeyPolicy);
$('policy-form').addEventListener('submit', createPolicy);
$('confirm-revoke').addEventListener('click', revokeKey);
$('activity-filter').addEventListener('change', renderActivity);
$('copy-secret').addEventListener('click', () => copy($('key-secret').value, () => { $('copy-secret').textContent = 'Copied'; }));
$('copy-workspace').addEventListener('click', () => { if (state.snapshot) copy(state.snapshot.buyer_id, () => notify('Workspace ID copied.')); });
const example = `curl ${window.location.origin}/api/workspace \\\n  -H "Authorization: Bearer $MACHINE_API_KEY"`;
$('api-example').textContent = example;
$('api-endpoint').textContent = window.location.origin;
$('copy-example').addEventListener('click', () => copy(example, () => { $('copy-example').textContent = 'Copied'; }));

async function initialize() {
  try {
    const catalog = await api('/api/catalog');
    state.offers = catalog.offers;
    await api('/api/sessions', {});
    await refresh();
  } catch (error) {
    connection(false, 'Connection unavailable');
    notify(error.message || 'Could not connect. Reload to retry.', true);
  }
}
initialize();
