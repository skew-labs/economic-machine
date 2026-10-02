"""Bounded official APAC price collection; remote host only. No signing."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from economic_machine.values import MachineError
from machine_commerce.atlas import REGIONS, azure_rows, build_report, observation

ROOT = Path(__file__).resolve().parents[1]


def fetch(client, url, destination, maximum):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {"prices.azure.com", "pricing.us-east-1.amazonaws.com"}
            or parts.username or parts.password or parts.port not in {None, 443}):
        raise MachineError("ALLOWLISTED_SOURCE_REQUIRED")
    checksum, size = hashlib.sha256(), 0
    partial = destination.with_suffix(".partial")
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > maximum:
                        raise MachineError("UPSTREAM_SIZE_LIMIT")
                    checksum.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return checksum.hexdigest(), size


def azure(client, region, folder):
    expression = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
                  "and (contains(armSkuName, 'NC') or contains(armSkuName, 'ND') or contains(armSkuName, 'NV')) "
                  "and priceType eq 'Consumption'")
    url = "https://prices.azure.com/api/retail/prices?" + urlencode({"$filter": expression})
    rows, pages, seen = [], [], set()
    for page in range(20):
        if url in seen:
            raise MachineError("UPSTREAM_PAGINATION_LOOP")
        seen.add(url)
        target = folder / f"azure-{region}-{page}.json"
        sha, size = fetch(client, url, target, 4_000_000)
        body = json.loads(target.read_text())
        observed_at = int(time.time())
        rows.extend(azure_rows(body["Items"], url, sha, observed_at))
        pages.append({"url": url, "sha256": sha, "bytes": size, "retrieved_at": observed_at})
        url = body.get("NextPageLink")
        if not url:
            return rows, pages
    raise MachineError("UPSTREAM_PAGINATION_LIMIT")


def aws(client, region, folder):
    import ijson
    url = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.json"
    target = folder / f"aws-{region}.json"
    sha, size = fetch(client, url, target, 512_000_000)
    at = int(time.time())
    selected = {}
    with target.open("rb") as source:
        for sku, product in ijson.kvitems(source, "products"):
            attrs = product.get("attributes", {})
            if (product.get("productFamily") == "Compute Instance" and attrs.get("gpu")
                    and attrs.get("operatingSystem") == "Linux" and attrs.get("tenancy") == "Shared"
                    and attrs.get("preInstalledSw") == "NA" and attrs.get("capacitystatus") == "Used"
                    and attrs.get("regionCode") == region and attrs.get("operation") == "RunInstances"):
                selected[sku] = attrs
                if len(selected) > 1000:
                    raise MachineError("AWS_SKU_LIMIT")
    rows = []
    with target.open("rb") as source:
        for sku, offers in ijson.kvitems(source, "terms.OnDemand"):
            if sku not in selected:
                continue
            attrs = selected[sku]
            count = int(attrs["gpu"]) if str(attrs["gpu"]).isdigit() else None
            # GPU manufacturer doesn't establish model. Explicit families only.
            instance = attrs["instanceType"]
            model = ("H100" if instance.startswith("p5.") else "A100" if instance.startswith(("p4d.", "p4de."))
                     else "V100" if instance.startswith("p3.") else "T4" if instance.startswith("g4dn.") else None)
            for offer in offers.values():
                for dimension in offer["priceDimensions"].values():
                    price = dimension.get("pricePerUnit", {}).get("USD")
                    if dimension.get("unit") != "Hrs" or dimension.get("beginRange") != "0" or not price or float(price) <= 0:
                        continue
                    rows.append(observation("AWS", region, instance, price,
                        {"url": url, "raw_sha256": sha, "retrieved_at": at, "effective_at": offer.get("effectiveDate")},
                        gpu_model=model, gpu_count=count if model else None,
                        product_name=dimension.get("description"), upstream_sku=sku))
    return rows, [{"url": url, "sha256": sha, "bytes": size, "retrieved_at": at}]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Collection runs only on the owner-selected remote host")
    parser = argparse.ArgumentParser()
    parser.add_argument("--aws", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "artifacts/atlas-release"))
    args = parser.parse_args()
    output = Path(args.out)
    private = ROOT / "private/atlas-upstream"
    output.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    rows, collection = [], []
    with httpx.Client(timeout=httpx.Timeout(90, connect=15), trust_env=False, follow_redirects=False) as client:
        for region in REGIONS:
            provider = "AWS" if region.startswith("ap-") else "Azure"
            if provider == "AWS" and not args.aws:
                continue
            print(f"Collecting {provider} {region}", flush=True)
            try:
                new_rows, pages = (aws if provider == "AWS" else azure)(client, region, private)
                rows.extend(new_rows)
                collection.append({"provider": provider, "region": region, "status": "OBSERVED" if new_rows else "NO_MATCHING_METERS", "rows": len(new_rows), "pages": pages})
            except (ValueError, KeyError, MachineError, httpx.HTTPError, OSError) as exc:
                collection.append({"provider": provider, "region": region, "status": "INCOMPLETE", "error_class": type(exc).__name__})
    report = build_report(rows, collection)
    versions = output / "versions"
    versions.mkdir(exist_ok=True)
    immutable = versions / (report["report_sha256"] + ".json")
    content = json.dumps(report, indent=2) + "\n"
    if immutable.exists():
        if immutable.is_symlink() or immutable.read_text() != content:
            raise MachineError("IMMUTABLE_RELEASE_COLLISION")
    else:
        with immutable.open("x") as destination:
            destination.write(content); destination.flush(); os.fsync(destination.fileno())
    (output / "atlas.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "brief.json").write_text(json.dumps(report["derived"], indent=2) + "\n")
    print(json.dumps({"report_sha256": report["report_sha256"], "coverage": report["derived"]["coverage"]}))


if __name__ == "__main__":
    main()
