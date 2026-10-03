"use strict";
const PORTAL = "https://machine.148-113-153-116.nip.io/commerce";
const $ = (id) => document.getElementById(id);
const node = (tag, text, klass) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (klass) element.className = klass;
  return element;
};
const canonical = (value) => JSON.stringify(sort(value));
function sort(value) {
  if (Array.isArray(value)) return value.map(sort);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sort(value[key])]),
    );
  return value;
}
async function sha256(value) {
  const bytes = new TextEncoder().encode(canonical(value));
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
async function api(path, options = {}) {
  const response = await fetch(PORTAL + path, {
    cache: "no-store",
    ...options,
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(result.error || "The source service is unavailable.");
  return result;
}
const usd = (value) =>
  Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
const date = (value) =>
  new Date(value * 1000).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
let release;
async function atlas() {
  if (release) return release;
  let value;
  try {
    value = await api("/demo/atlas");
  } catch {
    const response = await fetch("atlas.json", { cache: "no-store" });
    if (!response.ok)
      throw new Error("The source-verified snapshot is unavailable.");
    value = await response.json();
  }
  if (
    value.schema !== "atlas-public-release-1" ||
    (await sha256(value.derived)) !== value.report_sha256 ||
    (await sha256(value.terms)) !== value.terms_sha256 ||
    (await sha256(value.observations)) !== value.derived.source_observation_root
  )
    throw new Error("The release integrity check failed.");
  release = value;
  return value;
}
function metric(label, value) {
  const cell = node("div", undefined, "result-cell");
  cell.append(node("span", label), node("strong", value));
  return cell;
}
function coverage(target, report) {
  const c = report.derived.coverage;
  for (const [label, value] of [
    ["Providers", c.providers.length],
    ["Regions", c.regions.length],
    ["Observations", c.rows],
    ["Hardware documented", c.known_hardware_rows],
  ]) {
    const item = node("div");
    item.append(node("strong", value), node("span", label));
    target.append(item);
  }
}
async function home() {
  if (!$("home-coverage")) return;
  const report = await atlas();
  $("home-coverage").replaceChildren();
  const c = report.derived.coverage;
  for (const [label, value] of [
    ["Hyperscalers", c.providers.length],
    ["APAC regions", c.regions.length],
    ["Price observations", c.rows],
  ]) {
    const item = node("div");
    item.append(node("strong", value), node("span", label));
    $("home-coverage").append(item);
  }
  $("home-rates").replaceChildren();
  const selected = report.derived.statistics
    .filter((s) => s.gpu_model === "A100")
    .slice(0, 5);
  for (const row of selected) {
    const tr = node("tr");
    const location = report.observations.find(
      (r) => r.provider === row.provider && r.region === row.region,
    );
    const label = node("td", row.provider);
    label.append(node("small", location?.city || row.region));
    tr.append(label, node("td", row.gpu_model), node("td", usd(row.median)));
    $("home-rates").append(tr);
  }
  $("home-asof").textContent =
    `Median list prices · ${date(report.derived.as_of)}`;
}
async function table() {
  const report = await atlas();
  $("atlas-asof").textContent =
    `Observed ${date(report.derived.as_of)} · Integrity checked`;
  coverage($("atlas-coverage"), report);
  for (const region of report.derived.coverage.regions) {
    const item = report.observations.find((r) => r.region === region);
    const option = node("option", `${item.city} · ${region}`);
    option.value = region;
    $("region-filter").append(option);
  }
  $("atlas-collection").textContent = JSON.stringify(
    report.derived.collection.map((c) => ({
      provider: c.provider,
      region: c.region,
      status: c.status,
      rows: c.rows,
      source_pages: c.pages?.length || 0,
    })),
    null,
    2,
  );
  let maximum = 50;
  function render() {
    const provider = $("provider-filter").value,
      region = $("region-filter").value,
      gpu = $("gpu-filter").value;
    const rows = report.observations.filter(
      (r) =>
        (!provider || r.provider === provider) &&
        (!region || r.region === region) &&
        (!gpu || (gpu === "unknown" ? !r.gpu_model : r.gpu_model === gpu)),
    );
    $("atlas-rows").replaceChildren();
    for (const row of rows.slice(0, maximum)) {
      const tr = node("tr");
      const model = row.gpu_model
        ? `${row.gpu_count} × ${row.gpu_model}`
        : "Unclassified";
      const sku = node("td", row.sku);
      sku.append(node("small", "Linux · On-demand list"));
      const link = node("a", "Source ↗");
      const url = new URL(row.source.url);
      if (
        url.protocol !== "https:" ||
        !["prices.azure.com", "pricing.us-east-1.amazonaws.com"].includes(
          url.hostname,
        )
      )
        throw new Error("Unsupported source URL");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener";
      const source = node("td");
      source.append(link);
      source.title = row.source.raw_sha256;
      tr.append(
        node("td", row.provider, "vendor"),
        node("td", row.city),
        sku,
        node("td", model),
        node("td", usd(row.instance_usd_hour), "numeric"),
        node("td", row.gpu_usd_hour ? usd(row.gpu_usd_hour) : "—", "numeric"),
        source,
      );
      $("atlas-rows").append(tr);
    }
    if (!rows.length) {
      const td = node("td", "No matching observations.");
      td.colSpan = 7;
      const tr = node("tr");
      tr.append(td);
      $("atlas-rows").append(tr);
    }
    $("atlas-count").textContent =
      `${Math.min(maximum, rows.length)} of ${rows.length} observations`;
    $("atlas-more").hidden = rows.length <= maximum;
  }
  for (const id of ["provider-filter", "region-filter", "gpu-filter"])
    $(id).addEventListener("change", () => {
      maximum = 50;
      render();
    });
  $("atlas-more").addEventListener("click", () => {
    maximum += 50;
    render();
  });
  render();
}
async function capacity(event) {
  event.preventDefault();
  const form = $("capacity-form");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const request = Object.fromEntries(new FormData(form));
    const response = await api("/demo/site-lens", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if ((await sha256(request)) !== response.input_sha256)
      throw new Error("Scenario input hash mismatch");
    const grid = node("div", undefined, "result-grid");
    for (const [label, value] of [
      ["GPU capacity", response.gpus.toLocaleString()],
      ["Servers", response.servers.toLocaleString()],
      ["IT power", `${response.it_mw} MW`],
      ["Monthly power cost", `$${usd(response.monthly_power_cost_usd)}`],
      ["Monthly gross revenue", `$${usd(response.monthly_gross_revenue_usd)}`],
      [
        "Revenue less power",
        `$${usd(response.monthly_revenue_less_power_usd)}`,
      ],
    ])
      grid.append(metric(label, value));
    $("capacity-results").replaceChildren(grid);
    $("capacity-proof").textContent = JSON.stringify(
      { request, response },
      null,
      2,
    );
  } catch (error) {
    $("capacity-results").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
async function dataPass() {
  const catalog = await api("/demo/datapass");
  const product = catalog.products[0];
  $("datapass-status").textContent =
    catalog.datapass.status === "NOT_DEPLOYED"
      ? "Arbitrum deployment pending"
      : "Configured · Verify on-chain";
  if (catalog.datapass.status !== "NOT_DEPLOYED") {
    const proof = await api("/demo/datapass/deployment");
    if (proof.status === "PUBLIC_TESTNET_DEPLOYED" &&
        proof.contract_address.toLowerCase() === catalog.datapass.contract.toLowerCase()) {
      const link = document.createElement("a");
      link.href = `https://sepolia.arbiscan.io/address/${proof.contract_address}`;
      link.textContent = "Arbitrum Sepolia · View contract";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      $("datapass-status").replaceChildren(link);
    }
  }
  for (const [label, value] of [
    [
      "Coverage",
      `${product.coverage.providers.join(" + ")} · ${product.coverage.regions.length} APAC regions`,
    ],
    ["Release", date(product.as_of)],
    ["Content SHA-256", product.version],
    ["Terms SHA-256", product.terms_sha256],
  ]) {
    const item = node("div");
    item.append(
      node("span", label),
      node(label.includes("SHA") ? "code" : "strong", value),
    );
    $("dataset-facts").append(item);
  }
  $("dataset-proof").textContent = JSON.stringify(catalog, null, 2);
}
async function native() {
  const results = await api("/demo/native");
  const grid = node("div", undefined, "result-grid");
  for (const [label, value] of [
    ["Kernel p50", `${results.kernel.p50_ns} ns`],
    ["Kernel p95", `${results.kernel.p95_ns} ns`],
    [
      "Queue transit p95",
      `${(results.spsc_transit.p95_ns / 1000).toFixed(1)} μs`,
    ],
    ["Samples", results.kernel.samples.toLocaleString()],
    ["Model calls", results.llm_calls],
    ["Financial transmissions", results.financial_transmissions],
  ])
    grid.append(metric(label, value));
  $("native-results").replaceChildren(grid);
}
const view = document.body.dataset.tool;
const actions = { home, atlas: table, "data-pass": dataPass, engine: native };
if (view === "site-lens")
  $("capacity-form").addEventListener("submit", capacity);
if (actions[view])
  actions[view]().catch((error) => {
    const target =
      $("atlas-error") ||
      $("native-results") ||
      $("dataset-facts") ||
      $("home-coverage");
    if (target) target.textContent = error.message;
  });
