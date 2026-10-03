"""Remote implementation/test audit. No generated data or dependency counts."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def records(folder, suffixes, exclude=()):
    result = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes or any(item in path.parts for item in exclude):
            continue
        lines = path.read_text().splitlines()
        result.append({"path": str(path.relative_to(ROOT)), "physical": len(lines),
                       "nonblank": sum(bool(line.strip()) for line in lines)})
    return result


def total(rows):
    return {"files": len(rows), "physical": sum(row["physical"] for row in rows),
            "nonblank": sum(row["nonblank"] for row in rows)}


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Audit runs on the remote host")
    implementation = records(ROOT / "src", {".py"}, ("__pycache__",))
    implementation += records(ROOT / "web", {".js", ".html", ".css"})
    implementation += records(ROOT / "native", {".hpp", ".cpp"}, ("tests",))
    contracts = records(ROOT / "contracts", {".sol"})
    fixtures = {"TestCommerceToken.sol", "TestEIP3009Token.sol", "DataPassAdversaries.sol", "MiningAdversary.sol", "SolutionVRFMock.sol"}
    implementation += [row for row in contracts if Path(row["path"]).name not in fixtures]
    tests = records(ROOT / "tests", {".py", ".cjs"}, ("fixtures", "__pycache__"))
    tests += records(ROOT / "native/tests", {".cpp"})
    tests += [row for row in contracts if Path(row["path"]).name in fixtures]
    tools = records(ROOT / "scripts", {".py", ".mjs", ".sh"})
    result = {"schema": "machine-line-audit-1", "engine_implementation": total(implementation),
              "native_cpp_implementation": total([row for row in implementation if row["path"].startswith("native/")]),
              "tests": total(tests), "operational_scripts": total(tools),
              "implementation_files": implementation, "test_files": tests,
              "counted_as_engine": ["src", "web", "native implementation", "production contracts"],
              "excluded": ["data", "JSON", "evidence", "dependencies", "logs", "binaries", "docs", "landing site", "generated tables"],
              "target_30000_engine_nonblank_reached": total(implementation)["nonblank"] >= 30000}
    (ROOT / "artifacts/atlas-release/line-count.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ["engine_implementation", "tests", "operational_scripts", "target_30000_engine_nonblank_reached"]}))


if __name__ == "__main__":
    main()
