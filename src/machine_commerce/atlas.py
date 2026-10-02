"""Source-bound public compute observations and original derived reports.

Prices are public list prices, never evidence of inventory or transacted prices.
This module does not use Silicon Data content or infer permission to resell raw
provider archives. Unknown hardware and incomplete collection remain explicit.
"""

import json
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, require_keys

REGIONS = {
    "japaneast": ("Japan", "Tokyo"),
    "koreacentral": ("South Korea", "Seoul"),
    "southeastasia": ("Singapore", "Singapore"),
    "australiaeast": ("Australia", "Sydney"),
    "centralindia": ("India", "Pune"),
    "eastasia": ("Hong Kong", "Hong Kong"),
    "ap-northeast-1": ("Japan", "Tokyo"),
    "ap-northeast-2": ("South Korea", "Seoul"),
    "ap-southeast-1": ("Singapore", "Singapore"),
    "ap-southeast-2": ("Australia", "Sydney"),
}
HEX64 = re.compile(r"[0-9a-f]{64}")
PRODUCT_ID = "apac-compute-brief"
TERMS = {
    "schema": "datapass-terms-1",
    "product": PRODUCT_ID,
    "grant": "internal-use-original-derived-analysis",
    "duration_seconds": 86400,
    "transferable": True,
    "redistribute_upstream_archives": False,
    "provider_inventory_guarantee": False,
    "ownership_of_hardware": False,
    "copyright_attestation": "SUPPLIER_ATTESTATION_NOT_LEGAL_VERIFICATION",
}


def positive_price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MachineError("PRICE_NUMBER_REQUIRED")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise MachineError("INVALID_PRICE") from exc
    if not number.is_finite() or not Decimal(0) < number <= Decimal(1000000):
        raise MachineError("PRICE_OUT_OF_RANGE")
    if len(number.as_tuple().digits) > 30 or abs(number.as_tuple().exponent) > 18:
        raise MachineError("PRICE_PRECISION_OUT_OF_RANGE")
    return format(number, "f")


def azure_hardware(sku):
    # Only documented families/counts. New versions remain unclassified.
    patterns = [
        (r"Standard_NC(24|48|96)ads_A100_v4", "A100", {24: 1, 48: 2, 96: 4}),
        (r"Standard_NC(6|12|24)(?:r)?s_v3", "V100", {6: 1, 12: 2, 24: 4}),
        (r"Standard_NC(4|8|16|64)as_T4_v3", "T4", {4: 1, 8: 1, 16: 1, 64: 4}),
        (r"Standard_ND(96)isr_H100_v5", "H100", {96: 8}),
        (r"Standard_ND(96)amsr_A100_v4", "A100", {96: 8}),
    ]
    for pattern, model, counts in patterns:
        found = re.fullmatch(pattern, sku)
        if found:
            return model, counts[int(found.group(1))]
    return None, None


def observation(provider, region, sku, price, source, **extra):
    if not set(extra) <= {"gpu_model", "gpu_count", "operating_system", "product_name", "upstream_sku", "meter_id"}:
        raise MachineError("UNSUPPORTED_OBSERVATION_FIELD")
    if (region not in REGIONS or provider not in {"Azure", "AWS"}
            or (provider == "AWS") != region.startswith("ap-")):
        raise MachineError("SUPPORTED_APAC_REGION_REQUIRED")
    if not isinstance(sku, str) or not 1 <= len(sku) <= 160:
        raise MachineError("BOUNDED_SKU_REQUIRED")
    require_keys(source, {"url", "raw_sha256", "retrieved_at", "effective_at"}, "source evidence")
    if (not HEX64.fullmatch(source["raw_sha256"]) or type(source["retrieved_at"]) is not int
            or source["retrieved_at"] <= 0 or not isinstance(source["url"], str) or len(source["url"]) > 2048
            or (source["effective_at"] is not None and
                (not isinstance(source["effective_at"], str) or len(source["effective_at"]) > 200))):
        raise MachineError("SOURCE_EVIDENCE_REQUIRED")
    from urllib.parse import urlsplit
    parsed = urlsplit(source["url"])
    expected = "prices.azure.com" if provider == "Azure" else "pricing.us-east-1.amazonaws.com"
    if parsed.scheme != "https" or parsed.hostname != expected or parsed.username or parsed.password:
        raise MachineError("OFFICIAL_PRICE_SOURCE_REQUIRED")
    model, count = extra.pop("gpu_model", None), extra.pop("gpu_count", None)
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", model)):
        raise MachineError("BOUNDED_GPU_MODEL_REQUIRED")
    if (model is None) != (count is None):
        raise MachineError("MODEL_AND_COUNT_REQUIRED_TOGETHER")
    if count is not None and (type(count) is not int or not 1 <= count <= 32 or not model):
        raise MachineError("VERIFIED_HARDWARE_COUNT_REQUIRED")
    for value in extra.values():
        if not isinstance(value, str) or len(value) > 1000:
            raise MachineError("BOUNDED_OBSERVATION_LABEL_REQUIRED")
    if extra.get("operating_system", "Linux") != "Linux":
        raise MachineError("LINUX_COMPARABLE_PRICE_REQUIRED")
    rate = positive_price(price)
    with localcontext() as context:
        context.prec = 50
        per_gpu = format(Decimal(rate) / count, ".8f") if count else None
    body = {"provider": provider, "region": region, "country": REGIONS[region][0],
            "city": REGIONS[region][1], "sku": sku, "instance_usd_hour": rate,
            "gpu_usd_hour": per_gpu, "gpu_model": model, "gpu_count": count,
            "currency": "USD", "unit": "instance-hour", "offer_type": "PUBLIC_ON_DEMAND_LIST",
            "operating_system": extra.pop("operating_system", "Linux"),
            "source": source, "availability": "NOT_OBSERVED", **extra}
    return {"id": digest(body), **body}


