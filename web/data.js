"use strict";

(() => {
  const C = window.MachineConsole;
  let catalog = null, busy = false, draft = null, nativeStage = 'REVIEW', nativePurchase = null, nativePlan = null;
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
    if (value.purchase_id && value.transactions?.[1]?.purpose === 'VERSION_BOUND_LICENSE_PURCHASE') {
      nativeStage = 'REVIEW'; nativePlan=value; nativePurchase = {owner:value.from.toLowerCase(),purchase_id:value.purchase_id,version:value.report_sha256,stage:nativeStage,plan:value};
      savePurchase(); paint();
    }
  }
  function savePurchase() {
    if (nativePurchase) sessionStorage.setItem('machine_datapass_purchase_'+nativePurchase.owner,JSON.stringify(nativePurchase));
  }
  function purchaseControls(root) {
    if(C.PREVIEW || C.LOCAL_ENGINE || !C.state.connected || !C.state.identity) return;
    const owner=C.state.identity.address.toLowerCase();
    if(nativePurchase?.owner!==owner) {nativePurchase=null;nativePlan=null;nativeStage='REVIEW';}
    if (!nativePurchase) {
      try { nativePurchase = JSON.parse(sessionStorage.getItem('machine_datapass_purchase_'+owner)); nativeStage = nativePurchase?.stage || 'REVIEW'; nativePlan=nativePurchase?.plan; } catch { nativePurchase = null; }
    }
    if (!nativePurchase) return;
    const box = panel('Your license purchase', 'Arbitrum Sepolia · test USDC · exact allowance · separate from x402.');
    box.append(C.el('p','',nativePlan?.amount_atoms ? `${Number(nativePlan.amount_atoms)/1000000} test USDC · 24-hour access` : 'A previous purchase can be checked below.'));
    if (nativePlan?.purchase_id === nativePurchase.purchase_id && ['REVIEW','APPROVAL_SENT'].includes(nativeStage)) {
      box.append(action(nativeStage === 'REVIEW' ? 'Approve exact USDC amount' : 'Buy license in wallet', async () => {
        if (!C.getWallet()) { C.signIn(); return; }
        const step = nativeStage === 'REVIEW' ? 0 : 1;
        if (step) {
          const updated = await C.api('/api/data/purchase-plan?purchase_id='+nativePurchase.purchase_id+'&version='+nativePurchase.version);
          if (updated.amount_atoms !== nativePlan.amount_atoms || updated.transactions[1].data !== nativePlan.transactions[1].data)
            throw new Error('License terms changed. Review a new purchase before signing.');
          nativePlan = updated; nativePurchase.plan=updated; savePurchase();
        }
        const previous = nativeStage;
        nativeStage = 'SUBMISSION_UNKNOWN'; nativePurchase.stage = nativeStage; savePurchase();
        try {
          const result = await WalletBridge.sendDataPassStep(C.getWallet(),nativePlan,catalog.datapass.contract,step);
          nativeStage = step ? 'PURCHASE_SENT' : 'APPROVAL_SENT'; nativePurchase.stage = nativeStage;
          nativePurchase[step ? 'purchase_tx' : 'approval_tx'] = result.tx_hash; savePurchase(); paint();
        } catch (error) {
          if (!error.submission_uncertain) nativeStage = previous;
          nativePurchase.stage = nativeStage; savePurchase(); paint(); throw error;
        }
      }));
    }
    box.append(C.el('p','muted',nativeStage.replaceAll('_',' ').toLowerCase()), action('Check purchase & deliver',async () => {
      const status = await C.api('/api/data/purchase-status?purchase_id='+nativePurchase.purchase_id+'&version='+nativePurchase.version);
      if (status.status === 'DELIVERED') { nativeStage='DELIVERED'; nativePurchase.stage=nativeStage; savePurchase(); }
      else C.notify('This purchase is not visible in finalized state yet. Do not buy again.');
      draft = status; paint();
    }));
    for(const key of ['approval_tx','purchase_tx']) if(nativePurchase[key]) {
      const link=C.el('a','button secondary',key==='approval_tx' ? 'Approval transaction' : 'Purchase transaction');
      link.href='https://sepolia.arbiscan.io/tx/'+nativePurchase[key]; link.target='_blank';link.rel='noreferrer';box.append(link);
    }
    if(nativeStage==='SUBMISSION_UNKNOWN') box.append(C.el('p','muted','Submission may have reached the chain. Keep this purchase ID and check it; another signature is disabled.'));
    box.append(C.el('pre','receipt-json',nativePurchase.purchase_id)); root.append(box);
  }
  function paint() {
    const root = document.getElementById("ops-data");
    root.replaceChildren();
    const shopping = panel("Purchase data for your agents", "Use Buy services for x402 checkout or Subscriptions for monthly Atlas access. DataPass licenses use a separate contract purchase.");
    shopping.append(C.button("Buy services", "button secondary", () => C.setView("market")),
      C.button("Atlas subscription", "button secondary", () => C.setView("subscriptions")));
    root.append(shopping);
    if (!catalog) { root.append(C.el("p", "muted", "Loading source-bound releases…")); return; }
    for (const item of catalog.products) {
      const product = panel(item.name, `${item.coverage.rows} public price observations · ${item.coverage.regions.length} APAC regions · ${stamp(item.as_of)}`);
      const links = C.el("div", "ops-actions");
      const atlas = C.el("a", "button secondary", "Explore Atlas ↗");
      atlas.href = "/commerce/atlas.html";

      links.append(atlas, action("Refresh release", refresh));
      product.append(C.el("p", "", "24-hour transferable access to an original derived report. Public prices do not establish available capacity."));
      const hashes = C.el("pre", "receipt-json", `Report ${item.version}\nTerms  ${item.terms_sha256}\nSources ${item.source_observation_root}`);
      const details = C.el("details", "release-details"); details.append(C.el("summary", "", "Release details"), hashes);
      product.append(links, details); root.append(product);
    }
    const live = catalog.datapass.status !== "NOT_DEPLOYED";
    const licensing = panel("Arbitrum DataPass", live ? "Configured contract. Each delivery verifies finalized state against two RPC providers." : "Contract validated locally on the server. Public Arbitrum deployment has not been recorded.");
    licensing.append(C.el("p", "muted", live ? catalog.datapass.contract : "Arbitrum Sepolia · no deployed license token yet"));
    const controls = C.el("div", "ops-actions"), advancedControls = C.el("div", "ops-actions");
    const advanced = C.el("details", "release-details");
    advanced.append(C.el("summary", "", "Existing licenses & publisher tools"));
    if (!C.PREVIEW && !C.LOCAL_ENGINE) {
      advancedControls.append(action("Prepare deployment", async () => showDraft(await C.api("/api/data/deployment-plan", {}))));
      const register = action("Prepare release", async () => showDraft(await C.api("/api/data/release-plan", {price_atoms: 10000, sale_duration_seconds: 86400})));
      register.disabled = !live; advancedControls.append(register);
      const purchase = action("Review license purchase", async () => {
        if (!C.state.connected) { C.signIn(); return; }
        if (nativePurchase && !['REVIEW','DELIVERED'].includes(nativeStage)) throw new Error('Check your existing license purchase before creating another.');
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
      deliver.disabled = !live; advanced.append(label); advancedControls.append(deliver);
      const priceLabel = C.el("label", "", "Resale price · USDC");
      const price = C.el("input"); price.inputMode = "decimal"; price.value = "0.01";
      priceLabel.append(price); advanced.append(priceLabel);
      const list = action("Prepare sale", async () => {
        if (!/^[1-9][0-9]{0,77}$/.test(input.value)) throw new Error("Enter a positive license token ID.");
        if (!/^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$/.test(price.value)) throw new Error("Set a price above zero and at most 1 USDC, with up to six decimals.");
        const [whole, fraction = ""] = price.value.split(".");
        const atoms = Number(whole) * 1000000 + Number(fraction.padEnd(6, "0"));
        if (!atoms) throw new Error("The sale price must be positive.");
        showDraft(await C.api("/api/data/sale-plan", {token_id: input.value, price_atoms: atoms, sale_duration_seconds: 3600, version: catalog.products[0].version}));
      });
      list.disabled = !live; advancedControls.append(list);
      const resale = action("Prepare resale purchase", async () => {
        if (!/^[1-9][0-9]{0,77}$/.test(input.value)) throw new Error("Enter a positive license token ID.");
        const id = "0x" + Array.from(crypto.getRandomValues(new Uint8Array(32)), n => n.toString(16).padStart(2, "0")).join("");
        showDraft(await C.api("/api/data/resale-plan?token_id=" + input.value + "&purchase_id=" + id + "&min_remaining_seconds=60"));
      });
      resale.disabled = !live; advancedControls.append(resale);
    }
    advanced.append(advancedControls);
    licensing.append(controls, advanced, C.el("p", "muted", "Prepared plans are unsigned. Resale transfers the remaining original license; it does not renew its expiry. Native license purchases and x402 are separate payment paths; one purchase must not pay both."));
    const output = C.el("pre", "receipt-json"); output.id = "data-plan"; output.hidden = !draft;
    if (draft) output.textContent = JSON.stringify(draft, null, 2);
    licensing.append(output); root.append(licensing);
    purchaseControls(root);
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
