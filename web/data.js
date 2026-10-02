"use strict";

(() => {
  const C = window.MachineConsole;
  let catalog = null, busy = false, draft = null;
  const stamp = value => new Date(value * 1000).toLocaleString("en-US");
  const panel = (title, detail) => {
    const node = C.el("div", "panel");
    node.append(C.el("h2", "", title), C.el("p", "muted", detail));
    return node;
  };
  const action = (label, handler) => C.button(label, "button secondary", async () => {
    if (busy) return;
    busy = true;
    try { await handler(); } catch (error) { C.notify(error.message, true); }
    finally { busy = false; }
  });
  async function refresh() {
    try {
      catalog = await C.api("/api/data/catalog");
    } catch {
      const response = await fetch(C.API_PREFIX + "/demo/datapass", {credentials: "omit"});
      if (!response.ok) throw new Error("Dataset releases are unavailable.");
      catalog = await response.json();
    }
    paint();
  }
  function showDraft(value) {
    draft = value;
    const node = document.getElementById("data-plan");
    node.textContent = JSON.stringify(value, null, 2);
    node.hidden = false;
  }
  function paint() {
    const root = document.getElementById("ops-data");
    root.replaceChildren();
    if (!catalog) { root.append(C.el("p", "muted", "Loading source-bound releases…")); return; }
    for (const item of catalog.products) {
      const product = panel(item.name, `${item.coverage.rows} public price observations · ${item.coverage.regions.length} APAC regions · ${stamp(item.as_of)}`);
      const links = C.el("div", "ops-actions");
      const atlas = C.el("a", "button secondary", "Explore Atlas ↗");
      atlas.href = "https://skew-economic-machine.angus4314.chatgpt.site/atlas.html";
      atlas.target = "_blank"; atlas.rel = "noreferrer";
      links.append(atlas, action("Refresh release", refresh));
      product.append(C.el("p", "", "24-hour transferable access to an original derived report. Public prices do not establish available capacity."));
      const hashes = C.el("pre", "receipt-json", `Report ${item.version}\nTerms  ${item.terms_sha256}\nSources ${item.source_observation_root}`);
      product.append(hashes, links); root.append(product);
    }
    const live = catalog.datapass.status !== "NOT_DEPLOYED";
    const licensing = panel("Arbitrum DataPass", live ? "Configured contract. Each delivery verifies finalized state against two RPC providers." : "Contract validated locally on the server. Public Arbitrum deployment has not been recorded.");
    licensing.append(C.el("p", "muted", live ? catalog.datapass.contract : "Arbitrum Sepolia · no deployed license token yet"));
    const controls = C.el("div", "ops-actions");
    if (!C.PREVIEW && !C.LOCAL_ENGINE) {
      controls.append(action("Prepare deployment", async () => showDraft(await C.api("/api/data/deployment-plan", {}))));
      const register = action("Prepare release", async () => showDraft(await C.api("/api/data/release-plan", {price_atoms: 10000, sale_duration_seconds: 86400})));
      register.disabled = !live; controls.append(register);
      const purchase = action("Prepare purchase", async () => {
        const id = "0x" + Array.from(crypto.getRandomValues(new Uint8Array(32)), n => n.toString(16).padStart(2, "0")).join("");
        showDraft(await C.api("/api/data/purchase-plan?purchase_id=" + encodeURIComponent(id)));
      });
      purchase.disabled = !live; controls.append(purchase);
      const label = C.el("label", "", "License token ID");
      const input = C.el("input"); input.inputMode = "numeric"; input.placeholder = "e.g. 1";
      label.append(input);
      const deliver = action("Verify & deliver", async () => {
        if (!/^[1-9][0-9]{0,77}$/.test(input.value)) throw new Error("Enter a positive license token ID.");
        showDraft(await C.api("/api/data/licenses/" + input.value + "/delivery"));
      });
      deliver.disabled = !live; licensing.append(label); controls.append(deliver);
      const priceLabel = C.el("label", "", "Resale price · USDC");
      const price = C.el("input"); price.inputMode = "decimal"; price.value = "0.01";
      priceLabel.append(price); licensing.append(priceLabel);
      const list = action("Prepare sale", async () => {
        if (!/^[1-9][0-9]{0,77}$/.test(input.value)) throw new Error("Enter a positive license token ID.");
        if (!/^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$/.test(price.value)) throw new Error("Set a price above zero and at most 1 USDC, with up to six decimals.");
        const [whole, fraction = ""] = price.value.split(".");
        const atoms = Number(whole) * 1000000 + Number(fraction.padEnd(6, "0"));
        if (!atoms) throw new Error("The sale price must be positive.");
        showDraft(await C.api("/api/data/sale-plan", {token_id: input.value, price_atoms: atoms, sale_duration_seconds: 3600, version: catalog.products[0].version}));
      });
      list.disabled = !live; controls.append(list);
      const resale = action("Prepare resale purchase", async () => {
        if (!/^[1-9][0-9]{0,77}$/.test(input.value)) throw new Error("Enter a positive license token ID.");
        const id = "0x" + Array.from(crypto.getRandomValues(new Uint8Array(32)), n => n.toString(16).padStart(2, "0")).join("");
        showDraft(await C.api("/api/data/resale-plan?token_id=" + input.value + "&purchase_id=" + id + "&min_remaining_seconds=60"));
      });
      resale.disabled = !live; controls.append(resale);
    }
    licensing.append(controls, C.el("p", "muted", "Prepared plans are unsigned. Resale transfers the remaining original license; it does not renew its expiry. Native license purchases and x402 are separate payment paths; one purchase must not pay both."));
    const output = C.el("pre", "receipt-json"); output.id = "data-plan"; output.hidden = !draft;
    if (draft) output.textContent = JSON.stringify(draft, null, 2);
    licensing.append(output); root.append(licensing);
    const api = panel("Agent access", "Create a data:read API key in your wallet workspace. Keys never acquire wallet signing authority.");
    api.append(C.el("pre", "receipt-json", "GET /commerce/api/data/catalog\nGET /commerce/api/data/purchase-plan?purchase_id=0x…\nGET /commerce/api/data/resale-plan?token_id=1&purchase_id=0x…\nGET /commerce/api/data/licenses/{token_id}/delivery"));
    root.append(api);
  }
  window.DataConsole = {render: view => {
    if (view !== "data") return;
    if (catalog) paint();
    else refresh().catch(error => C.notify(error.message, true));
  }};
})();
