<img src="../../site/assets/app-site-lens.svg" width="64" alt="SiteLens">

# SiteLens

Estimate GPU capacity from facility power and operating assumptions. The result is a planning
scenario, not a verified inventory or an underwriting opinion.

[Open SiteLens](https://skew.deals/commerce/site-lens.html)

- Calculation: `site_lens` in [atlas.py](../../src/machine_commerce/atlas.py).
- Browser: [site-lens.html](../../site/site-lens.html), [tools.js](../../site/tools.js).
- Shared report types and source provenance: [Atlas](../atlas/README.md).

The web tool applies the bounded deterministic model to user-supplied facility assumptions.
It does not book servers or infer power availability from a site address.
