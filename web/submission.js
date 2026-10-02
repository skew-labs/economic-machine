"use strict";
const $ = (id) => document.getElementById(id);
const money = (atoms) =>
  `${(Number(atoms) / 1e6).toLocaleString("en-US", { maximumFractionDigits: 6 })} test USDC`;
let bundle;
const reasons = {
  PRICE_OUTSIDE_BUYER_POLICY: "Price exceeds this budget.",
  DATA_NOT_FRESH: "The snapshot is older than this freshness limit.",
  LICENSE_NOT_GRANTED: "The seller does not grant commercial usage.",
};

function render(record) {
  bundle = record;
  $("supplier").textContent = record.supplier.name;
  $("version").textContent = record.supplier.data_version;
  $("ask").textContent = record.supplier.ask;
  $("counter").textContent = record.supplier.counter;
  $("discount").textContent = `${record.supplier.discount_bps / 100}%`;
  $("units").textContent = `${record.policy.units} snapshot`;
  $("model-calls").textContent = record.agreement.language_model_calls;
  $("terms-hash").textContent = record.agreement.terms_hash;
  $("agreement-status").textContent = "Agreed";
  $("replay-result").textContent =
    "Original policy replay matches the recorded agreement.";
  $("signature-result").textContent = "Signature matches runtime record";
  $("signer").textContent = record.authorization.signer;
  $("authorization-hash").textContent = record.authorization.payload_hash;
  $("trade-state").textContent = record.payment.status;
  $("settled-amount").textContent =
    `${record.payment.amount} test USDC finalized`;
  $("tx-hash").textContent = record.payment.tx_hash;
  $("receipt-block").textContent =
    record.payment.receipt_block.toLocaleString("en-US");
  $("capital-hold").textContent = money(record.capital.reserved_atoms);
  $("capital-spent").textContent = money(record.capital.spent_atoms);
  $("delivery-result").textContent = "Delivered data matches chain state";
  $("artifact").textContent = JSON.stringify(record.delivery.artifact, null, 2);
  $("artifact-hash").textContent = record.delivery.artifact_hash;
  $("buyer-before").textContent = money(record.capital.buyer_before_atoms);
  $("buyer-after").textContent = money(record.capital.buyer_after_atoms);
  $("seller-received").textContent = money(
    record.capital.seller_after_atoms - record.capital.seller_before_atoms,
  );
  $("token-address").textContent = record.token;
  $("bundle-hash").textContent = record.bundle_hash;
  $("checked-at").textContent = new Date(
    record.checked_at * 1000,
  ).toISOString();
  $("load-status").textContent =
    `Executed ${new Date(record.policy.evaluated_at * 1000).toLocaleString("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} UTC. Read-only record; no new payment.`;
  for (const event of record.audit.timeline) {
    const item = document.createElement("li"),
      name = document.createElement("strong"),
      hash = document.createElement("code");
    name.textContent = event.kind.toLowerCase().replaceAll("_", " ");
    hash.textContent = event.event_hash;
    item.append(name, hash);
    $("timeline").append(item);
  }
  $("replay").disabled = false;
}

$("policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!bundle) return;
  $("replay").disabled = true;
  try {
    const response = await fetch("/commerce/demo/trade/replay", {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        budget: $("budget").value,
        max_age_seconds: Number($("freshness").value),
        license: $("license").value,
      }),
      signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || "Policy replay unavailable.");
    const agreed = result.result.status === "AGREED";
    $("agreement-status").textContent = agreed
      ? "Policy accepts"
      : "Policy rejects";
    $("agreement-status").classList.toggle("rejected", !agreed);
    $("replay-result").textContent = agreed
      ? "This policy accepts the same 0.01 test-USDC offer. The historical payment record stays unchanged."
      : result.result.reason_codes
          .map((code) => reasons[code] || code)
          .join(" ") + " No payment is authorized.";
  } catch (error) {
    $("replay-result").textContent = error.message;
  } finally {
    $("replay").disabled = false;
  }
});

fetch("/commerce/demo/trade", {
  credentials: "omit",
  signal: AbortSignal.timeout(15000),
})
  .then(async (response) => {
    const record = await response.json();
    if (
      !response.ok ||
      record.verified !== true ||
      record.read_only !== true ||
      record.new_payment_authorized !== false
    )
      throw new Error("Verified completed-trade evidence is unavailable.");
    render(record);
  })
  .catch((error) => {
    $("load-status").textContent = error.message;
    $("load-status").classList.add("error");
  });
