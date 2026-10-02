'use strict';
const PORTAL = 'https://machine.148-113-153-116.nip.io/commerce';
if (window.location.hash === '#engine') window.location.replace(PORTAL + '/engine');
const $ = id => document.getElementById(id);
const node = (tag, text, cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
async function run(caseName, button) {
  button.disabled = true;
  try {
    const response = await fetch(`${PORTAL}/demo/run`, {method: 'POST', credentials: 'omit', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({scenario: caseName}), signal: AbortSignal.timeout(15000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The engine is unavailable. Try again shortly.');
    return data;
  } finally { button.disabled = false; }
}
function providerMark(index) {
  const providers = ['orbit', 'relay', 'archive', 'prism', 'signal', 'scope'];
  const mark = document.createElement('img');
  mark.src = `assets/provider-${providers[index] || providers[0]}.svg`;
  mark.alt = ''; mark.width = 36; mark.height = 36;
  return mark;
}

const reasons = {PRICE_OUTSIDE_BUYER_POLICY:'Price exceeds policy', DATA_NOT_FRESH:'Data is too old', LICENSE_NOT_GRANTED:'License not granted', PURPOSE_NOT_GRANTED:'Purpose not granted', REFRESH_TOO_SLOW:'Refresh too slow', RESPONSE_TOO_SLOW:'Response too slow'};
$('run-policy').addEventListener('click', async () => {
  $('scenario').disabled = true; $('run-status').classList.remove('error-state'); $('run-status').textContent = 'Evaluating registered supplier rules…';
  try {
    const result = await run($('scenario').value, $('run-policy'));
    renderHero(result);
    $('supplier-results').replaceChildren();
    for (const s of result.suppliers) {
      const row = node('div', '', 'supplier-row'); const identity = node('div', ''); const mark = node('span', '', `provider-logo mark-${s.mark}`); mark.append(providerMark(s.mark)); const label=node('div',''); label.append(node('strong', s.name), node('small', `${s.age_seconds}s old`)); identity.className='provider-identity'; identity.append(mark,label);
      const price = node('div', ''); price.append(node('strong', `${s.total_price} cr`), node('small', `${s.ask_price} cr / unit`));
      const decision = node('div', s.status === 'AGREED' ? 'Match' : reasons[s.reason_codes[0]] || 'Review required', `supplier-decision ${s.status === 'AGREED' ? 'accepted' : 'deferred'}`);
      decision.title=s.reason_codes.map(r=>reasons[r]||r).join('; '); row.append(identity, price, decision); $('supplier-results').append(row);
    }
    $('match-count').textContent = `${result.compatible} of ${result.suppliers.length} compatible`;
    $('run-status').textContent = `One buyer policy evaluated ${result.suppliers.length} providers. ${result.language_model_calls} model calls.`;
    $('decision-trace').hidden = false;
    $('decision-trace').replaceChildren(node('strong', result.trace_title), node('p', result.trace_summary));
  } catch (e) { $('run-status').textContent = e.message || 'Comparison unavailable. Retry the policy.'; $('run-status').classList.add('error-state'); $('hero-matches').textContent = 'Unavailable'; $('hero-outcome').textContent = 'Retry the comparison below.'; $('hero-offers').replaceChildren(node('p','Engine temporarily unavailable.','quote-placeholder')); }
  finally { $('scenario').disabled=false; }
});
function renderHero(result) {
  $('hero-price-cap').textContent = Number(result.policy.max_total_price).toFixed(2) + ' credits';
  $('hero-freshness').textContent = result.policy.max_age_seconds + ' seconds';
  $('hero-matches').textContent = `${result.compatible} of ${result.suppliers.length}`;
  $('hero-offers').replaceChildren();
  const compatible = result.suppliers.filter(s => s.status === 'AGREED').sort((a,b) => Number(a.total_price)-Number(b.total_price));
  for (const s of compatible) {
    const row = node('div','','quote-offer');
    const logo = node('span','',`provider-logo mark-${s.mark}`); logo.append(providerMark(s.mark));
    const identity = node('div','', 'quote-provider'); identity.append(node('strong',s.name),node('small',`${s.age_seconds}s old · License approved`));
    const price = node('div','','quote-price'); price.append(node('strong',Number(s.total_price).toFixed(2)),node('small','credits / total'));
    row.append(logo,identity,price); $('hero-offers').append(row);
  }
  if (!compatible.length) $('hero-offers').append(node('p','No offer meets this policy.','quote-placeholder'));
  $('hero-outcome').textContent = compatible.length ? 'Terms matched. Your policy enforced.' : 'Outside the policy. No trade admitted.';
}
for (const [id, scenario] of [['check-budget','budget'],['check-recovery','recovery']]) $(id).addEventListener('click', async () => {
  $('boundary-result').textContent = 'Running the isolated test-credit scenario…'; $('boundary-result').classList.remove('error-state');
  try {
    const result = await run(scenario, $(id)); $('boundary-title').textContent = result.title; $('boundary-steps').replaceChildren();
    for (const step of result.steps) { const li = node('li',''); li.append(node('strong',step.title),node('span',step.description)); $('boundary-steps').append(li); }
    $('boundary-result').textContent = result.summary;
  } catch (e) { $('boundary-result').textContent = e.message || 'Scenario unavailable. Try again.'; $('boundary-result').classList.add('error-state'); }
});
const studies = {
 requests: {label:'Warm-path discovery and negotiation', number:'87.5%', unit:'fewer requests', description:'192 → 24 requests. Both paths: 18/24 valid agreements.', title:'A controlled HTTP comparison', context:'64 fixture suppliers, 24 paired requests and real loopback HTTP. Both code paths use zero LLM tokens.', limit:'Setup is additional: 65 registration requests. The first 24-trade workload was faster overall with the cached direct client. This is a warm-path result, not a customer conversion claim.', bars:[['Cached direct',192],['Machine',24]], link:'evidence.json', linkText:'Inspect the evidence'},
 tokens: {label:'Actual Qwen3-32B per-event loop', number:'99.66%', unit:'fewer tokens, including initial policy interpretation', description:'122,242 → 417 tokens. Completed feasible trades: 10/24 → 24/24.', title:'A bounded model comparison', context:'Live Kiln Qwen3-32B calls and verified ephemeral test-token payment/delivery. Same trading conditions; all repair calls included.', limit:'The cached deterministic direct client also completed 24/24 with 417 startup tokens. This is an advantage over the measured model loop, not over every agent or total operating cost. No customer traffic was measured.', bars:[['Qwen loop',122242],['Machine',417]], link:'evidence.json', linkText:'Read scope and measurements'},
 payment: {label:'Public Arbitrum Sepolia x402 purchase', number:'0.01', unit:'Circle test USDC, finalized', description:'Data delivered. 0.01 spent. Zero held.', title:'One completed agent purchase', context:'The chain signature matches the runtime authorization. Two RPCs reconfirm the receipt, consumed nonce and delivered block. Historical balances are preserved from the earlier audit.', limit:'Disposable test wallets. One approved seller/order. Recorded replay, not a new purchase. RPC-backed finality; off-chain delivery is not atomic with payment. Our separate escrow is not publicly deployed.', bars:[['Spent · test USDC',0.01],['Held',0]], link:'https://machine.148-113-153-116.nip.io/commerce/submission', linkText:'Inspect the completed trade'}
};
const tabs = [...document.querySelectorAll('[data-study]')];
function selectStudy(tab) {
  const s = studies[tab.dataset.study];
  for(const t of tabs) { const selected=t===tab; t.setAttribute('aria-selected', String(selected)); t.tabIndex=selected?0:-1; }
  $('evidence-label').textContent=s.label; $('evidence-number').replaceChildren(document.createTextNode(s.number), node('span',s.unit));
  $('evidence-description').textContent=s.description; $('evidence-context-title').textContent=s.title; $('evidence-context-copy').textContent=s.context; $('evidence-limit').textContent=s.limit;
  $('evidence-link').href=s.link; $('evidence-link').textContent=s.linkText; $('comparison-bars').replaceChildren();
  const max=Math.max(...s.bars.map(b=>b[1]));
  for(const [label,value] of s.bars) {const row=node('div','','bar-row');const track=node('div','','bar-track');const fill=node('div','',`bar-fill ${label==='Machine'?'machine':''}`);fill.style.width=`${max ? value/max*100 : 0}%`;track.append(fill);row.append(node('span',label),track,node('span',value.toLocaleString('en-US',{maximumFractionDigits:6}),'bar-value'));$('comparison-bars').append(row);}
}
for(const tab of tabs){tab.addEventListener('click',()=>selectStudy(tab));tab.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();let n=tabs.indexOf(tab);n=e.key==='Home'?0:e.key==='End'?tabs.length-1:(n+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectStudy(tabs[n]);tabs[n].focus();});}
selectStudy(tabs[0]);
const example=`curl ${PORTAL}/api/demands \\\n  -H "Authorization: Bearer $MACHINE_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{\n    "data_type": "arbitrum.finalized-block",\n    "purpose": "research",\n    "license": "internal-use",\n    "units": 1,\n    "max_unit_price": "0.01",\n    "max_total_price": "0.01",\n    "max_age_seconds": 86400,\n    "max_refresh_seconds": 86400,\n    "response_seconds": 30,\n    "ttl_seconds": 3600,\n    "payment_asset": "eip155:421614/erc20:0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"\n  }'`;
$('developer-code').textContent=example;
$('copy-code').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(example);$('copy-code').textContent='Copied';}catch{$('copy-code').textContent='Select and copy the request below';}});

$('scenario').addEventListener('change', () => $('run-policy').click());
$('run-policy').click();
