"""Remote, public readbacks of the new calculation API and deployed code.

All calculations use labelled synthetic assumptions. No account credentials,
signature, payment, order submission or release registration is performed.
"""

import copy
import hashlib
import json
import time
from pathlib import Path

import httpx

from economic_machine.values import digest
from machine_commerce.datapass import DataPassChain, call_data
from machine_engine.economic_examples import strategy_example

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://machine.148-113-153-116.nip.io/commerce"


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Live validation uses the authorized remote host")
    build = json.loads((ROOT / "artifacts/atlas-release/economics-build.json").read_text())
    proof = json.loads((ROOT / "artifacts/arbitrum-sepolia/datapass-deployment.json").read_text())
    observations = []
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        catalogue = client.get(ORIGIN + "/demo/economics")
        catalogue.raise_for_status()
        catalog = catalogue.json()
        if (not catalog["enabled"] or catalog["library_sha256"] != build["library_sha256"]
                or len(catalog["operations"]) != 20 or catalog["execution_authority"] != "NONE"):
            raise RuntimeError("Deployed native library does not match the tested artifact")
        baseline = strategy_example()
        changed = copy.deepcopy(baseline)
        changed["input"]["frame"]["dependencies"]["facts"][0]["value"] = 200000000
        for scenario, expected in ((baseline, "REDUCE"), (changed, "HOLD")):
            response = client.post(ORIGIN + "/demo/economics/evaluate", json=scenario)
            response.raise_for_status()
            result = response.json()
            if (result["receipt_sha256"] != digest({key: value for key, value in result.items() if key != "receipt_sha256"})
                    or result["action"] != expected or result["execution_authority"] != "NONE"
                    or result["language_model_calls"] != 0 or result["library_sha256"] != build["library_sha256"]
                    or result["demonstration"] != "SYNTHETIC_ASSUMPTIONS_NOT_CUSTOMER_ACCOUNT"):
                raise RuntimeError("Public calculation differs from independently declared counterfactual")
            observations.append({"request": scenario, "response": result, "http_status": response.status_code})
        public = client.get(ORIGIN + "/demo/datapass/deployment")
        public.raise_for_status()
        if public.json() != proof:
            raise RuntimeError("Public deployment proof differs from retained RPC observations")
        product = client.get(ORIGIN + "/demo/datapass")
        product.raise_for_status()
        if product.json()["datapass"]["contract"] != proof["contract_address"]:
            raise RuntimeError("Console dataset catalogue is not connected to the deployed contract")
        protected = client.get(ORIGIN + "/api/engine/economics")
        if protected.status_code not in {401, 403}:
            raise RuntimeError("Workspace authorization boundary unverified; HTTP " + str(protected.status_code))
    code_hash = proof["observations"][0]["runtime_sha256"]
    chain = DataPassChain(proof["contract_address"], code_hash)
    owner, chain_evidence = chain.read(call_data("owner()", [], []))
    if len(owner) != 32 or owner[:12] != bytes(12):
        raise RuntimeError("Contract owner ABI is invalid")
    observed_owner = "0x" + owner[-20:].hex()
    if observed_owner.lower() != proof["observations"][0]["owner"].lower():
        raise RuntimeError("Deployed contract owner differs")
    for path, checksum in build["source_sha256"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Native source changed after verification: " + path)
    body = {"schema": "economic-live-verification-1", "checked_at": int(time.time()), "origin": ORIGIN,
        "library_sha256": build["library_sha256"], "public_catalogue": catalog,
        "synthetic_counterfactuals": observations, "deployment_proof_sha256": proof["proof_sha256"],
        "live_read_only_contract_owner": observed_owner, "finalized_chain_read": chain_evidence,
        "workspace_without_auth_http_status": protected.status_code, "customer_orders": 0,
        "new_datapass_purchases": 0, "scope": "DEPLOYED_CALCULATION_API_AND_READ_ONLY_TESTNET_CODE_NOT_CALIBRATED_INVESTMENT_RESULTS"}
    record = body | {"evidence_sha256": digest(body)}
    (ROOT / "artifacts/atlas-release/economics-live-check.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"passed": True, "operations": len(catalog["operations"]),
        "counterfactuals": [row["response"]["action"] for row in observations],
        "contract": proof["contract_address"], "evidence_sha256": record["evidence_sha256"]}))


if __name__ == "__main__":
    main()
