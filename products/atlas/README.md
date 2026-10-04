<img src="../../site/assets/app-atlas.svg" width="64" alt="Atlas">

# Atlas

Compare public APAC compute list prices with timestamps, source URLs and content hashes.
Atlas records observations, not available capacity or a purchasable reservation.

[Open Atlas](https://skew.deals/commerce/atlas.html)

| Area | Source |
| --- | --- |
| Normalization, classification, report validation | [atlas.py](../../src/machine_commerce/atlas.py) |
| Bounded AWS / Azure collector | [collect_atlas.py](../../scripts/collect_atlas.py) |
| Independent source audit | [audit_atlas_sources.py](../../scripts/audit_atlas_sources.py) |
| Browser | [atlas.html](../../site/atlas.html), [tools.js](../../site/tools.js) |

```python
from machine_commerce.atlas import load_report, verify_report
report = load_report("site/atlas.json")
verify_report(report)
```

For collection, install `pip install -e '.[atlas]'` on your Linux collection host.
The maintainer collector currently requires a checkout under `/srv/skew/` and writes raw
archives to `private/atlas-upstream/`; `--out` selects the report destination. AWS collection
is opt-in with `--aws` because price catalogs can be large.

Unknown GPU models remain unclassified. The software license does not grant redistribution
rights over third-party archives. Review source terms before publishing a DataPass product.
