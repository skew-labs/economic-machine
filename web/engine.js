"use strict";
const $ = (id) => document.getElementById(id);
const prefix = document.querySelector('meta[name="engine-prefix"]').content;
const recorded =
  document.querySelector('meta[name="engine-mode"]').content === "RECORDED";
let ownerToken = "",
  snapshot,
  view = "overview",
  busy = false;
const titles = {
  overview: "Overview",
  connections: "Connections",
  agents: "Agents & limits",
  activity: "Activity",
  usage: "API usage",
};
const subtitles = {
  overview: "Your accounts, agents and operations.",
  connections: "Connect once. Read from your own accounts.",
  agents: "Capital, allowed operations and approval boundaries.",
  activity: "Decisions, failures and settlement records.",
  usage: "Model requests and costs reported by your adapters.",
};
const glyphs = {
  wallet: "wallet",
  exchange: "arrows-clockwise",
  data: "stack",
  ai: "code",
};
const connectionFields = {
  "arbitrum-sepolia-wallet": [["address", "Public wallet address", "0x…"]],
  "binance-spot": [
    ["api_key_env", "API key environment name", "BINANCE_API_KEY"],
    ["api_secret_env", "Secret environment name", "BINANCE_API_SECRET"],
  ],
  "json-data": [
    ["url", "JSON endpoint", "https://…"],
    ["api_key_env", "API key environment name", "DATA_API_KEY"],
  ],
  "openai-compatible": [
    ["url", "Models endpoint", "https://…/v1/models"],
    ["api_key_env", "API key environment name", "AI_API_KEY"],
  ],
};
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function icon(name) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 256 256");
  el.classList.add("icon");
  el.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(el.namespaceURI, "use");
  use.setAttribute("href", `${prefix}/assets/ui-icons.svg#${name}`);
  el.append(use);
  return el;
}
function button(text, action, cls = "button secondary") {
  const el = node("button", text, cls);
  el.type = "button";
  el.addEventListener("click", async (event) => {
    try {
      await action(event);
    } catch (error) {
      notice(error.message, true);
    }
  });
  return el;
}
function pill(status) {
  return node(
    "span",
    status.toLowerCase().replaceAll("_", " "),
    "pill " +
      (["CONNECTED", "SETTLED", "COMPLETED", "ACTIVE", "CURRENT"].includes(
        status,
      )
        ? "ok"
        : ["ABORT", "ABORTED", "DEGRADED", "FAILED", "STALE"].includes(status)
          ? "warn"
          : ""),
  );
}
function notice(text, error = false) {
  $("notice").hidden = !text;
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}
async function request(path, body) {
  const response = await fetch(prefix + path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "omit",
    headers: {
      ...(ownerToken ? { Authorization: "Bearer " + ownerToken } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(15000),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !recorded) $("auth-dialog").showModal();
    throw new Error(data.error || "Engine request unavailable");
  }
  return data;
}
async function reload() {
  if (busy) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    snapshot = await request(
      recorded ? "/demo/engine" : "/api/engine/overview",
    );
    render();
    notice(
      recorded
        ? "Recorded Arbitrum workspace · public testnet purchase. Connect your own accounts in the self-hosted engine."
        : "",
    );
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
function table(headings, rows, emptyText) {
  const wrap = node("div", undefined, "table-scroll"),
    table = node("table"),
    thead = node("thead"),
    tr = node("tr");
  for (const h of headings) tr.append(node("th", h));
  thead.append(tr);
  table.append(thead);
  const tbody = node("tbody");
  if (!rows.length) {
    const row = node("tr"),
      td = node("td", emptyText, "empty-cell");
    td.colSpan = headings.length;
    row.append(td);
    tbody.append(row);
  }
  for (const cells of rows) {
    const row = node("tr");
    for (const value of cells) {
      const cell = node("td");
      cell.append(
        value instanceof Node ? value : document.createTextNode(String(value)),
      );
      row.append(cell);
    }
    tbody.append(row);
  }
  table.append(tbody);
  wrap.append(table);
  return wrap;
}
function panel(title, content, action) {
  const el = node("section", undefined, "panel"),
    head = node("div", undefined, "panel-heading");
  head.append(node("h2", title));
  if (action) head.append(action);
  el.append(head, content);
  return el;
}
function empty(title, copy, action) {
  const el = node("div", undefined, "empty-state");
  el.append(icon("stack"), node("h3", title), node("p", copy));
  if (action) el.append(action);
  return el;
}
function connectionLabel(item) {
  const label = node("div", undefined, "connection-label"),
    mark = node("span", undefined, "connection-mark");
  mark.append(icon(glyphs[item.kind] || "stack"));
  const text = node("div");
  text.append(node("strong", item.name), node("small", item.network));
  label.append(mark, text);
  return label;
}
function assetRows() {
  return snapshot.assets.map((a) => [
    node("strong", a.symbol),
    a.quantity,
    a.connection,
    a.network,
    pill(recorded ? "RECORDED" : a.stale ? "STALE" : "CURRENT"),
  ]);
}
function showView(next) {
  view = next;
  for (const nav of document.querySelectorAll("[data-view]")) {
    nav.classList.toggle("active", nav.dataset.view === view);
    nav.setAttribute(
      "aria-current",
      nav.dataset.view === view ? "page" : "false",
    );
  }
  render();
}
function render() {
  if (!snapshot) return;
  $("breadcrumb").textContent = titles[view];
  $("page-title").textContent = titles[view];
  $("page-subtitle").textContent = subtitles[view];
  $("workspace-mode").textContent = recorded
    ? "Recorded public purchase"
    : "User-owned runtime";
  $("environment").textContent = recorded ? "Arbitrum Sepolia" : "Self hosted";
  $("connection-count").textContent = snapshot.connections.filter(
    (c) => c.status !== "DISCONNECTED",
  ).length;
  $("observed-at").textContent =
    `${recorded ? "Record checked" : "Workspace refreshed"} ${new Date(snapshot.as_of * 1000).toLocaleString("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} UTC`;
  const content = $("content");
  content.replaceChildren();
  ({
    overview: renderOverview,
    connections: renderConnections,
    agents: renderAgents,
    activity: renderActivity,
    usage: renderUsage,
  })[view](content);
}
function renderOverview(content) {
  const stats = node("div", undefined, "stat-row");
  for (const [label, value, note] of [
    [
      "Connections",
      snapshot.connections.filter((c) => c.status !== "DISCONNECTED").length,
      recorded ? "1 recorded wallet" : "User-owned credentials",
    ],
    [
      "Active agents",
      snapshot.programs.filter((p) => p.status === "ACTIVE").length,
      `${snapshot.programs.length} policies registered`,
    ],
    ["Open orders", snapshot.orders.length, "No synthetic orders"],
    [
      "Payment records",
      snapshot.payments.length,
      recorded ? "0.01 test USDC settled" : "Venue orders bypass x402",
    ],
  ]) {
    const stat = node("div", undefined, "stat");
    stat.append(
      node("span", label),
      node("strong", String(value)),
      node("small", note),
    );
    stats.append(stat);
  }
  content.append(stats);
  const grid = node("div", undefined, "overview-grid");
  grid.append(
    panel(
      "Balances",
      table(
        ["Asset", "Quantity", "Account", "Network", "Observation"],
        assetRows(),
        "Connect an account to read its balances.",
      ),
    ),
  );
  const agent = node("div", undefined, "agent-summary");
  if (snapshot.programs.length) {
    for (const p of snapshot.programs.slice(0, 4)) {
      const row = node("div", undefined, "agent-summary-row");
      row.append(
        node("strong", p.name),
        pill(p.status),
        node("small", `${p.sandbox.capital_limit} ${p.sandbox.asset} budget`),
      );
      agent.append(row);
    }
  } else
    agent.append(
      empty(
        "Your first agent",
        "Define a budget, allowed venues and execution conditions.",
      ),
    );
  grid.append(
    panel(
      "Agent capital",
      agent,
      button("View all", () => showView("agents"), "text-button"),
    ),
  );
  content.append(grid);
  content.append(panel("Recent activity", activityTable()));
  const note = node("div", undefined, "boundary-note");
  note.append(
    icon("shield-check"),
    node(
      "span",
      "Balances retain their native units. Assets are not combined into a dollar total without price evidence.",
    ),
  );
  content.append(note);
}
function renderConnections(content) {
  const bar = node("div", undefined, "section-note");
  bar.append(
    node("span", "Wallet · Exchange · Data · AI"),
    node("span", recorded ? "Recorded view" : "Read-only adapters"),
  );
  content.append(bar);
  const grid = node("div", undefined, "connection-grid");
  for (const c of snapshot.connections) {
    const card = node("article", undefined, "connection-card"),
      top = node("div", undefined, "connection-top");
    top.append(connectionLabel(c), pill(c.status));
    card.append(top);
    const detail = node("dl");
    for (const [label, value] of [
      [
        "Permissions",
        c.operations.join(" · ").replaceAll("_", " ").toLowerCase(),
      ],
      [
        "Keys",
        c.secret_storage === "USER_ENVIRONMENT_ONLY"
          ? "Your process environment"
          : "External disposable buyer",
      ],
      [
        recorded ? "Balance record" : "Last read",
        c.observed_at
          ? new Date(c.observed_at * 1000).toISOString()
          : recorded ? `Receipt block ${c.snapshot.block_number}` : "Not read",
      ],
      [
        "Status",
        c.error_code ||
          (c.stale ? "Historical or stale observation" : "Current observation"),
      ],
    ]) {
      const row = node("div");
      row.append(node("dt", label), node("dd", value));
      detail.append(row);
    }
    card.append(detail);
    if (!recorded && c.status !== "DISCONNECTED") {
      const actions = node("div", undefined, "card-actions");
      actions.append(
        button("Refresh", async () => {
          await act(`/api/engine/connections/${c.id}/sync`, {});
        }),
        button(
          "Disconnect",
          async () => {
            await act(`/api/engine/connections/${c.id}/disconnect`, {});
          },
          "text-button",
        ),
      );
      card.append(actions);
    }
    grid.append(card);
  }
  if (!snapshot.connections.length)
    grid.append(
      empty(
        "Connect your accounts",
        "Balances, open orders and API metadata remain in your runtime.",
        button("Connect account", openConnection),
      ),
    );
  content.append(grid);
  if (recorded)
    content.append(
      panel(
        "Run it in your environment",
        empty(
          "Bring your own connections",
          "Clone the engine, configure environment references, then connect your wallet, exchange or API.",
          sourceLink(),
        ),
      ),
    );
}
function sourceLink() {
  const a = node("a", "Open source repository ↗", "button secondary");
  a.href = "https://github.com/skew-labs/economic-machine";
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}
function renderAgents(content) {
  const actions = node("div", undefined, "section-actions");
  actions.append(node("span", `${snapshot.programs.length} agent policies`));
  if (!recorded)
    actions.append(
      button(
        "Import policy",
        () => {
          $("program-source").value = "";
          $("program-result").textContent = "";
          $("program-dialog").showModal();
        },
        "button primary",
      ),
    );
  content.append(actions);
  for (const p of snapshot.programs) {
    const card = node("article", undefined, "policy-card"),
      head = node("div", undefined, "policy-card-heading");
    head.append(node("h2", p.name), pill(p.status));
    card.append(head);
    const limits = node("div", undefined, "limit-grid");
    for (const [label, value] of [
      ["Capital limit", `${p.sandbox.capital_limit} ${p.sandbox.asset}`],
      [
        "Exposure limit",
        `${p.sandbox.max_exposure === undefined ? "Not in purchase mandate" : p.sandbox.max_exposure + " " + p.sandbox.asset}`,
      ],
      [
        "Cost limit",
        `${p.sandbox.max_total_cost === undefined ? "Not in purchase mandate" : p.sandbox.max_total_cost + " " + p.sandbox.asset}`,
      ],
      ["Allowed venues", p.sandbox.allowed_protocols.join(",")],
    ]) {
      const item = node("div");
      item.append(node("span", label), node("strong", value));
      limits.append(item);
    }
    card.append(
      limits,
      node(
        "p",
        p.approval.replaceAll("_", " ").toLowerCase(),
        "policy-approval",
      ),
    );
    const details = node("details"),
      summary = node("summary", "Typed policy");
    details.append(summary, node("pre", JSON.stringify(p.source, null, 2)));
    card.append(details);
    if (!recorded) {
      const controls = node("div", undefined, "card-actions");
      controls.append(
        button("Evaluate", async () =>
          act(`/api/engine/programs/${p.id}/evaluate`, {
            at: new Date().toISOString(),
          }),
        ),
        button(p.status === "PAUSED" ? "Resume" : "Pause", async () =>
          act(
            `/api/engine/programs/${p.id}/${p.status === "PAUSED" ? "resume" : "pause"}`,
            { reason: "OWNER_CONSOLE_REQUEST" },
          ),
        ),
      );
      card.append(controls);
    }
    content.append(card);
  }
  if (!snapshot.programs.length)
    content.append(
      empty(
        "Register a bounded policy",
        "Each agent receives a capital limit, an allowed venue list and explicit risk limits.",
      ),
    );
  if (recorded) content.append(policyReplay());
}
function policyReplay() {
  const form = node("form", undefined, "policy-replay"),
    title = node("h2", "Check buying conditions"),
    copy = node("p", "Replay the original purchase decision. No new payment."),
    fields = node("div", undefined, "replay-fields");
  const definitions = [
    ["Budget · test USDC", "replay-budget", "0.01"],
    ["Maximum data age · seconds", "replay-age", "86400"],
  ];
  for (const [label, id, value] of definitions) {
    const field = node("label", label),
      input = node("input");
    input.id = id;
    input.value = value;
    input.required = true;
    field.append(input);
    fields.append(field);
  }
  const label = node("label", "Usage right"),
    select = node("select");
  select.id = "replay-license";
  for (const [value, text] of [
    ["internal-use", "Internal research"],
    ["commercial-use", "Commercial use"],
  ]) {
    const option = node("option", text);
    option.value = value;
    select.append(option);
  }
  label.append(select);
  fields.append(label);
  const submit = node("button", "Check policy", "button primary");
  submit.type = "submit";
  const result = node(
    "p",
    "Conditions are evaluated at the original purchase time.",
    "replay-result",
  );
  result.setAttribute("role", "status");
  form.append(title, copy, fields, submit, result);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const data = await request("/demo/trade/replay", {
        budget: $("replay-budget").value,
        max_age_seconds: Number($("replay-age").value),
        license: $("replay-license").value,
      });
      result.textContent =
        data.result.status === "AGREED"
          ? "Policy accepts the 0.01 test-USDC offer. No new payment."
          : data.result.reason_codes
              .join(" · ")
              .replaceAll("_", " ")
              .toLowerCase() + ". No payment authorized.";
    } catch (error) {
      result.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}
function activityTable() {
  return table(
    ["Operation", "State", "Verification", "Record"],
    snapshot.runs.map((r) => [
      node("span", r.program_id, "mono-short"),
      pill(r.status),
      r.verification ? "Replay verified" : "Verification unavailable",
      button("Inspect", () => openReceipt(r), "text-button"),
    ]),
    "No execution records. Evaluations will appear here.",
  );
}
function renderActivity(content) {
  content.append(panel("Execution history", activityTable()));
  content.append(
    panel(
      "Open orders",
      table(
        ["ID", "Symbol", "Side", "Quantity", "Filled", "State"],
        snapshot.orders.map((o) => [
          o.id,
          o.symbol,
          o.side,
          o.quantity,
          o.filled,
          pill(o.status),
        ]),
        "No open exchange orders have been imported.",
      ),
    ),
  );
  content.append(
    panel(
      "Positions",
      table(
        ["Instrument", "Quantity", "Account"],
        snapshot.positions.map((p) => [p.symbol, p.quantity, p.connection]),
        "No derivative positions have been imported.",
      ),
    ),
  );
}
function renderUsage(content) {
  content.append(
    panel(
      "API usage",
      table(
        [
          "Model",
          "Calls",
          "Input tokens",
          "Output tokens",
          "Reported cost · USD",
        ],
        snapshot.usage.map((u) => [
          u.model,
          u.calls,
          u.input_tokens,
          u.output_tokens,
          (u.cost_microusd / 1e6).toFixed(6),
        ]),
        "No usage has been reported by a local adapter.",
      ),
    ),
  );
  content.append(
    node(
      "p",
      "Usage comes from your adapter ledger. Listing model availability does not run inference or establish a provider invoice.",
      "boundary-note",
    ),
  );
}
async function act(path, data) {
  try {
    const result = await request(path, data);
    await reload();
    notice(
      result.error_code ||
        "Engine state updated. No financial transaction was submitted.",
      Boolean(result.error_code),
    );
    return result;
  } catch (error) {
    notice(error.message, true);
    throw error;
  }
}
function openReceipt(run) {
  $("receipt-json").textContent = JSON.stringify(run.receipt, null, 2);
  $("receipt-assurance").textContent = recorded
    ? "Recorded finalized x402 purchase. Joined authorization, receipt and delivery evidence."
    : "Deterministic evaluation receipt. External authorization and execution evidence remain separate.";
  const hash = run.receipt.tx_hash;
  $("receipt-chain").hidden = !hash;
  if (hash) $("receipt-chain").href = "https://sepolia.arbiscan.io/tx/" + hash;
  $("receipt-dialog").showModal();
}
function openConnection() {
  if (recorded) {
    showView("connections");
    notice(
      "Connect your own accounts in the self-hosted engine. This public workspace exposes a recorded purchase.",
    );
    return;
  }
  $("connection-error").textContent = "";
  connectionForm();
  $("connection-dialog").showModal();
}
function connectionForm() {
  const fields = $("connection-fields");
  fields.replaceChildren();
  for (const [id, label, placeholder] of connectionFields[
    $("connection-profile").value
  ]) {
    const wrap = node("label", label),
      input = node("input");
    input.id = "config-" + id;
    input.placeholder = placeholder;
    input.required = true;
    input.autocomplete = "off";
    wrap.htmlFor = input.id;
    wrap.append(input);
    fields.append(wrap);
  }
}
for (const nav of document.querySelectorAll("[data-view]"))
  nav.addEventListener("click", () => showView(nav.dataset.view));
for (const close of document.querySelectorAll(".close-dialog"))
  close.addEventListener("click", () => close.closest("dialog").close());
$("connect-button").addEventListener("click", openConnection);
$("refresh").addEventListener("click", reload);
$("connection-profile").addEventListener("change", connectionForm);
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  ownerToken = $("owner-token").value;
  $("owner-token").value = "";
  try {
    snapshot = await request("/api/engine/overview");
    $("auth-dialog").close();
    render();
    notice("Your local engine is connected.");
  } catch (error) {
    ownerToken = "";
    $("auth-error").textContent = error.message;
  }
});
$("connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const config = {};
  for (const [key] of connectionFields[$("connection-profile").value])
    config[key] = $("config-" + key).value;
  try {
    await act("/api/engine/connections", {
      name: $("connection-name").value,
      profile: $("connection-profile").value,
      config,
    });
    $("connection-dialog").close();
    showView("connections");
  } catch (error) {
    $("connection-error").textContent = error.message;
  }
});
$("compile-program").addEventListener("click", async () => {
  try {
    const result = await request(
      "/api/engine/programs/compile",
      JSON.parse($("program-source").value),
    );
    $("program-result").textContent =
      "Policy valid · " +
      result.program.program_hash +
      " · no execution authority";
  } catch (error) {
    $("program-result").textContent = error.message;
  }
});
$("program-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await act("/api/engine/programs", JSON.parse($("program-source").value));
    $("program-dialog").close();
    showView("agents");
  } catch (error) {
    $("program-result").textContent = error.message;
  }
});
reload();
