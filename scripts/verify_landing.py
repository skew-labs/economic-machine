"""Bounded static-asset and live-link checks, run on the Canadian host."""
import hashlib
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://machine.148-113-153-116.nip.io/commerce/"


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.ids = []
        self.launch = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for name in ["src", "href"]:
            if attrs.get(name):
                self.refs.append(attrs[name])
        if tag == "a" and "launch" in attrs.get("class", "").split():
            self.launch.append(attrs.get("href"))


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Verification runs on the remote host")
    parser = References()
    parser.feed((ROOT / "site/index.html").read_text())
    if len(parser.ids) != len(set(parser.ids)):
        raise RuntimeError("duplicate landing IDs")
    local = sorted({urlsplit(ref).path for ref in parser.refs if not urlsplit(ref).scheme and urlsplit(ref).path})
    for name in local:
        if not (ROOT / "site" / name).is_file():
            raise RuntimeError("missing landing reference: " + name)
    if parser.launch != [BASE + "console"] * 2:
        raise RuntimeError("launch actions must open the existing console")
    css = (ROOT / "site/landing.css").read_text()
    if "prefers-reduced-motion:reduce" not in css:
        raise RuntimeError("reduced motion fallback missing")
    checks = []
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for name in ["landing.css", "landing.js"]:
            response = client.get(BASE + name)
            response.raise_for_status()
            if response.content != (ROOT / "site" / name).read_bytes():
                raise RuntimeError("deployed landing source differs: " + name)
            checks.append({"path": name, "status": response.status_code, "sha256": hashlib.sha256(response.content).hexdigest()})
        for name in ["", "atlas.html", "site-lens.html", "data-pass.html", "evidence.html", "console", "assets/nvidia-logo.svg"]:
            response = client.get(BASE + name)
            response.raise_for_status()
            checks.append({"path": name, "status": response.status_code})
    result = {"accepted": True, "checked_at": datetime.now(UTC).isoformat(), "local_references": len(local),
              "checks": checks, "scope": "STATIC_ASSETS_AND_READ_ONLY_ROUTES", "customer_actions": 0}
    (ROOT / "artifacts/landing-20261003/http-check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
