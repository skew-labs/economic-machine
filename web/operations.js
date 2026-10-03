"use strict";

(() => {
  const C = window.MachineConsole;
  const { el, button, notify, API_PREFIX } = C;
  const byId = (id) => document.getElementById(id);
  const views = new Set([
    "overview",
    "connections",
    "agents",
    "execution",
    "playground",
    "usage",
  ]);
  let record = null,
    profiles = {},
    currentPlan = null,
    refreshing = false;
  const stamp = (value) =>
    value
      ? new Date(
          typeof value === "number" ? value * 1000 : value,
        ).toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "Not observed";

  async function request(path, body) {
    if (C.LOCAL_ENGINE) return C.api("/api/engine" + path, body);
    if (C.PREVIEW && body !== undefined)
      throw new Error("Recorded evidence is read-only.");
    const response = await fetch(API_PREFIX + "/api/engine" + path, {
      credentials: "same-origin",
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok)
      throw new Error(
        response.status === 401
          ? "Connect your wallet to manage this runtime."
          : value.error || value.detail || "Engine request rejected.",
      );
    return value;
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      if (C.PREVIEW) {
        const response = await fetch(API_PREFIX + "/demo/engine", {
          credentials: "omit",
        });
        if (!response.ok) throw new Error("Recorded evidence is unavailable.");
        record = await response.json();
      } else {
        try {
          record = await request("/overview");
          profiles = (await request("/profiles")).profiles;
        } catch (error) {
          if (
            !C.LOCAL_ENGINE &&
            error.message.startsWith("Connect your wallet")
          ) {
            const response = await fetch(API_PREFIX + "/demo/engine", {
              credentials: "omit",
            });
            if (!response.ok) throw error;
            record = await response.json();
          } else throw error;
        }
      }
      currentPlan = record.read_only
        ? null
        : record.trading?.orders.find(
            (order) => order.id === currentPlan?.id,
          ) || null;
      C.engineStatus?.(record);
      render(C.state.view);
    } catch (error) {
      notify(error.message, true);
    } finally {
      refreshing = false;
    }
  }
  const writable = () => record && !record.read_only;
  function action(text, fn, className = "button secondary") {
    const node = button(text, className, async () => {
      node.disabled = true;
      try {
        await fn();
      } catch (error) {
        notify(error.message, true);
      } finally {
        node.disabled = !writable();
      }
    });
    node.disabled = !writable();
    return node;
  }
  function table(title, labels, rows, emptyMessage, note = "") {
    const section = el("section", "ops-section"),
      head = el("div", "ops-section-head");
    head.append(el("h2", "", title), el("span", "", note));
    section.append(head);
    if (!rows.length) {
      section.append(el("div", "ops-empty", emptyMessage));
      return section;
    }
    const wrap = el("div", "ops-table-wrap"),
      grid = el("table", "ops-table"),
      thead = el("thead"),
      tr = el("tr");
    for (const text of labels) tr.append(el("th", "", text));
    thead.append(tr);
    grid.append(thead);
    const tbody = el("tbody");
    for (const values of rows) {
      const row = el("tr");
      for (const value of values) {
        const cell = el("td", typeof value === "object" ? "" : "amount");
        value instanceof Node
          ? cell.append(value)
          : (cell.textContent = String(value ?? "—"));
        row.append(cell);
      }
      tbody.append(row);
    }
    grid.append(tbody);
    wrap.append(grid);
    section.append(wrap);
    return section;
  }
  function identity(name, detail) {
    const node = el("div", "", name);
    node.append(el("small", "", detail));
    return node;
  }
  function status(value, stale = false) {
    return el(
      "span",
      "ops-pill" +
        (stale
          ? " stale"
          : value === "CONNECTED" || value === "SETTLED"
            ? " good"
            : ""),
      value,
    );
  }
  function banner() {
    const bar = el("div", "ops-record");
    bar.append(
      el(
        "span",
        "",
        `Historical Sepolia evidence · ${stamp(record.as_of)} · not your account`,
      ),
    );
    const link = el("a", "", "View receipt ↗");
    link.href = API_PREFIX + "/submission";
    bar.append(link);
    return bar;
  }
  function startGuide(root) {
    const live = writable();
    const control = record?.control || {policies: [], agents: [], runs: []};
    const connected = live && record.connections.some(c => c.status === "CONNECTED");
    const policy = live && control.policies.some(p => p.status === "ACTIVE");
    const agent = live && control.agents.some(a => a.status === "ACTIVE");
    const section = el("section", "setup-guide");
    section.setAttribute("aria-label", "Workspace setup");
    const intro = el("div", "setup-intro");
    intro.append(el("span", "setup-eyebrow", "YOUR WORKSPACE"),
      el("h2", "", agent ? "Your agents. Under control." : "Set up once. Stay in control."),
      el("p", "", "Connect an account, set its limits, then give your agents a job."));
    const access = el("div", "setup-access");
    if (!live) {
      access.append(el("span", "setup-status", C.PREVIEW ? "Read-only preview" : C.LOCAL_ENGINE ? "Runtime locked" : "Not signed in"));
      const signIn = button(C.PREVIEW ? "Open your workspace" : C.LOCAL_ENGINE ? "Unlock your runtime" : "Connect wallet", "button primary", () => {
        if (C.PREVIEW) location.assign(API_PREFIX + "/console#overview");
        else if (C.LOCAL_ENGINE) byId("local-owner-token").focus();
        else byId("wallet-account").click();
      });
      access.append(signIn);
    } else access.append(el("span", "setup-status ready", "Workspace connected"));
    const head = el("div", "setup-head"); head.append(intro, access); section.append(head);
    const steps = el("ol", "setup-steps");
    const entries = [
      ["Connect an account", "Exchange, wallet, data or AI API.", "connections", connected],
      ["Set shared limits", "Choose a budget and allowed actions.", "agents", policy],
      ["Add your agents", "Assign a name, job and policy.", "agents", agent],
      ["Review every run", "See outcomes, failures and receipts.", "agents", live && control.runs.length > 0],
    ];
    entries.forEach(([title, detail, view, done], index) => {
      const item = el("li", done ? "complete" : "");
      const link = button("", "setup-step", () => {
        C.setView(view);
        if (index > 0) {
          const target = document.querySelector(["", "#agent-policy-setup", "#agent-register-setup", "#agent-run-history"][index]);
          if (target) {
            if (target.tagName === "DETAILS") target.open = true;
            target.scrollIntoView({block: "center", behavior: "smooth"});
          }
        }
      });
      link.append(el("span", "step-number", done ? "✓" : String(index + 1)), el("strong", "", title), el("span", "step-detail", detail));
      item.append(link); steps.append(item);
    });
    section.append(steps);
    const foot = el("div", "setup-foot");
    foot.append(el("span", "", "Keys stay in your environment. Orders require the approval set in your rules."));
    foot.append(button("How it works", "quiet setup-help", () => byId("guide-dialog").showModal()));
    section.append(foot); root.append(section);
  }
  function overview() {
    const root = byId("ops-overview");
    root.replaceChildren();
    startGuide(root);
    if (!record) {
      root.append(
        el("p", "ops-empty", "Connect your runtime to read accounts."),
      );
      return;
    }
    if (record.read_only) {
      const history = el("details", "recorded-details");
      history.append(el("summary", "", "View recorded payment evidence"), banner(),
        table("Historical balances", ["Asset / account", "Balance", "Source"], record.assets.map(a => [identity(a.symbol, a.connection), a.quantity, "Historical · not a live balance"]), "No recorded balances."));
      root.append(history);
      const next = el("div", "workspace-empty");
      next.append(C.uiIcon("plugs-connected"), el("h3", "", "Your accounts will appear here."), el("p", "", "Connect your wallet to sign in, then add the APIs you want your agents to use."),
        button("Explore connections", "button secondary", () => C.setView("connections")));
      root.append(next);
      return;
    }
    window.AgentConsole?.summary(root, record.control || {agents: [], policies: [], runs: []});
    const metrics = el("div", "ops-summary");
    for (const [label, value] of [
      [
        "Connected sources",
        record.connections.filter((c) => c.status === "CONNECTED").length,
      ],
      ["Derivative positions", record.positions.length],
      ["Open venue orders", record.orders.length],
    ]) {
      const cell = el("div");
      cell.append(el("span", "", label), el("strong", "", String(value)));
      metrics.append(cell);
    }
    root.append(
      metrics,
      table(
        "Assets",
        ["Asset / account", "Balance", "Available", "Observed"],
        record.assets.map((a) => [
          identity(a.symbol, a.connection),
          a.quantity,
          a.available ?? "—",
          identity(
            stamp(a.observed_at),
            a.stale ? "Stale / historical" : "API observation",
          ),
        ]),
        "No balances imported. Connect an account to begin.",
        "Amounts stay in their native units",
      ),
      table(
        "Positions",
        [
          "Instrument / account",
          "Size",
          "Entry / mark",
          "Unrealized P&L",
          "Liquidation",
        ],
        record.positions.map((p) => [
          identity(p.symbol, p.connection),
          identity(p.quantity, p.side),
          identity(p.entry_price, p.mark_price),
          identity(p.unrealized_pnl, p.margin_asset),
          p.liquidation_price,
        ]),
        "No derivative positions imported.",
        "Sequential venue reads",
      ),
      table(
        "Open orders",
        [
          "Instrument / account",
          "Side",
          "Quantity / filled",
          "Limit",
          "Status",
        ],
        record.orders.map((o) => [
          identity(o.symbol, o.connection),
          o.side,
          identity(o.quantity, o.filled),
          o.price,
          status(o.status, o.stale),
        ]),
        "No open venue orders imported.",
      ),
    );
    if (record.runs.length)
      root.append(
        table(
          "Recent runtime receipts",
          ["Program", "Outcome", "Verification"],
          record.runs
            .slice(0, 5)
            .map((r) => [
              r.program_id,
              status(r.status),
              r.verification ? "Verified replay" : "Unverified",
            ]),
          "",
        ),
      );
  }
  function labeled(form, text, name, type = "text", value = "") {
    const label = el("label", "", text);
    const input = el("input");
    input.name = name;
    input.id = "ops-field-" + name;
    input.type = type;
    input.value = value;
    label.htmlFor = input.id;
    input.autocomplete = "off";
    form.append(label, input);
    return input;
  }
  function select(form, text, name, choices) {
    const label = el("label", "", text),
      input = el("select");
    input.name = name;
    input.id = "ops-field-" + name;
    label.htmlFor = input.id;
    for (const [value, title] of choices) {
      const option = el("option", "", title);
      option.value = value;
      input.append(option);
    }
    form.append(label, input);
    return input;
  }
  function connections() {
    const root = byId("ops-connections");
    root.replaceChildren();
    if (!record) return;
    if (record.read_only) {
      const intro = el("section", "connect-intro");
      intro.append(el("h2", "", "Bring the accounts you already use."), el("p", "", "Sign in to your workspace to configure a connection. Keep API keys in your server environment."));
      const choices = el("div", "connection-choices");
      for (const [icon, title, detail] of [["chart-line-up", "Exchange", "Binance Spot & USD-M"], ["wallet", "Wallet", "Arbitrum account reads"], ["database", "Data", "Your HTTP data source"], ["code", "AI API", "OpenAI-compatible model catalog"]]) {
        const card = el("div", "connection-choice"); card.append(C.uiIcon(icon), el("h3", "", title), el("p", "", detail)); choices.append(card);
      }
      intro.append(choices);
      const signIn = button(C.PREVIEW ? "Open your workspace" : "Connect wallet", "button primary", () => C.PREVIEW ? location.assign(API_PREFIX + "/console#connections") : byId("wallet-account").click());
      intro.append(signIn, el("p", "ops-note", "Wallet login identifies your workspace. It does not authorize a trade or payment."));
      root.append(intro);
      return;
    }
    const split = el("div", "ops-split");
    split.append(
      table(
        "Account connections",
        ["Source", "State", "Last read", "Synchronization"],
        record.connections.map((c) => {
          const actions = el("div", "ops-row-actions");
          actions.append(
            action("Sync", async () => {
              await request(`/connections/${c.id}/sync`, {});
              await refresh();
            }),
          );
          const schedule = el("select");
          schedule.setAttribute("aria-label", `${c.name} sync interval`);
          const job = record.sync_jobs?.find((j) => j.connection_id === c.id);
          for (const [value, label] of [
            ["0", "Manual"],
            ["15", "Every 15s"],
            ["30", "Every 30s"],
            ["60", "Every 1m"],
            ["300", "Every 5m"],
          ]) {
            const option = el("option", "", label);
            option.value = value;
            schedule.append(option);
          }
          schedule.value = job?.enabled ? String(job.interval_seconds) : "0";
          schedule.disabled = !writable() || c.status === "DISCONNECTED";
          schedule.addEventListener("change", async () => {
            schedule.disabled = true;
            try {
              await request(`/connections/${c.id}/schedule`, {
                enabled: schedule.value !== "0",
                interval_seconds: Number(schedule.value) || 60,
              });
              await refresh();
            } catch (e) {
              notify(e.message, true);
              await refresh();
            }
          });
          actions.append(
            schedule,
            action("Disconnect", async () => {
              await request(`/connections/${c.id}/disconnect`, {});
              await refresh();
            }),
          );
          return [
            identity(c.name, c.network),
            status(c.status, c.stale),
            identity(
              stamp(c.observed_at),
              c.error_code ||
                (job?.failures ? `Backoff · ${job.failures} failures` : ""),
            ),
            actions,
          ];
        }),
        "No account connections yet.",
      ),
    );
    const panel = el("section", "ops-section"),
      head = el("div", "ops-section-head");
    head.append(el("h2", "", "Add a connection"));
    panel.append(head);
    const form = el("form", "ops-form");
    const profile = select(
      form,
      "Connector",
      "profile",
      Object.entries(profiles).map(([key, p]) => [key, p.name]),
    );
    const name = labeled(form, "Account name", "connection-name");
    name.maxLength = 60;
    name.required = true;
    const fields = el("div");
    form.append(fields);
    function updateFields() {
      fields.replaceChildren();
      const p = profiles[profile.value];
      if (!p) return;
      for (const key of [...p.fields, ...p.credentials]) {
        const input = labeled(fields, key.replaceAll("_", " "), key);
        input.required = true;
        if (p.credentials.includes(key))
          input.placeholder =
            (record.credential_namespace || "") + key.toUpperCase();
        if (key === "url")
          input.placeholder =
            profile.value === "openai-compatible"
              ? "https://api.example.com/v1/models"
              : "https://data.example.com/resource";
      }
    }
    profile.addEventListener("change", updateFields);
    updateFields();
    const submit = el("button", "button primary", "Add connection");
    submit.type = "submit";
    submit.disabled = !writable();
    form.append(
      submit,
      el(
        "p",
        "ops-note",
        record.credential_namespace
          ? `Server environment references must start with ${record.credential_namespace}. Values are never accepted here.`
          : "Use environment variable names. Keep keys in your own process environment.",
      ),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const config = {};
        for (const input of fields.querySelectorAll("input"))
          config[input.name] = input.value;
        await request("/connections", {
          name: name.value,
          profile: profile.value,
          config,
        });
        await refresh();
        notify("Connection configured. Choose Sync to read the account.");
      } catch (e) {
        notify(e.message, true);
        submit.disabled = !writable();
      }
    });
    panel.append(form);
    split.append(panel);
    root.append(split);
  }
  function execution() {
    const root = byId("ops-execution");
    root.replaceChildren();
    if (!record) return;
    if (!writable()) {
      const intro = el("section", "connect-intro");
      intro.append(el("h2", "", "Review the plan before an order is sent."), el("p", "", "Connect an exchange and create venue rules. The engine prepares the price, quantity and limits for your approval."),
        el("p", "ops-record", "Live exchange transmission is disabled on this hosted runtime."),
        button("Connect an exchange", "button primary", () => C.setView("connections")));
      root.append(intro); return;
    }
    const trading = record.trading || {
      policies: [],
      orders: [],
      live_transmission_enabled: false,
    };
    root.append(
      el(
        "p",
        "ops-note",
        trading.live_transmission_enabled
          ? "Live transmission enabled. Each order requires your exact-plan approval."
          : "Live transmission is disabled on this runtime. Plans can be compiled; no order is sent.",
      ),
    );
    const workbench = el("div", "ops-workbench"),
      left = el("div", "ops-editor"),
      right = el("div", "ops-result");
    left.append(el("div", "ops-panel-label", "ORDER COMPILER"));
    right.append(el("div", "ops-panel-label", "PLAN / RECEIPT"));
    const form = el("form", "ops-form");
    const policy = select(
      form,
      "Turnover policy",
      "policy-id",
      trading.policies
        .filter((p) => p.status === "ACTIVE")
        .map((p) => [p.id, p.policy.name]),
    );
    labeled(form, "Venue symbol", "symbol", "text", "BTCUSDT");
    select(form, "Side", "side", [
      ["BUY", "Buy"],
      ["SELL", "Sell"],
    ]);
    const grid = el("div", "ops-form-grid");
    labeled(grid, "Base quantity", "quantity", "text", "0.001");
    labeled(grid, "Limit price · USDT", "price", "text", "");
    form.append(grid);
    select(form, "Time in force", "time-in-force", [
      ["IOC", "Immediate or cancel"],
      ["GTC", "Good until canceled"],
    ]);
    select(form, "Position mode", "reduce-only", [
      ["false", "Spot"],
      ["true", "Reduce-only futures"],
    ]);
    const submit = el("button", "button primary", "Compile order");
    submit.type = "submit";
    submit.disabled = !writable() || !trading.policies.length;
    if (submit.disabled) form.append(el("p", "ops-record", !writable() ? "Sign in and connect an exchange before preparing an order." : "Create venue rules below before preparing an order."));
    form.append(submit);
    left.append(form);
    const output = el(
      "pre",
      "",
      currentPlan
        ? JSON.stringify(currentPlan, null, 2)
        : "Select a policy and compile an order.\n\nThe plan binds instrument rules, price, quantity, policy, expiry and client order ID.",
    );
    const run = record.control?.runs.find(item => item.order_id === currentPlan?.id);
    if (run) {
      const agent = record.control.agents.find(item => item.id === run.agent_id);
      const team = record.control.policies.find(item => item.id === run.policy_id);
      right.append(el("p", "ops-record", `${agent?.agent.name || run.agent_id} · ${team?.policy.name || run.policy_id} · held ${run.held_usdt} USDT`));
    }
    right.append(output);
    const actions = el("div", "ops-actions");
    if (currentPlan?.status === "AWAITING_APPROVAL")
      actions.append(
        action(
          "Approve exact plan",
          async () => {
            currentPlan = await request(
              `/trade/orders/${currentPlan.id}/approve`,
              { plan_hash: currentPlan.plan_hash },
            );
            await refresh();
          },
          "button primary",
        ),
      );
    if (currentPlan?.status === "APPROVED" && trading.live_transmission_enabled)
      actions.append(
        action(
          "Transmit approved order",
          async () => {
            currentPlan = await request(
              `/trade/orders/${currentPlan.id}/dispatch`,
              {},
            );
            await refresh();
          },
          "button primary",
        ),
      );
    right.append(actions);
    workbench.append(left, right);
    root.append(workbench);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const data = new FormData(form);
        currentPlan = await request("/trade/orders", {
          request_id: "console-" + crypto.randomUUID(),
          policy_id: policy.value,
          symbol: data.get("symbol"),
          side: data.get("side"),
          quantity: data.get("quantity"),
          price: data.get("price"),
          time_in_force: data.get("time-in-force"),
          reduce_only: data.get("reduce-only") === "true",
        });
        await refresh();
      } catch (e) {
        notify(e.message, true);
        submit.disabled = false;
      }
    });
    root.append(
      table(
        "Venue order history",
        [
          "Order",
          "Instrument",
          "State",
          "Filled / quote",
          "Reserved · USDT",
          "Recovery",
        ],
        trading.orders.map((o) => {
          const controls = el("div", "ops-row-actions");
          if (["FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"].includes(o.status)) controls.append(
            el("small", "muted", `Account: ${o.account_readback?.status || 'NOT_OBSERVED'}`),
            action("Refresh account", async () => { await request(`/trade/orders/${o.id}/readback`, {}); await refresh(); })
          );
          controls.append(
            action("Reconcile", async () => {
              await request(`/trade/orders/${o.id}/reconcile`, {});
              await refresh();
            }),
          );
          if (
            ["NEW", "PARTIALLY_FILLED", "UNKNOWN"].includes(o.status) &&
            trading.live_transmission_enabled
          )
            controls.append(
              action("Cancel", async () => {
                await request(`/trade/orders/${o.id}/cancel`, {});
                await refresh();
              }),
            );
          return [
            identity(o.id.slice(-12), o.venue_order_id || "Not accepted yet"),
            identity(o.plan.symbol, o.plan.side),
            status(o.status),
            identity(o.filled_quantity, o.filled_quote_usdt + " USDT"),
            o.reserved_usdt,
            controls,
          ];
        }),
        "No venue orders.",
        "Commission is not yet reconciled",
      ),
    );
    const policyPanel = el("section", "ops-section");
    const heading = el("div", "ops-section-head");
    heading.append(el("h2", "", "Create turnover policy"));
    policyPanel.append(heading);
    const policyForm = el("form", "ops-form");
    labeled(policyForm, "Policy name", "policy-name", "text", "SPOT_BUYER");
    select(
      policyForm,
      "Venue account",
      "connection-id",
      record.connections
        .filter(
          (c) =>
            ["binance-spot", "binance-usdm", "binance-spot-testnet", "binance-usdm-testnet"].includes(c.profile) &&
            c.status !== "DISCONNECTED",
        )
        .map((c) => [c.id, c.name]),
    );
    labeled(
      policyForm,
      "Allowed symbols · comma separated",
      "symbols",
      "text",
      "BTCUSDT",
    );
    select(policyForm, "Allowed sides", "sides", [
      ["BUY", "Buy"],
      ["SELL", "Sell"],
      ["BUY,SELL", "Buy and sell"],
    ]);
    const limits = el("div", "ops-form-grid");
    labeled(limits, "Total gross turnover · USDT", "turnover", "text", "100");
    labeled(limits, "Maximum order · USDT", "max-order", "text", "10");
    policyForm.append(limits);
    labeled(
      policyForm,
      "Balance fee reserve · basis points",
      "fee-reserve",
      "text",
      "20",
    );
    const create = el("button", "button secondary", "Create policy");
    create.type = "submit";
    create.disabled = !writable();
    policyForm.append(
      create,
      el(
        "p",
        "ops-note",
        "Turnover caps apply to venue trades. External data and compute purchases keep their existing payment limits.",
      ),
    );
    policyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      create.disabled = true;
      try {
        const d = new FormData(policyForm);
        await request("/trade/policies", {
          name: d.get("policy-name"),
          connection_id: d.get("connection-id"),
          symbols: d
            .get("symbols")
            .split(",")
            .map((v) => v.trim()),
          sides: d.get("sides").split(","),
          turnover_limit_usdt: d.get("turnover"),
          max_order_usdt: d.get("max-order"),
          fee_reserve_bps: d.get("fee-reserve"),
          expires_at: Math.floor(Date.now() / 1000) + 86400,
        });
        await refresh();
      } catch (e) {
        notify(e.message, true);
        create.disabled = false;
      }
    });
    policyPanel.append(policyForm);
    root.append(
      policyPanel,
      table(
        "Turnover policies",
        ["Policy / account", "Budget · USDT", "Spent", "Held", "State"],
        trading.policies.map((p) => [
          identity(p.policy.name, p.policy.connection_id.slice(-12)),
          p.policy.turnover_limit_usdt,
          p.spent_usdt,
          p.reserved_usdt,
          status(p.status),
        ]),
        "No venue policies.",
      ),
    );
    liveControls(root, trading);
  }
  function liveControls(root, trading) {
    const section = el('section', 'ops-section'); section.append(el('h2', '', 'Live market rules'),
      el('p', 'muted', 'Connect a live market and venue account, set a rule, then review each proposed order.'));
    const form = el('form', 'ops-form');
    labeled(form, 'Rule name', 'live-name', 'text', 'MarketGuard');
    select(form, 'Live market', 'live-market', record.connections.filter(c => ['binance-public-market', 'binance-public-futures', 'binance-testnet-market', 'binance-testnet-futures'].includes(c.profile) && c.status !== 'DISCONNECTED').map(c => [c.id, c.name]));
    select(form, 'Venue policy', 'live-policy', trading.policies.filter(p => p.status === 'ACTIVE').map(p => [p.id, p.policy.name]));
    labeled(form, 'Drawdown trigger · basis points', 'live-trigger', 'number', '100');
    labeled(form, 'Order quantity', 'live-quantity', 'text', '0.001');
    select(form, 'Action', 'live-side', [['SELL', 'Sell / reduce long'], ['BUY', 'Buy / reduce short']]);
    labeled(form, 'Maximum slippage · basis points', 'live-slip', 'number', '25');
    const submit = el('button', 'button secondary', 'Save market rule'); submit.type = 'submit';
    submit.disabled = !writable() || !trading.policies.length; form.append(submit);
    form.addEventListener('submit', async e => { e.preventDefault(); submit.disabled = true;
      try { const d = new FormData(form), vp = trading.policies.find(p => p.id === d.get('live-policy'));
        const venue = record.connections.find(c => c.id === vp?.policy.connection_id);
        await request('/live/watches', {name: d.get('live-name'), market_connection_id: d.get('live-market'), venue_policy_id: d.get('live-policy'),
          trigger_drawdown_bps: Number(d.get('live-trigger')), quantity: d.get('live-quantity'), side: d.get('live-side'),
          reduce_only: venue?.profile.startsWith('binance-usdm') || false, maximum_slippage_bps: Number(d.get('live-slip')), max_age_seconds: 120});
        await refresh();
      } catch(error) { notify(error.message, true); submit.disabled = !writable(); }
    }); section.append(form); root.append(section);
    const live = record.live || {watches: [], decisions: []};
    root.append(table('Saved market rules', ['Rule', 'State', 'Check'], live.watches.map(w => [w.body.name, status(w.status), action('Evaluate', async () => {
      await request(`/connections/${w.body.market_connection_id}/sync`, {}); await request(`/live/watches/${w.id}/evaluate`, {}); await refresh();
    })]), 'No market rules.'));
    root.append(table('Market decisions', ['Market', 'Decision', 'Valid until', 'Review'], live.decisions.map(d => [
      identity(d.symbol, 'Native C++ · zero LLM calls'), status(d.action), stamp(d.expires_at),
      d.action === 'PLAN' && d.expires_at > Date.now() / 1000 ? action('Review order', async () => { currentPlan = await request(`/live/decisions/${d.id}/plan`, {}); await refresh(); }) : 'No order'
    ]), 'No observed decisions.'));
  }
  function usage() {
    const root = byId("ops-usage");
    root.replaceChildren();
    if (!record) return;
    root.append(
      table(
        "Reported AI usage",
        [
          "Model / connection",
          "Calls",
          "Input tokens",
          "Output tokens",
          "Cost · USD",
        ],
        record.usage.map((u) => [
          identity(u.model, u.connection_id),
          u.calls,
          u.input_tokens,
          u.output_tokens,
          (u.cost_microusd / 1e6).toFixed(6),
        ]),
        "No usage reports imported. Model catalog reads are not inference calls.",
        "Adapter reports · not a provider invoice",
      ),
    );
  }
  function agents() {
    const root = byId("ops-agents");
    root.replaceChildren();
    if (!record) return;
    window.AgentConsole.render(root, record, {request, refresh, writable});
    const advanced = el("details", "agent-setup");
    advanced.append(el("summary", "", "Economic IR programs · advanced"));
    programs(advanced);
    root.append(advanced);
  }
  function programs(root) {
    root.append(
      table(
        "Economic programs",
        ["Agent / program", "Capital limit", "Network", "State", "Control"],
        record.programs.map((p) => {
          const controls = el("div", "ops-row-actions");
          controls.append(
            action("Evaluate", async () => {
              const result = await request(`/programs/${p.id}/evaluate`, {
                at: new Date().toISOString(),
              });
              notify(
                "Evaluation completed. This is a kernel receipt, not a trade.",
              );
              byId("ops-program-result").textContent = JSON.stringify(
                result,
                null,
                2,
              );
              await refresh();
            }),
          );
          controls.append(
            action(p.status === "PAUSED" ? "Resume" : "Pause", async () => {
              await request(
                `/programs/${p.id}/${p.status === "PAUSED" ? "resume" : "pause"}`,
                { reason: "Owner console control" },
              );
              await refresh();
            }),
          );
          return [
            identity(p.name, p.id),
            identity(p.sandbox.capital_limit, p.sandbox.asset),
            p.network,
            status(p.status),
            controls,
          ];
        }),
        "No economic programs registered.",
      ),
    );
    const section = el("div", "ops-section"),
      head = el("div", "ops-section-head");
    head.append(el("h2", "", "Register a program"));
    section.append(head);
    const form = el("form", "ops-form"),
      label = el("label", "", "Economic IR JSON"),
      textarea = el("textarea");
    label.htmlFor = "ops-agent-source";
    textarea.id = "ops-agent-source";
    textarea.rows = 8;
    textarea.spellcheck = false;
    textarea.required = true;
    const submit = el("button", "button secondary", "Validate & register");
    submit.type = "submit";
    submit.disabled = !writable();
    form.append(
      label,
      textarea,
      submit,
      el(
        "p",
        "ops-note",
        "Connection snapshots do not invent a typed economic state. Install sourced state through the owner API or CLI before evaluation.",
      ),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const source = JSON.parse(textarea.value);
        await request("/programs/compile", source);
        await request("/programs", source);
        await refresh();
      } catch (e) {
        notify(e.message, true);
      }
    });
    section.append(form);
    root.append(section);
  }
  function render(view) {
    if (!views.has(view)) return;
    ({ overview, connections, agents, execution, usage })[view]?.();
    if (view === "playground")
      byId("ops-compile").disabled = !writable() && API_PREFIX !== "/commerce";
  }
  const updateRuntimeLabel = () => {
    byId("ops-decision-summary").hidden = true;
    const financial = byId("ops-language").value === "economics";
    byId("ops-compile").textContent = financial ? "Evaluate decision" : "Compile program";
    byId("ops-runtime-label").textContent = financial ? "C++ · state → decision · no signing" :
      byId("ops-language").value === "native" ? "Native C++ · bounded candidate" : "Economic IR · static validation";
  };
  byId("ops-language").addEventListener("change", updateRuntimeLabel);
  updateRuntimeLabel();
  byId("ops-program").addEventListener("input", () => {
    byId("ops-decision-summary").hidden = true;
    byId("ops-compiled").textContent = "Inputs changed. Evaluate again for the current decision.";
  });
  byId("ops-compile").addEventListener("click", async () => {
    byId("ops-decision-summary").hidden = true;
    try {
      if (!byId("ops-program").value.trim()) throw new Error("Load an example or paste a program before running it.");
      const source = JSON.parse(byId("ops-program").value);
      const exact = (value) => {
        if (typeof value === "number" && !Number.isSafeInteger(value)) {
          throw new Error('Use {"integer":"exact decimal"} for amounts or IDs beyond the browser integer range.');
        }
        if (value && typeof value === "object") Object.values(value).forEach(exact);
      };
      if (byId("ops-language").value === "economics") exact(source);
      let result;
      if (byId("ops-language").value === "economics") {
        if (API_PREFIX === "/commerce" && record?.read_only) {
          const response = await fetch(API_PREFIX + "/demo/economics/evaluate", {
            method: "POST", credentials: "omit", headers: {"Content-Type": "application/json"}, body: JSON.stringify(source)});
          result = await response.json();
          if (!response.ok) throw new Error(result.error || "Economic scenario rejected.");
        } else result = await request("/economics/evaluate", source);
      } else if (byId("ops-language").value === "native") {
        if (API_PREFIX === "/commerce") {
          const response = await fetch(API_PREFIX + "/demo/native-program", {
            method: "POST", credentials: "omit", headers: {"Content-Type": "application/json"}, body: JSON.stringify(source)});
          result = await response.json();
          if (!response.ok) throw new Error(result.error || "Native program rejected.");
        } else result = await request("/native/programs/evaluate", source);
      } else if (record?.read_only && API_PREFIX === "/commerce") {
        const response = await fetch(API_PREFIX + "/demo/engine/compile", {
          method: "POST",
          credentials: "omit",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(source),
        });
        result = await response.json();
        if (!response.ok)
          throw new Error(result.error || "Static compilation rejected.");
      } else result = await request("/programs/compile", source);
      byId("ops-compiled").textContent = JSON.stringify(result, null, 2);
      if (byId("ops-language").value === "economics") {
        const summary = byId("ops-decision-summary");
        summary.replaceChildren(el("strong", "", result.computed ? (result.action || "Computed") : "Abstained"),
          el("span", "", `${result.demonstration ? "Synthetic inputs" : "Input assumptions"} · ${result.language_model_calls} model calls`));
        summary.hidden = false;
      }
    } catch (e) {
      byId("ops-compiled").textContent = e instanceof SyntaxError ? "This program is not valid JSON. Check the brackets and commas, or load an example." : e.message;
    }
  });
  byId("ops-example").hidden = API_PREFIX !== "/commerce";
  byId("ops-example").addEventListener("click", async () => {
    byId("ops-decision-summary").hidden = true;
    try {
      const native = byId("ops-language").value === "native";
      const economics = byId("ops-language").value === "economics";
      const response = await fetch(API_PREFIX + (economics ? "/demo/economics/example" : native ? "/demo/native-program/example" : "/demo/engine/example"), {
        credentials: "omit",
      });
      if (!response.ok) throw new Error("Example is unavailable.");
      byId("ops-program").value = JSON.stringify(
        (await response.json()).example,
        null,
        2,
      );
      byId("ops-compiled").textContent =
        economics ? "Economic program loaded. Change the fact value from 100000000 to 200000000 to compare REDUCE with HOLD. Synthetic inputs; no signing authority." : native ? "Synthetic numeric state loaded. C++ evaluation returns a candidate and grants no execution authority." : "Synthetic historical fixture loaded. Static compilation only; no execution authority.";
    } catch (error) {
      notify(error.message, true);
    }
  });
  byId("local-owner-unlock").addEventListener("click", async () => {
    const input = byId("local-owner-token"),
      token = input.value;
    input.value = "";
    try {
      await C.unlock(token);
      byId("local-owner-access").hidden = true;
    } catch (e) {
      notify(e.message, true);
    }
  });
  window.EngineConsole = { refresh, render, openOrder: async id => {currentPlan = await request(`/trade/orders/${id}`); C.setView("execution");}, isView: (view) => views.has(view) };
  render(C.state.view);
  setInterval(() => {
    if (
      !document.hidden &&
      !document.querySelector('.ops-view:not([hidden]) details[open] form, .ops-view:not([hidden]) form[data-editing="true"]') &&
      !["INPUT", "SELECT", "TEXTAREA"].includes(
        document.activeElement?.tagName,
      ) &&
      views.has(C.state.view) &&
      record &&
      !record.read_only
    )
      refresh();
  }, 15000);
})();