def azure_rows(items, url, raw_sha256, retrieved_at):
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise MachineError("INVALID_AZURE_ROW")
        sku = item.get("armSkuName", "")
        name = item.get("productName", "")
        meter = item.get("meterName", "")
        if (item.get("serviceName") != "Virtual Machines" or item.get("type") != "Consumption"
                or item.get("currencyCode") != "USD" or item.get("unitOfMeasure") != "1 Hour"
                or item.get("armRegionName") not in REGIONS or not sku.startswith(("Standard_NC", "Standard_ND", "Standard_NV"))
                or "Windows" in name or any(w in meter for w in ("Spot", "Low Priority"))
                or item.get("tierMinimumUnits", 0) != 0 or item.get("isPrimaryMeterRegion") is False):
            continue
        model, count = azure_hardware(sku)
        result.append(observation("Azure", item["armRegionName"], sku, item["retailPrice"],
            {"url": url, "raw_sha256": raw_sha256, "retrieved_at": retrieved_at,
             "effective_at": item.get("effectiveStartDate")}, gpu_model=model, gpu_count=count,
            meter_id=item.get("meterId"), product_name=name))
    return result


def quantile(values, numerator, denominator):
    ordered = sorted(Decimal(positive_price(v)) for v in values)
    if not ordered:
        raise MachineError("QUANTILE_REQUIRES_OBSERVATIONS")
    with localcontext() as context:
        context.prec = 50
        position = Decimal(len(ordered) - 1) * numerator / denominator
        lo = int(position)
        hi = min(lo + 1, len(ordered) - 1)
        value = ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)
        return format(value, ".8f")


def build_report(rows, collection, now=None):
    now = int(time.time()) if now is None else now
    if type(now) is not int or now <= 0 or not isinstance(collection, list):
        raise MachineError("REPORT_CLOCK_AND_COLLECTION_REQUIRED")
    if not rows or len(rows) > 50000:
        raise MachineError("BOUNDED_NONEMPTY_REPORT_REQUIRED")
    by_id = {}
    for row in rows:
        fixed = {"id", "provider", "region", "country", "city", "sku", "instance_usd_hour", "gpu_usd_hour",
                 "gpu_model", "gpu_count", "currency", "unit", "offer_type", "operating_system", "source", "availability"}
        if not isinstance(row, dict) or not fixed <= set(row) or not set(row) <= fixed | {"product_name", "upstream_sku", "meter_id"}:
            raise MachineError("OBSERVATION_SCHEMA_MISMATCH")
        normalized = observation(row["provider"], row["region"], row["sku"], row["instance_usd_hour"], row["source"],
            gpu_model=row["gpu_model"], gpu_count=row["gpu_count"], operating_system=row["operating_system"],
            **{name: row[name] for name in ("product_name", "upstream_sku", "meter_id") if name in row})
        if canonical(normalized) != canonical(row):
            raise MachineError("OBSERVATION_NORMALIZATION_MISMATCH")
        base = {k: v for k, v in row.items() if k != "id"}
        if row["id"] != digest(base):
            raise MachineError("OBSERVATION_HASH_MISMATCH")
        if row["source"]["retrieved_at"] > now + 5:
            raise MachineError("FUTURE_OBSERVATION")
        by_id[row["id"]] = row
    rows = sorted(by_id.values(), key=lambda r: (r["provider"], r["region"], r["sku"], r["id"]))
    groups = defaultdict(list)
    for row in rows:
        if row["gpu_model"] is not None:
            groups[(row["provider"], row["region"], row["gpu_model"])].append(row)
    summaries = []
    for (provider, region, model), group in sorted(groups.items()):
        values = [r["gpu_usd_hour"] for r in group]
        summaries.append({"provider": provider, "region": region, "gpu_model": model,
            "count": len(group), "unit": "USD/GPU-hour", "p25": quantile(values, 1, 4),
            "median": quantile(values, 1, 2), "p75": quantile(values, 3, 4),
            "source_roots": sorted({r["source"]["raw_sha256"] for r in group}),
            "comparability": "NORMALIZED_GPU_COUNT_NOT_EQUAL_SYSTEM_PERFORMANCE"})
    derived = {"schema": "apac-compute-brief-1", "product_id": PRODUCT_ID,
               "as_of": now, "statistics": summaries,
               "source_observation_root": digest(rows), "collection": collection,
               "method": "Linear quantiles of public on-demand Linux list prices, normalized only for documented GPU counts.",
               "coverage": {"providers": sorted({r["provider"] for r in rows}),
                            "regions": sorted({r["region"] for r in rows}),
                            "rows": len(rows), "known_hardware_rows": sum(r["gpu_count"] is not None for r in rows)},
               "limitations": ["NO_CAPACITY_OBSERVATION", "NO_PRIVATE_DISCOUNTS", "NO_EXECUTED_PRICES", "NO_PERFORMANCE_EQUIVALENCE"]}
    report_hash = digest(derived)
    return {"schema": "atlas-public-release-1", "derived": derived, "report_sha256": report_hash,
            "observations": rows, "terms": TERMS, "terms_sha256": digest(TERMS),
            "sale_admission": "OWN_DERIVED_ANALYSIS_ONLY_RAW_ARCHIVES_NOT_FOR_RESALE",
            "tokenization": "CONTRACT_IMPLEMENTED_DEPLOYMENT_REQUIRES_INDEPENDENT_RECEIPT"}


