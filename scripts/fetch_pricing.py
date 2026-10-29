"""Regenerate rightsize/data/pricing.json from the Cloud Billing Catalog API (Cloud Run SKUs).

    .venv/bin/python scripts/fetch_pricing.py --region us-central1
Uses Application Default Credentials; the project needs the Cloud Billing API enabled for quota."""

import argparse
import json
import subprocess
import urllib.request
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "rightsize" / "data" / "pricing.json"
SERVICE = "152E-C115-5142"  # Cloud Run


def price(sku) -> float:
    r = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
    return int(r.get("units", "0")) + r.get("nanos", 0) / 1e9


def main(region: str, project: str):
    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    skus, page = [], ""
    while True:
        url = (
            f"https://cloudbilling.googleapis.com/v1/services/{SERVICE}/skus?pageSize=500&currencyCode=USD"
            + (f"&pageToken={page}" if page else "")
        )
        d = json.load(
            urllib.request.urlopen(
                urllib.request.Request(
                    url, headers={"Authorization": f"Bearer {tok}", "x-goog-user-project": project}
                ),
                timeout=60,
            )
        )
        skus += d.get("skus", [])
        page = d.get("nextPageToken", "")
        if not page:
            break

    def find(desc_start: str, exact=False):
        for s in skus:
            if (
                region in s.get("serviceRegions", [])
                and (
                    s["description"] == desc_start
                    if exact
                    else s["description"].startswith(desc_start)
                )
                and "Tier 2" not in s["description"]
            ):
                return price(s)
        raise SystemExit(f"SKU not found for {region}: {desc_start}")

    req_sku = next(s for s in skus if s["description"] == "Requests")
    tiers = req_sku["pricingInfo"][0]["pricingExpression"]["tieredRates"]
    table = json.loads(OUT.read_text()) if OUT.exists() else {"regions": {}}
    table["source"] = f"Cloud Billing Catalog API, service {SERVICE} (Cloud Run), currency USD"
    table["retrieved"] = str(date.today())
    table["regions"][region] = {
        "request_based": {
            "cpu_per_vcpu_s": find("Services CPU (Request-based billing)", True),
            "mem_per_gib_s": find("Services Memory (Request-based billing)", True),
        },
        "instance_based": {
            "cpu_per_vcpu_s": find("Services CPU (Instance-based billing)", True),
            "mem_per_gib_s": find("Services Memory (Instance-based billing)", True),
        },
        "min_instance_idle": {
            "cpu_per_vcpu_s": find("Services Min Instance CPU (Request-based billing)", True),
            "mem_per_gib_s": find("Services Min Instance Memory (Request-based billing)", True),
        },
        "per_million_requests": round(price(req_sku) * 1e6, 4)
        or round(int(tiers[-1]["unitPrice"].get("nanos", 0)) / 1e9 * 1e6, 4),
        "free_requests_per_month": int(tiers[-1]["startUsageAmount"]),
    }
    OUT.write_text(json.dumps(table, indent=2) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--project", default="claude-code-507112")
    a = ap.parse_args()
    main(a.region, a.project)
