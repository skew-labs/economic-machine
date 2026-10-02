"""Offline second-path audit of prices against retained official input bytes.

No recollection and no model. This is independent of collector selection logic:
it matches each selected row to a meter/offer in its content-addressed archive.
Raw files stay private. A passed audit proves archived source agreement, not
provider inventory, retrieval timestamp notarization or redistribution rights.
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from machine_commerce.atlas import load_report

ROOT = Path(__file__).resolve().parents[1]


def checksum(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Source audit runs only on the remote host")
    import ijson
    report = load_report(ROOT / "artifacts/atlas-release/atlas.json")
    grouped = defaultdict(list)
    for row in report["observations"]:
        grouped[row["source"]["raw_sha256"]].append(row)
    private = ROOT / "private/atlas-upstream"
    files = {}
    for p in private.glob("*.json"):
        if p.is_symlink() or p.stat().st_size > 512000000:
            raise RuntimeError("Unsafe source archive")
        files[checksum(p)] = p
    checked, archives = 0, []
    for sha, rows in grouped.items():
        path = files[sha]
        if rows[0]["provider"] == "Azure":
            body = json.loads(path.read_text())
            items = body["Items"]
            for row in rows:
                matches = [item for item in items if item.get("meterId") == row["meter_id"]
                    and item.get("armSkuName") == row["sku"] and item.get("armRegionName") == row["region"]
                    and Decimal(str(item["retailPrice"])) == Decimal(row["instance_usd_hour"])
                    and item.get("currencyCode") == "USD" and item.get("unitOfMeasure") == "1 Hour"
                    and item.get("type") == "Consumption" and "Windows" not in item.get("productName", "")
                    and item.get("effectiveStartDate") == row["source"]["effective_at"]]
                if not matches:
                    raise RuntimeError("Azure selected price does not match source bytes")
                checked += 1
        else:
            selected = {row["upstream_sku"] for row in rows}
            products, rates = {}, defaultdict(list)
            with path.open("rb") as source:
                for sku, product in ijson.kvitems(source, "products"):
                    if sku in selected:
                        products[sku] = product["attributes"]
            with path.open("rb") as source:
                for sku, offers in ijson.kvitems(source, "terms.OnDemand"):
                    if sku in selected:
                        for offer in offers.values():
                            for price in offer["priceDimensions"].values():
                                if price["unit"] == "Hrs" and price["beginRange"] == "0":
                                    rates[sku].append((Decimal(price["pricePerUnit"]["USD"]), offer["effectiveDate"]))
            for row in rows:
                sku = row["upstream_sku"]
                attrs = products[sku]
                if (attrs["regionCode"] != row["region"] or attrs["instanceType"] != row["sku"]
                        or attrs["operatingSystem"] != "Linux" or attrs["tenancy"] != "Shared"
                        or (Decimal(row["instance_usd_hour"]), row["source"]["effective_at"]) not in rates[sku]):
                    raise RuntimeError("AWS selected price does not match source bytes")
                checked += 1
        archives.append({"sha256": sha, "bytes": path.stat().st_size,
                         "provider": rows[0]["provider"], "rows_checked": len(rows)})
    if checked != report["derived"]["coverage"]["rows"]:
        raise RuntimeError("Not every observation was rechecked")
    result = {"schema": "atlas-source-audit-1", "accepted": True,
              "report_sha256": report["report_sha256"], "observations_checked": checked,
              "archives": sorted(archives, key=lambda item: item["sha256"]),
              "assurance": "ARCHIVED_OFFICIAL_SOURCE_AGREEMENT_NOT_INVENTORY_OR_RIGHTS_PROOF",
              "financial_transmissions": 0}
    destination = ROOT / "artifacts/atlas-release/source-audit.json"
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"accepted": True, "checked": checked, "archives": len(archives)}))


if __name__ == "__main__":
    main()
