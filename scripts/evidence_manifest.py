"""Generate a source/evidence inventory on the authorized remote compute host."""

import hashlib
import json
import platform
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def record(path):
    content = path.read_bytes()
    lines = content.decode().splitlines()
    return {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(content).hexdigest(),
            "physical_lines": len(lines), "nonblank_noncomment_lines": sum(
                bool(line.strip()) and not line.lstrip().startswith(("#", "//")) for line in lines)}


def main():
    paths = sorted([*ROOT.glob("src/**/*.py"), *ROOT.glob("contracts/*.sol"), *ROOT.glob("tests/*.py"),
                    *ROOT.glob("scripts/*.py"), *ROOT.glob("web/*"), *ROOT.glob("docs/*.md"),
                    *ROOT.glob("deploy/*"), ROOT / "README.md", ROOT / "pyproject.toml", ROOT / "requirements-verified.txt"])
    files = [record(path) for path in paths if path.is_file()]
    groups = {}
    for name, prefix in [("core_and_contracts", ("src/", "contracts/")), ("tests", ("tests/",)),
                         ("scripts", ("scripts/",)), ("web", ("web/",))]:
        group = [item for item in files if item["path"].startswith(prefix)]
        groups[name] = {key: sum(item[key] for item in group) for key in ["physical_lines", "nonblank_noncomment_lines"]}
    evidence = {}
    passed = set()
    current_passed = set()
    current_logs = {"payments-tests-final.log", "operations-tests-final.log", "market-tests-x402.log",
        "core-tests-x402.log", "access-tests-x402.log", "api-tests-x402.log", "admission-tests-x402.log", "recovery-readiness.log"}
    for filename in ["python-tests.log", "market-tests-final.log", "api-tests-final.log",
                     "access-tests-final.log", "console-api-tests-final.log", "contract-build.json",
                     "agent-purchases.json", "bilateral-agents.json", "console-agent.json",
                     "console-browser.json", "payments-tests-final.log", "operations-tests-final.log",
                     "market-tests-x402.log", "core-tests-x402.log", "access-tests-x402.log", "api-tests-x402.log",
                     "admission-tests-x402.log", "payment-lint.log", "console-x402-syntax.log", "eip3009-build.json",
                     "comparison.json", "comparison-before.json", "x402-evm.json", "live-chain-read.json",
                     "production-preflight.json", "payments-browser.json", "runtime-versions.json",
                     "payment-limits.png", "payments-mobile.png", "recovery-readiness.log", "service-health-x402.json"]:
        path = ROOT / "artifacts" / filename
        if path.exists():
            evidence[filename] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            if filename.endswith(".log"):
                for match in re.finditer(r"^test_\w+ \(([^)]+)\) \.\.\. ok$", path.read_text(), re.MULTILINE):
                    passed.add(match.group(1))
                    if filename in current_logs:
                        current_passed.add(match.group(1))
    manifest = {"schema_version": "machine-commerce-verification-1", "created_at": int(time.time()),
                "remote_host": platform.node(), "remote_path": str(ROOT), "files": files, "line_counts": groups,
                "line_count_definition": "Physical and nonblank/noncomment lines; no claim all lines are original",
                "evidence": evidence, "passed_test_cases": sorted(passed), "passed_test_count": len(passed),
                "current_passed_test_cases": sorted(current_passed), "current_passed_test_count": len(current_passed),
                "test_matrix_note": "Current changed runtime/admission/auth/API/market/core checks plus prior unchanged escrow evidence. Only the new EIP3009 test token was compiled.",
                "source_reuse": [item for item in files if item["path"] in
                    {"src/economic_machine/values.py", "src/economic_machine/journal.py"}],
                "service_status": subprocess.check_output(["systemctl", "is-active", "machine-commerce.service"], text=True).strip(),
                "real_payment": "X402_RUNTIME_IMPLEMENTED_EXTERNAL_RESOURCE_NOT_CONFIGURED",
                "contract_deployment": "PY_EVM_TEST_ONLY"}
    (ROOT / "artifacts/verification.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"line_counts": groups, "evidence_files": list(evidence), "service_status": manifest["service_status"]}, indent=2))


if __name__ == "__main__":
    main()
