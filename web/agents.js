"use strict";

(() => {
  const C = window.MachineConsole;
  const {el, button, notify} = C;
  const titles = {SYNC_CONNECTION: "Sync account", NATIVE_CANDIDATE: "Evaluate native program", PLAN_VENUE_ORDER: "Prepare venue order", ECONOMIC_DECISION: "Assess financial state"};
  let sequence = 0;
  function field(form, title, kind = "text", value = "") {
    const label = el("label", "", title), input = el(kind === "select" ? "select" : "input");
    input.id = `agent-field-${++sequence}`;
    label.htmlFor = input.id;
    if (kind !== "select") {input.type = kind; input.value = value; input.autocomplete = "off";}
    form.append(label, input);
    return input;
  }
  function options(input, choices) {
    input.replaceChildren();
    for (const [value, title] of choices) {const option = el("option", "", title); option.value = value; input.append(option);}
  }
  function picks(form, title, choices) {
    const group = el("fieldset", "agent-picks");
    group.append(el("legend", "", title));
    for (const [value, title] of choices) {
      const label = el("label"), box = el("input"); box.type = "checkbox"; box.value = value;
      label.append(box, el("span", "", title)); group.append(label);
    }
    form.append(group);
    return () => [...group.querySelectorAll("input:checked")].map(node => node.value);
  }
  function section(title, note = "") {
    const node = el("section", "ops-section"), head = el("div", "ops-section-head");
    head.append(el("h2", "", title), el("span", "", note)); node.append(head); return node;
  }
  function submit(form, title, allowed, fn) {
    const action = el("button", "button primary", title); action.type = "submit"; action.disabled = !allowed;
    form.append(action);
    form.addEventListener("submit", async event => {
      event.preventDefault(); action.disabled = true;
      try {await fn();} catch(error) {notify(error.message, true);} finally {action.disabled = !allowed;}
    });
  }
  function controlButton(title, allowed, fn) {
    const node = button(title, "button secondary", async () => {
      node.disabled = true;
      try {await fn();} catch(error) {notify(error.message, true);} finally {node.disabled = !allowed;}
    }); node.disabled = !allowed; return node;
  }
  function summary(root, control) {
    const bar = el("div", "agent-summary");
    for (const [title, value] of [["Agents", control.agents.length], ["Shared policies", control.policies.length], ["Tracked runs", control.runs.length]]) {
      const item = el("div"); item.append(el("span", "", title), el("strong", "", String(value))); bar.append(item);
    }
    bar.append(button("Manage agents", "button secondary", () => C.setView("agents"))); root.append(bar);
  }
  function render(root, record, {request, refresh, writable}) {
    const control = record.control || {policies: [], agents: [], runs: [], local_keys: []};
    const allowed = Boolean(writable());
    if (!root.dataset.agentEvents) {
      const edited = event => {const form = event.target.closest("form"); if (form) form.dataset.editing = "true";};
      root.addEventListener("input", edited); root.addEventListener("change", edited);
      root.dataset.agentEvents = "bound";
    }
    const connections = record.connections.filter(c => c.status !== "DISCONNECTED" && c.status !== "RECORDED");
    summary(root, control);
    if (!allowed) root.append(el("p", "ops-record", "Connect your wallet or unlock your self-hosted runtime to create agents. Historical payment records are separate from your agent roster."));
    const budgets = section("Shared budgets & rules", "One engine ledger · limits, holds and actual turnover");
    if (!control.policies.length) budgets.append(el("p", "ops-empty", "Connect an API, then create a shared policy for your agents."));
    for (const entry of control.policies) {
      const card = el("article", "agent-policy"), top = el("div", "agent-card-head");
      top.append(el("strong", "", entry.policy.name), el("span", "ops-pill", entry.status));
      const stats = el("div", "agent-budget-stats");
      for (const [title, value] of [["Limit", entry.policy.turnover_limit_usdt], ["Held", entry.held_usdt], ["Used", entry.spent_usdt], ["Remaining", entry.remaining_usdt]]) {
        const item = el("div"); item.append(el("small", "", title), el("strong", "", `${value} USDT`)); stats.append(item);
      }
      card.append(top, stats, el("p", "ops-note", `Gross venue turnover · max ${entry.policy.max_order_usdt} USDT per order · ${entry.policy.max_active_runs} concurrent tasks`));
      card.append(el("p", "ops-note", entry.policy.allowed_operations.map(op => titles[op]).join(" · ")));
      if (entry.status === "ACTIVE") card.append(controlButton("Pause shared policy", allowed, async () => {await request(`/control-policies/${entry.id}/pause`, {}); await refresh();}));
      budgets.append(card);
    }
    const rules = el("details", "agent-setup"), policyForm = el("form", "ops-form"); rules.append(el("summary", "", "Create a shared policy"));
    const policyName = field(policyForm, "Policy name", "text", "TEAM");
    const policyConnections = picks(policyForm, "Connected APIs", connections.map(c => [c.id, c.name]));
    const venuePolicies = picks(policyForm, "Approved venue rules", (record.trading?.policies || []).filter(p => p.status === "ACTIVE").map(p => [p.id, p.policy.name]));
    const ops = picks(policyForm, "Allowed operations", Object.entries(titles));
    const policyGrid = el("div", "ops-form-grid"), cap = field(policyGrid, "Turnover limit · USDT", "text", "0"), maximum = field(policyGrid, "Per-order limit · USDT", "text", "0"); policyForm.append(policyGrid);
    const ttl = field(policyForm, "Policy lifetime · hours", "number", "24"), concurrency = field(policyForm, "Concurrent tasks · 1 to 8", "number", "2");
    ttl.min = "1"; ttl.max = "720"; concurrency.min = "1"; concurrency.max = "8";
    submit(policyForm, "Create shared policy", allowed && connections.length > 0, async () => {
      await request("/control-policies", {name: policyName.value.trim(), connection_ids: policyConnections(), venue_policy_ids: venuePolicies(), allowed_operations: ops(), turnover_limit_usdt: cap.value, max_order_usdt: maximum.value, max_active_runs: Number(concurrency.value), expires_at: Math.floor(Date.now()/1000) + Number(ttl.value)*3600}); await refresh();
    });
    policyForm.append(el("p", "ops-note", "USDT turnover limits are separate from USD API usage and token-specific service payments. Order preparation still requires owner approval before transmission."));
    rules.append(policyForm); budgets.append(rules); root.append(budgets);

    const roster = section("Your agents", "Bound keys · shared policies · the same connected APIs"), cards = el("div", "agent-roster");
    if (!control.agents.length) cards.append(el("p", "ops-empty", "Name an agent and assign its role, connections and shared policy."));
    for (const entry of control.agents) {
      const card = el("article", "agent-card"), top = el("div", "agent-card-head"), identity = el("div");
      identity.append(el("h3", "", entry.agent.name), el("small", "", entry.agent.role)); top.append(identity, el("span", "ops-pill", entry.status)); card.append(top);
      card.append(el("p", "ops-note", control.policies.find(p => p.id === entry.agent.policy_id)?.policy.name || entry.agent.policy_id));
      card.append(el("p", "ops-note", entry.agent.connection_ids.map(id => connections.find(c => c.id === id)?.name || id).join(" · ")));
      const actions = el("div", "ops-row-actions");
      actions.append(controlButton("Create agent key", allowed && entry.status === "ACTIVE", async () => {
        const result = C.LOCAL_ENGINE ? await request(`/agents/${entry.id}/keys`, {ttl_seconds:3600}) : await C.api("/api/keys", {name:entry.agent.name, scopes:["agents:run"], policy_id:null, ttl_seconds:3600, engine_agent_id:entry.id});
        C.showSecret(result.secret); await refresh();
      }));
      if (entry.status === "ACTIVE") actions.append(controlButton("Pause", allowed, async () => {await request(`/agents/${entry.id}/pause`, {}); await refresh();}));
      card.append(actions);
      const integration = el("details", "agent-setup"); integration.append(el("summary", "", "API integration"));
      integration.append(el("pre", "receipt-json", `POST ${C.API_PREFIX}/api/engine/agents/${entry.id}/runs\nAuthorization: Bearer <agent-key>\n\n${JSON.stringify({request_id:"unique-task-id", operation:entry.agent.operations[0], connection_id:entry.agent.connection_ids[0], payload:{}}, null, 2)}`));
      card.append(integration); cards.append(card);
    }
    roster.append(cards);
    const create = el("details", "agent-setup"), form = el("form", "ops-form"); create.append(el("summary", "", "Register an agent"));
    const name = field(form, "Agent name", "text", "WATCH"), role = field(form, "Role", "text", "Account monitor"), policy = field(form, "Shared policy", "select");
    options(policy, control.policies.filter(p => p.status === "ACTIVE").map(p => [p.id, p.policy.name]));
    const narrow = el("div"); form.append(narrow);
    let chosenConnections = () => [], chosenOps = () => [];
    const update = () => {
      narrow.replaceChildren(); const selected = control.policies.find(p => p.id === policy.value)?.policy;
      chosenConnections = picks(narrow, "Agent connections", (selected?.connection_ids || []).map(id => [id, connections.find(c => c.id === id)?.name || id]));
      chosenOps = picks(narrow, "Agent operations", (selected?.allowed_operations || []).map(op => [op, titles[op]]));
    }; policy.addEventListener("change", update); update();
    submit(form, "Register agent", allowed && Boolean(policy.value), async () => {await request("/agents", {name:name.value.trim(), role:role.value.trim(), policy_id:policy.value, connection_ids:chosenConnections(), operations:chosenOps()}); await refresh();});
    create.append(form); roster.append(create); root.append(roster);

    const tasks = section("Run an agent", "Events and API callers enter the same engine");
    const task = el("form", "ops-form"), agent = field(task, "Agent", "select"), operation = field(task, "Operation", "select"), connection = field(task, "Connected API", "select");
    options(agent, control.agents.filter(a => a.status === "ACTIVE").map(a => [a.id, `${a.agent.name} · ${a.agent.role}`]));
    const payloadFields = el("div"); task.append(payloadFields);
    let payload = () => ({});
    const updatePayload = () => {
      payloadFields.replaceChildren();
      if (operation.value === "PLAN_VENUE_ORDER") {
        const team = control.agents.find(a => a.id === agent.value)?.agent.policy_id;
        const childIds = control.policies.find(p => p.id === team)?.policy.venue_policy_ids || [];
        const child = field(payloadFields, "Venue rules", "select");
        options(child, (record.trading?.policies || []).filter(p => childIds.includes(p.id) && p.policy.connection_id === connection.value && p.status === "ACTIVE").map(p => [p.id,p.policy.name]));
        const symbol = field(payloadFields, "Symbol", "text", "BTCUSDT"), side = field(payloadFields, "Side", "select"); options(side, [["BUY","Buy"],["SELL","Sell"]]);
        const quantity = field(payloadFields, "Base quantity", "text", ""), price = field(payloadFields, "Limit price · USDT", "text", ""), tif = field(payloadFields, "Time in force", "select"); options(tif, [["IOC","Immediate or cancel"],["GTC","Good until canceled"]]);
        const reduce = field(payloadFields, "Position mode", "select"); options(reduce, [["false","Spot"],["true","Reduce-only futures"]]);
        payload = () => ({policy_id:child.value, symbol:symbol.value, side:side.value, quantity:quantity.value, price:price.value, time_in_force:tif.value, reduce_only:reduce.value === "true"});
      } else if (operation.value === "ECONOMIC_DECISION") {
        const label = el("label", "", "Economic task · typed input"), source = el("textarea");
        source.id = `agent-economic-${++sequence}`; label.htmlFor = source.id; source.rows = 8; source.spellcheck = false;
        const example = {operation: "DERIVATIVE_RISK", input: {instrument: 1, signed_quantity: 1000000, entry_price: 100000000, mark_price: 100000000, collateral: 20000000, accrued_funding: 0, unpaid_fees: 0, initial_margin_rate: 100000, maintenance_margin_rate: 50000}};
        source.value = JSON.stringify(example, null, 2);
        const guide = el("a", "ops-inline-link", "Economic API schemas"); guide.href = "#playground";
        payloadFields.append(label, source, el("p", "ops-note", "Example assumptions only. Prices, positions and margin rules must come from your verified adapters. Native calculations produce a receipt, never an order."), guide);
        payload = () => JSON.parse(source.value);
      } else if (operation.value === "NATIVE_CANDIDATE") {
        const label = el("label", "", "Typed native program and numeric state"), source = el("textarea"); source.id = `agent-native-${++sequence}`; label.htmlFor = source.id; source.rows = 8; source.spellcheck = false;
        payloadFields.append(label, source, el("p", "ops-note", "Use the native program format from Playground. This produces a candidate receipt and grants no order authority.")); payload = () => JSON.parse(source.value);
      } else payload = () => ({});
    };
    const updateTask = () => {
      const selected = control.agents.find(a => a.id === agent.value)?.agent;
      options(operation, (selected?.operations || []).map(op => [op,titles[op]])); options(connection, (selected?.connection_ids || []).map(id => [id,connections.find(c => c.id === id)?.name || id])); updatePayload();
    }; agent.addEventListener("change", updateTask); operation.addEventListener("change", updatePayload); connection.addEventListener("change", updatePayload); updateTask();
    submit(task, "Run task", allowed && Boolean(agent.value), async () => {
      const result = await request(`/agents/${agent.value}/runs`, {request_id: `console-${crypto.randomUUID()}`, operation:operation.value, connection_id:connection.value, payload:payload()});
      notify(result.error_code || `Task ${result.status.toLowerCase().replaceAll("_"," ")}.` , Boolean(result.error_code)); await refresh();
    }); tasks.append(task); root.append(tasks);
    const history = section("Execution history", "Agent → policy → connection → receipt");
    if (!control.runs.length) history.append(el("p", "ops-empty", "Your agents' tasks, holds and failure reasons appear here."));
    for (const run of control.runs) {
      const row = el("article", "agent-run"), top = el("div", "agent-card-head");
      top.append(el("strong", "", control.agents.find(a => a.id === run.agent_id)?.agent.name || run.agent_id), el("span", "ops-pill", run.status));
      row.append(top, el("p", "ops-note", `${titles[run.operation]} · ${connections.find(c => c.id === run.connection_id)?.name || run.connection_id}`), el("p", "ops-note", `Held ${run.held_usdt} USDT · Final turnover ${run.charged_usdt} USDT`));
      if (run.error_code) row.append(el("p", "form-error", run.error_code));
      const actions = el("div", "ops-row-actions");
      if (run.order_id) actions.append(controlButton("Review order", allowed, () => window.EngineConsole.openOrder(run.order_id)));
      if (["AWAITING_APPROVAL", "APPROVED", "EXPIRED_UNSENT"].includes(run.status)) actions.append(controlButton("Withdraw unsent plan", allowed, async () => {await request(`/agents/${run.agent_id}/runs/${run.id}/withdraw`, {}); await refresh();}));
      const details = el("details", "agent-setup"); details.append(el("summary", "", "Receipt"));
      details.append(el("pre", "receipt-json", JSON.stringify(run, null, 2))); row.append(actions, details); history.append(row);
    } root.append(history);
    if (C.LOCAL_ENGINE && control.local_keys.length) {
      const keys = section("Local agent keys", "Stored as hashes · shown once");
      for (const key of control.local_keys) {
        const row = el("div", "agent-card-head"); row.append(el("span", "", `${key.prefix}•••• · ${key.status}`));
        if (key.status === "ACTIVE") row.append(controlButton("Revoke", allowed, async () => {await request(`/agent-keys/${key.id}/revoke`, {}); await refresh();})); keys.append(row);
      } root.append(keys);
    }
  }
  window.AgentConsole = {render, summary};
})();
