"use strict";
const $ = (id) => document.getElementById(id);
const state = {offers: [], snapshot: null, policy: null, offer: null, busy: false, selected: null,
  attempt: null, market: null};
const phases = {RESERVED:"대금 예약",FULFILLING:"납품 중",DELIVERED:"납품 완료",VERIFIED:"검증 통과",
  SETTLED:"지급 완료",REFUNDED:"환불 완료"};
const icons = {"csv-normalize":"≡","arbitrum-state":"◇"};

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = `notice${error ? " error" : ""}`;
  $("notice").hidden = !message;
}
async function api(path, method = "GET", body = undefined) {
  const response = await fetch(path,{method,credentials:"same-origin",cache:"no-store",
    headers:method === "GET" ? {} : {"Content-Type":"application/json"},
    body:method === "GET" ? undefined : JSON.stringify(body || {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.detail || "요청을 처리하지 못했습니다.");
  return result;
}
function short(value) { return value ? `${value.slice(0,9)}…${value.slice(-5)}` : "—"; }
function clock(value) { return new Date(value * 1000).toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit",second:"2-digit"}); }
function setBusy(busy) {
  state.busy = busy;
  $("buyer-state").textContent = busy ? "거래 중" : state.policy ? "준비" : "대기";
  $("run-agent").disabled = busy || !state.policy;
  $("purchase-submit").disabled = busy || !state.policy;
  $("save-policy").disabled = busy;
  for (const button of document.querySelectorAll(".buy-service")) button.disabled = busy;
}

function renderCatalog() {
  $("catalog").replaceChildren();
  for (const offer of state.offers) {
    const card = node("article","service-card");
    const meta = node("div","service-meta");
    meta.append(node("span","",offer.provider),node("span","",offer.category));
    const bottom = node("div","service-bottom");
    const price = node("div","service-price",offer.price);
    price.append(node("small","","test credits / 주문"));
    const button = node("button","button secondary buy-service","서비스 구매");
    button.type = "button";
    button.addEventListener("click",() => openPurchase(offer));
    bottom.append(price,button);
    card.append(node("div","service-icon",icons[offer.id]),meta,node("h3","",offer.name),
      node("p","",offer.description),node("div","service-verification",`✓ ${offer.verification}`),bottom);
    $("catalog").append(card);
  }
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  state.policy = snapshot.policies.find(p => p.expires > Date.now()/1000) || null;
  $("balance").textContent = snapshot.balance;
  $("reserved").textContent = snapshot.reserved;
  $("spent").textContent = snapshot.spent;
  $("order-count").textContent = snapshot.orders.length;
  $("policy-state").textContent = state.policy ? "구매 허용" : "설정 전";
  $("policy-state").className = `tag${state.policy ? " ready" : ""}`;
  if (state.policy) {
    const p = state.policy;
    const remaining = (p.budget_atoms-p.reserved_atoms-p.spent_atoms)/1000000;
    $("budget-remaining").textContent = `남은 예산 ${remaining.toFixed(4).replace(/0+$/,"").replace(/\.$/,"")} credits`;
    $("budget-meter-fill").style.width = `${Math.min(100,(p.reserved_atoms+p.spent_atoms)/p.budget_atoms*100)}%`;
    $("save-policy").textContent = "새 구매 규칙 설정";
  }
  renderOrders($("recent-orders"),snapshot.orders.slice(0,4));
  renderOrders($("all-orders"),snapshot.orders);
  setBusy(state.busy);
}

function renderOrders(parent,orders) {
  parent.replaceChildren();
  if (!orders.length) {
    const empty = node("div","empty-orders");
    empty.append(node("strong","","첫 거래를 시작하세요"),node("span","","예산을 설정하고 필요한 서비스를 구매합니다."));
    parent.append(empty);return;
  }
  for (const order of orders) {
    const offer = state.offers.find(o => o.id === order.offer_id);
    const button = node("button","order-row");button.type = "button";
    const info = node("div","order-info");
    info.append(node("strong","",offer?.name || order.offer_id),node("small","",`${short(order.id)} · ${clock(order.created)}`));
    button.append(node("span","order-icon",icons[order.offer_id]),info,node("span","order-price",`${order.amount} credits`),
      node("span",`phase-tag ${order.status.toLowerCase()}`,phases[order.status] || order.status));
    button.addEventListener("click",() => inspectOrder(order).catch(e => notice(e.message,true)));
    parent.append(button);
  }
}

async function refresh() {
  const [snapshot,market] = await Promise.all([api("/api/workspace"),api("/api/market")]);
  renderSnapshot(snapshot);renderMarket(market);
  if (state.selected) {
    const updated = snapshot.orders.find(o => o.id === state.selected);
    if(updated) await inspectOrder(updated);
  }
}

function editorField(id,label,value,multiline=false) {
  const wrap = node("div");const title = node("label","editor-label",label);title.htmlFor=id;
  const input = node(multiline ? "textarea" : "input","request-input");input.id=id;input.value=value;
  if(!multiline) input.type="text";
  if(multiline) input.maxLength=40000;
  wrap.append(title,input);return wrap;
}
function openPurchase(offer) {
  if(state.busy)return;
  if(state.offer?.id!==offer.id)state.attempt=null;
  state.offer=offer;
  $("purchase-provider").textContent=offer.provider;
  $("purchase-title").textContent=offer.name;
  $("purchase-price").textContent=`${offer.price} test credits`;
  $("purchase-rule").textContent=state.policy ? "설정된 구매 규칙을 확인하고 대금을 예약합니다. 검증 실패 시 지급하지 않습니다." : "오른쪽 Buyer desk에서 구매 예산을 먼저 설정하세요.";
  $("request-editor").replaceChildren();
  if(offer.id==="csv-normalize") {
    $("request-editor").append(editorField("csv-columns","열 이름 (쉼표로 구분)",offer.default_request.columns.join(",")),
      editorField("csv-numeric","숫자로 정규화할 열",offer.default_request.numeric_columns.join(",")),
      editorField("csv-data","변환할 CSV",offer.default_request.csv,true));
  } else {
    const info=node("div","delivery-preview","Arbitrum Sepolia (421614)\n최신 블록·시간·가스 가격\n최대 데이터 나이: 120초\n출처: Arbitrum 공식 RPC");
    $("request-editor").append(info);
  }
  setBusy(false);$("purchase-dialog").showModal();
}
function readRequest(offer) {
  if(offer.id!=="csv-normalize")return structuredClone(offer.default_request);
  const split=value => value.split(",").map(x=>x.trim()).filter(Boolean);
  return {columns:split($("csv-columns").value),numeric_columns:split($("csv-numeric").value),csv:$("csv-data").value};
}

async function purchase(offer,request,key) {
  const order=await api("/api/orders","POST",{policy_id:state.policy.id,offer_id:offer.id,request,idempotency_key:key});
  await refresh();
  const done=await api(`/api/orders/${order.id}/run`,"POST");
  await refresh();await inspectOrder(done);
  return done;
}

async function inspectOrder(order) {
  state.selected=order.id;
  const host=$("delivery-inspector");host.replaceChildren();
  const heading=node("div","section-heading");heading.append(node("h3","","납품과 영수증"),node("span",`phase-tag ${order.status.toLowerCase()}`,phases[order.status]));
  host.append(heading,node("h4","delivery-title",state.offers.find(o=>o.id===order.offer_id)?.name || order.offer_id),node("div","delivery-id",order.id));
  const events=await api(`/api/orders/${order.id}/events`);
  const timeline=node("div","timeline");
  for(const event of events.events) {
    const row=node("div","timeline-item");row.append(node("i"),node("strong","",phases[event.kind] || event.kind),node("span","",clock(event.event.at || event.event.settled_at)));
    timeline.append(row);
  }
  host.append(timeline);
  if(order.reason)host.append(node("p","purchase-rule",`환불 사유: ${order.reason}`));
  if(order.artifact) {
    host.append(node("div","delivery-preview",JSON.stringify(order.artifact,null,2)));
    const links=node("div","delivery-links");const artifact=node("a","","납품 JSON 받기");
    artifact.href=`/api/orders/${order.id}/artifact`;links.append(artifact);
    if(order.receipt) { const receipt=node("a","","영수증 받기");receipt.href=`/api/orders/${order.id}/receipt`;links.append(receipt); }
    host.append(links);
  }
  if(order.receipt) {
    for(const [label,value] of [["공급자 지급",`${order.receipt.provider_amount} credits`],["서비스 수수료",`${order.receipt.platform_fee} credits`],["정산 방식","테스트 크레딧 장부"]]) {
      const fact=node("div","receipt-fact");fact.append(node("span","",label),node("b","",value));host.append(fact);
    }
    host.append(node("div","receipt-proof",`영수증 SHA-256\n${order.receipt.receipt_hash}`));
  }
}

function selectTab(tab) {
  if(!state.busy)notice("");
  for(const button of document.querySelectorAll("[data-tab]")) {
    if(button.dataset.tab===tab)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");
  }
  $("matching-view").hidden=tab!=="matching";$("market-view").hidden=tab!=="market";$("orders-view").hidden=tab!=="orders";
  document.querySelector(".market-shell").classList.toggle("matching-mode",tab==="matching");
  document.querySelector(".inspector").hidden=tab==="matching";
  const labels={matching:["양쪽의 조건이 맞으면, 거래가 시작됩니다.","구매 수요와 판매 규칙을 등록하면 머신이 매칭하고 협상합니다."],
    market:["머신이 필요한 일을 구매합니다.","예제 공급자의 서비스를 테스트 크레딧으로 구매합니다."],
    orders:["주문부터 납품까지, 거래 내역.","각 거래의 결과·검증·지급 또는 환불을 확인합니다."]};
  $("page-title").textContent=labels[tab][0];$("page-description").textContent=labels[tab][1];
}

const reasonNames={PURPOSE_NOT_GRANTED:"사용 목적 미허용",LICENSE_NOT_GRANTED:"사용권 불일치",
  QUANTITY_OUTSIDE_RULES:"수량 범위 초과",DATA_NOT_FRESH:"최신성 기준 미달",REFRESH_TOO_SLOW:"갱신 주기 초과",
  RESPONSE_TOO_SLOW:"응답 기한 초과",PRICE_OUTSIDE_BUYER_POLICY:"구매 예산 초과"};
function renderMarket(market) {
  state.market=market;$("match-count").textContent=`${market.matches.length}개 · 결제 전`;
  $("matches").replaceChildren();
  if(!market.matches.length)$("matches").append(node("div","empty-orders","수요를 등록하면 다른 공급자의 판매 규칙과 비교합니다. 조건이 맞는 거래를 이곳에서 확인하세요."));
  for(const match of market.matches) {
    const t=match.terms;
    const card=node("article","agreement");const title=node("div","agreement-title");
    title.append(node("h3","",t.data_type),node("span","tag ready","조건 합의 · 미결제"));
    const details=node("p","",`${t.units}개 · ${t.total_price} credits · ${t.license} · ${t.data_version}`);
    const trace=node("div","negotiation-trace");
    for(const item of match.trace)trace.append(node("span","",item.kind==="OFFER" ? `제안 ${item.unit_price}` : item.kind==="POLICY_COUNTER" ? `규칙 적용 ${item.unit_price}` : `수락 ${item.total_price}`));
    const payment=node("button","button secondary","결제 연결 상태 확인");payment.type="button";
    payment.addEventListener("click",async()=>{
      payment.disabled=true;
      try {
        const result=await api(`/api/matches/${match.id}/payment-request`,"POST",{terms_hash:match.terms_hash});
        notice(result.status==="BLOCKED" ? "조건은 합의됐습니다. 실제 결제 지갑·x402 연결이 아직 설정되지 않아 서명하거나 지급하지 않았습니다." : "결제 상태를 확인했습니다.");
      } catch(e){notice(e.message,true);await refresh();}finally{payment.disabled=false;}
    });
    const foot=node("div","agreement-foot");foot.append(node("small","",`합의 만료 ${clock(match.expires)} · LLM 호출 ${match.language_model_calls}회`),payment);
    card.append(title,details,trace,node("div","receipt-proof",`조건 해시 ${match.terms_hash}`),foot);$("matches").append(card);
  }
  $("deferred").replaceChildren();
  if(market.deferred.length) {
    $("deferred").append(node("h3","","추가 판단이 필요한 조건"));
    for(const item of market.deferred)$("deferred").append(node("p","",item.reason_codes.map(r=>reasonNames[r] || r).join(" · ")));
  }
  $("supplies").replaceChildren();
  if(!market.supplies.length)$("supplies").append(node("p","muted","공급자가 판매 규칙을 등록하면 표시됩니다."));
  for(const supply of market.supplies) {
    const row=node("div","supply-row");const info=node("div");
    info.append(node("strong","",supply.name),node("small","",`${supply.data_type} · ${supply.version} · 갱신 ${clock(supply.updated_at)}`));
    row.append(info,node("span","",`${supply.unit_price} credits / 개`));
    if(supply.owner===state.snapshot?.buyer_id) {
      const update=node("button","button secondary","새 버전 알리기");update.type="button";
      update.addEventListener("click",async()=>{
        try {await api(`/api/supplies/${supply.id}/refresh`,"POST",{version:$("supply-version").value});await refresh();notice("데이터 버전 변경을 반영했습니다. 같은 버전은 최신성을 연장하지 않습니다.");}
        catch(e){notice(e.message,true);}
      });row.append(update);
    }
    $("supplies").append(row);
  }
}
const fieldInt=id=>Number($(id).value);
$("demand-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector("button");button.disabled=true;
  try {
    const result=await api("/api/demands","POST",{data_type:$("demand-type").value,purpose:$("demand-purpose").value,
      license:$("demand-license").value,units:fieldInt("demand-units"),max_unit_price:$("demand-unit-price").value,
      max_total_price:$("demand-total").value,max_age_seconds:fieldInt("demand-age"),
      max_refresh_seconds:fieldInt("demand-refresh"),response_seconds:fieldInt("demand-response"),ttl_seconds:3600});
    await refresh();notice(`수요를 등록했습니다. ${result.matching.pairs_evaluated}개 공급 조건 비교, ${result.matching.compatible_pairs}개 합의. LLM 호출 0회.`);
  } catch(e){notice(e.message,true);}finally{button.disabled=false;}
});
$("supply-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector("button");button.disabled=true;
  try {
    await api("/api/supplies","POST",{name:$("supply-name").value,data_type:$("supply-type").value,
      version:$("supply-version").value,unit_price:$("supply-price").value,floor_price:$("supply-floor").value,
      discount_bps:Math.round(fieldInt("supply-discount")*100),discount_min_units:10,min_units:1,max_units:100,
      purposes:[$("supply-purpose").value],licenses:[$("supply-license").value],updated_at:Math.floor(Date.now()/1000),
      refresh_seconds:fieldInt("supply-refresh"),response_seconds:fieldInt("supply-response"),ttl_seconds:3600});
    await refresh();notice("판매 규칙을 등록했습니다. 다른 구매자의 수요가 들어오면 규칙에 맞춰 협상합니다.");
  } catch(e){notice(e.message,true);}finally{button.disabled=false;}
});
for(const button of document.querySelectorAll("[data-tab]"))button.addEventListener("click",()=>selectTab(button.dataset.tab));
$("show-orders").addEventListener("click",()=>selectTab("orders"));
$("refresh").addEventListener("click",()=>refresh().catch(e=>notice(e.message,true)));
$("close-purchase").addEventListener("click",()=>$("purchase-dialog").close());
$("policy-form").addEventListener("submit",async event=>{
  event.preventDefault();setBusy(true);
  try {
    await api("/api/policies","POST",{budget:$("budget").value,max_order:$("max-order").value,
      allowed_offers:state.offers.map(o=>o.id),ttl_seconds:3600});
    await refresh();notice("구매 규칙을 설정했습니다. Research Buyer가 이 예산 안에서 서비스를 구매합니다.");
  } catch(e) {notice(e.message,true);} finally {setBusy(false);}
});
$("purchase-form").addEventListener("submit",async event=>{
  event.preventDefault();if(state.busy || !state.policy)return;
  const request=readRequest(state.offer);
  const signature=JSON.stringify({request,policy:state.policy.id,offer:state.offer.id});
  if(!state.attempt || state.attempt.signature!==signature)state.attempt={signature,key:crypto.randomUUID()};
  setBusy(true);$("purchase-submit").textContent="납품·검증·정산 중…";
  try {
    const result=await purchase(state.offer,request,state.attempt.key);
    state.attempt=null;$("purchase-dialog").close();
    notice(result.status==="SETTLED" ? "납품을 검증하고 공급자에게 지급했습니다. 오른쪽에서 결과와 영수증을 확인하세요." : `현재 거래 상태: ${phases[result.status]}`,result.status==="REFUNDED");
  } catch(e) {notice(e.message,true);$("purchase-dialog").close();}
  finally {setBusy(false);$("purchase-submit").textContent="예약하고 서비스 구매";}
});
$("run-agent").addEventListener("click",async()=>{
  if(state.busy || !state.policy)return;
  setBusy(true);notice("Research Buyer가 두 서비스를 순서대로 구매합니다.");
  const results=[];
  try {
    for(const offer of state.offers)results.push(await purchase(offer,offer.default_request,crypto.randomUUID()));
    const settled=results.filter(o=>o.status==="SETTLED").length;
    notice(`${results.length}개 주문 중 ${settled}개를 검증·지급했습니다. 납품 실패 건은 환불되었습니다.`,settled!==results.length);
  } catch(e) {notice(e.message,true);} finally {setBusy(false);}
});

async function start() {
  try {
    const [catalog,session]=await Promise.all([api("/api/catalog"),api("/api/sessions","POST")]);
    state.offers=catalog.offers;renderCatalog();renderSnapshot(session.snapshot);
    renderMarket(await api("/api/market"));
    $("connection-dot").classList.add("connected");$("connection-label").textContent="Economic Machine 연결됨";
  } catch(e) {notice(e.message,true);$("connection-label").textContent="연결을 확인해 주세요";}
}
start();