def verify_report(report):
    require_keys(report, {"schema", "derived", "report_sha256", "observations", "terms", "terms_sha256",
                         "sale_admission", "tokenization"}, "Atlas release")
    if report["schema"] != "atlas-public-release-1" or report["terms"] != TERMS:
        raise MachineError("REPORT_SCHEMA_OR_TERMS_MISMATCH")
    rebuilt = build_report(report["observations"], report["derived"]["collection"], report["derived"]["as_of"])
    if canonical(rebuilt) != canonical(report):
        raise MachineError("REPORT_RECOMPUTATION_MISMATCH")
    return {"accepted": True, "report_sha256": report["report_sha256"],
            "rows": len(report["observations"]), "assurance": "INTEGRITY_AND_DERIVATION_NOT_SOURCE_TRUTH"}


def load_report(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000_000:
        raise MachineError("BOUNDED_RELEASE_FILE_REQUIRED")
    report = json.loads(path.read_text())
    verify_report(report)
    return report


def site_lens(raw):
    """Transparent scenario arithmetic, without invented climate/live inputs."""
    require_keys(raw, {"facility_mw", "pue", "network_fraction", "reserve_fraction",
                       "gpu_watts", "gpus_per_server", "server_overhead_watts",
                       "electricity_usd_kwh", "utilization", "gpu_usd_hour"}, "capacity scenario")
    values = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            raise MachineError("SCENARIO_NUMBERS_REQUIRED")
        try:
            values[key] = Decimal(str(value))
        except InvalidOperation as exc:
            raise MachineError("INVALID_SCENARIO_NUMBER") from exc
        if (not values[key].is_finite() or abs(values[key]) > 1_000_000
                or len(values[key].as_tuple().digits) > 30 or abs(values[key].as_tuple().exponent) > 18):
            raise MachineError("BOUNDED_SCENARIO_REQUIRED")
    v = values
    if not (0 < v["facility_mw"] <= 1000 and 1 <= v["pue"] <= 3
            and 0 <= v["network_fraction"] < 1 and 0 <= v["reserve_fraction"] < 1
            and 0 < v["gpu_watts"] <= 5000 and 1 <= v["gpus_per_server"] <= 32
            and v["gpus_per_server"] == int(v["gpus_per_server"])
            and 0 <= v["server_overhead_watts"] <= 10000 and 0 <= v["electricity_usd_kwh"] <= 10
            and 0 <= v["utilization"] <= 1 and v["gpu_usd_hour"] >= 0):
        raise MachineError("SCENARIO_RANGE_VIOLATION")
    with localcontext() as context:
        context.prec = 50
        facility_watts = v["facility_mw"] * 1_000_000
        it_watts = facility_watts / v["pue"]
        server_watts = v["gpu_watts"] * v["gpus_per_server"] + v["server_overhead_watts"]
        budget = it_watts * (1 - v["network_fraction"]) * (1 - v["reserve_fraction"])
        servers = int(budget // server_watts)
        gpus = servers * int(v["gpus_per_server"])
        used_it = Decimal(servers) * server_watts / (1 - v["network_fraction"])
        energy_kwh = used_it * v["pue"] / 1000 * 730
        revenue = gpus * v["gpu_usd_hour"] * v["utilization"] * 730
        power_cost = energy_kwh * v["electricity_usd_kwh"]
        full_revenue = gpus * v["gpu_usd_hour"] * 730
        return {"servers": servers, "gpus": gpus, "it_mw": format(it_watts / 1_000_000, ".4f"),
                "monthly_power_cost_usd": format(power_cost, ".2f"),
                "monthly_gross_revenue_usd": format(revenue, ".2f"),
                "monthly_revenue_less_power_usd": format(revenue - power_cost, ".2f"),
                "power_only_break_even_utilization": format(power_cost / full_revenue, ".6f") if full_revenue else None,
                "input_sha256": digest(raw), "assurance": "USER_INPUT_SCENARIO_NOT_SITE_UNDERWRITING",
                "excluded_costs": ["capex", "depreciation", "staff", "tax", "rent", "maintenance"],
                "climate_data_used": False, "capacity_availability_verified": False}
