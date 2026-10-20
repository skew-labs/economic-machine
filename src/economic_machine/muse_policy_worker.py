"""Bounded Muse control plane. It has no exchange endpoint or trading key path."""
import argparse
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.request

from .muse_maker import MODEL, canonical, context, digest, request, response, unique, validate_policy


def now_ms(): return time.time_ns()//1_000_000


def cost_limit(value):
    if str(value).strip().lower() == "unlimited": return None
    try: maximum = Decimal(str(value))
    except Exception: raise ValueError("model budget must be positive USD or unlimited") from None
    if not maximum.is_finite() or maximum <= 0:
        raise ValueError("model budget must be positive USD or unlimited")
    return maximum


def atomic(path, value):
    path = Path(path)
    raw = (canonical(value)+"\n").encode()
    fd, name = tempfile.mkstemp(prefix=".control-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, limit=131072):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("control file shape")
        raw = f.read(limit+1)
    if len(raw) > limit: raise ValueError("control file budget")
    return json.loads(raw, object_pairs_hook=unique)


def observer(root):
    root = Path(root)
    if root.is_file(): return read_json(root)
    files = list(root.glob("run-*/status.json"))
    if not files: raise ValueError("public observer unavailable")
    return read_json(max(files, key=lambda p: p.stat().st_mtime_ns))


class Budget:
    def __init__(self, path, session_id, deadline_ms, maximum_usd, max_calls):
        maximum = cost_limit(maximum_usd)
        if type(max_calls) is not int or not 1 <= max_calls <= 1000:
            raise ValueError("model budget range")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS scope(id INTEGER PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,request_hash TEXT UNIQUE,reserved TEXT,
        actual TEXT,status TEXT,response_hash TEXT,created_ms INTEGER);""")
        self.scope = dict(session_id=session_id, deadline_ms=deadline_ms, maximum_usd="unlimited" if maximum is None else str(maximum), max_calls=max_calls)
        body = canonical(self.scope)
        with self.db: self.db.execute("INSERT OR IGNORE INTO scope VALUES(1,?)", (body,))
        if self.db.execute("SELECT body FROM scope WHERE id=1").fetchone()[0] != body:
            raise ValueError("model budget/session changed")

    def totals(self):
        rows = list(self.db.execute("SELECT reserved,actual,status FROM calls"))
        used = sum((Decimal(a if a is not None else r) for r, a, _ in rows), Decimal(0))
        return {"calls_reserved": len(rows), "cost_committed_or_reserved_usd": str(used),
                "unconfirmed_cost_reservations": sum(a is None for _, a, _ in rows),
                "cost_breach": any(status == "cost_breach" for _, _, status in rows)}

    def reserve(self, req, now):
        body = canonical(req).encode()
        if len(body) > 16000 or req.get("model") != MODEL or req.get("max_tokens") != 1024:
            raise ValueError("model request bound")
        # UTF-8 bytes + framing is a conservative prompt-token bound. Prices are
        # capped in the provider route. Unknown outcome remains fully reserved.
        bound = (Decimal(len(body)+1024)*Decimal("0.0000001") + Decimal(1024)*Decimal("0.0000002"))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            totals = self.totals()
            maximum = cost_limit(self.scope["maximum_usd"])
            if (totals["cost_breach"] or now >= self.scope["deadline_ms"] or totals["calls_reserved"] >= self.scope["max_calls"]
                    or (maximum is not None and Decimal(totals["cost_committed_or_reserved_usd"])+bound > maximum)):
                raise ValueError("model budget or deadline exhausted")
            cur = self.db.execute("INSERT INTO calls(request_hash,reserved,status,created_ms) VALUES(?,?,?,?)",
                                  (hashlib.sha256(body).hexdigest(), str(bound), "reserved", now))
        return cur.lastrowid

    def finish(self, ident, raw, accepted):
        value = json.loads(raw, object_pairs_hook=unique)
        actual = None; breach = False
        cost = value.get("usage", {}).get("cost") if type(value.get("usage")) is dict else None
        if type(cost) in (str, int, float) and type(cost) is not bool:
            candidate = Decimal(str(cost))
            reserved = Decimal(self.db.execute("SELECT reserved FROM calls WHERE id=?", (ident,)).fetchone()[0])
            if not candidate.is_finite() or candidate < 0:
                raise ValueError("provider cost invalid")
            breach = candidate > reserved
            actual = str(candidate)
        with self.db:
            cur = self.db.execute("UPDATE calls SET actual=?,status=?,response_hash=? WHERE id=? AND status='reserved'",
                                 (actual, "cost_breach" if breach else ("valid" if accepted else "rejected"), hashlib.sha256(raw.encode()).hexdigest(), ident))
            if cur.rowcount != 1: raise ValueError("model call already completed")
        if breach: raise ValueError("provider cost exceeds reserved bound")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def provider_call(req, key_file):
    # Only the explicitly configured provider credential is read by an operator-
    # launched worker. Never use the venue credential, environment dumps or chat.
    fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077 or s.st_size > 512:
            raise ValueError("provider credential ownership/permissions")
        key = f.read().strip()
    if not key or "\n" in key or "\r" in key: raise ValueError("provider credential shape")
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=canonical(req).encode(),
        headers={"Authorization": "Bearer "+key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as r:
            raw = r.read(32769)
        if len(raw) > 32768: raise ValueError("provider response byte budget")
        return raw.decode()
    except urllib.error.HTTPError as exc:
        raise ValueError("provider HTTP "+str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("provider outcome unknown") from None


class ForwardGate:
    """Future input sanity, NOT a profitability or simulated-fill evaluator."""
    def __init__(self, issued):
        self.issued = issued; self.versions = set(); self.first = None; self.last = None

    def add(self, value, now):
        frame = context(value, now)
        quality = frame["quality"]
        stamp, version = quality["model_observed_ms"], quality["feature_version"]
        if not quality["research_features_available"] or type(version) is not int or type(stamp) is not int:
            return False
        if version <= 0 or stamp <= self.issued or version in self.versions or (self.last is not None and stamp <= self.last):
            return False
        fair = frame["features"]["fair_usdc"]
        if fair is None: return False
        self.versions.add(version); self.first = self.first or stamp; self.last = stamp
        return True

    def ready(self):
        return len(self.versions) >= 8 and self.last-self.first >= 1000


def packet(proposal, *, session_id, version, issued_ms, published_ms, deadline_ms, gate, kind="muse"):
    policy = validate_policy(proposal["policy"])
    expires = min(issued_ms+policy["ttl_ms"], deadline_ms)
    if not gate.ready() or not issued_ms <= published_ms < expires or gate.last > published_ms:
        raise ValueError("future gate or policy TTL not satisfied")
    if kind not in ("muse", "fixture"): raise ValueError("policy origin")
    return {"schema": "muse-policy-install-v1", "session_id": session_id, "version": version,
            "model": MODEL, "kind": kind, "issued_ms": issued_ms, "published_ms": published_ms,
            "expires_ms": expires, "policy": policy, "policy_sha256": digest(policy),
            "context_sha256": proposal["context_sha256"],
            "forward_gate": {"first_ms": gate.first, "last_ms": gate.last, "samples": len(gate.versions),
                             "kind": "future_input_sanity", "profitability_proven": False}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True); p.add_argument("--observer", type=Path, required=True)
    p.add_argument("--key-file", type=Path, required=True); p.add_argument("--budget-usd", required=True)
    p.add_argument("--max-calls", type=int, default=720)
    args = p.parse_args(); os.umask(0o077)
    control = args.root/"control"; control.mkdir(parents=True, exist_ok=True)
    lock = os.open(control/"worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lease = read_json(args.root/"session-start.json")
    budget = Budget(control/"model.sqlite", lease["session_id"], lease["deadline_ms"], args.budget_usd, args.max_calls)
    attempts = 0; responses = 0; accepted = 0
    try:
        while now_ms() < lease["deadline_ms"]:
            try:
                obs = observer(args.observer); stamp = now_ms()
                if not context(obs, stamp)["quality"]["research_features_available"]:
                    raise ValueError("fresh public input unavailable")
                req = request(obs, stamp)
                ident = budget.reserve(req, stamp); attempts += 1
                raw = provider_call(req, args.key_file); issued = now_ms(); responses += 1
                expected = digest(context(obs, stamp))
                try: proposal = response(raw, expected)
                except ValueError:
                    budget.finish(ident, raw, False); raise
                budget.finish(ident, raw, True)
                atomic(control/("response-"+str(ident)+".json"), json.loads(raw, object_pairs_hook=unique))
                gate = ForwardGate(issued)
                end = min(issued+proposal["policy"]["ttl_ms"]-1000, lease["deadline_ms"], issued+30000)
                while now_ms() < end and not gate.ready():
                    gate.add(observer(args.observer), now_ms()); time.sleep(0.2)
                install = packet(proposal, session_id=lease["session_id"], version=ident, issued_ms=issued,
                                 published_ms=now_ms(), deadline_ms=lease["deadline_ms"], gate=gate)
                atomic(control/"policy.json", install); accepted += 1
                atomic(control/"worker-status.json", {"status": "published", "version": ident, "accepted": accepted,
                    "actual_responses_this_process": responses, "calls_reserved_this_process": attempts,
                    "budget": budget.totals(), "orders_submitted": 0})
                # Request the successor while the original 60-second policy is
                # still fresh. Never renew/re-date a stale model response.
                delay = max(1, (install["expires_ms"]-now_ms())/1000-30)
            except (ValueError, OSError, sqlite3.Error) as exc:
                # Never include provider request headers/credential values or an
                # arbitrary provider error body in this status file.
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                atomic(control/"worker-status.json", {"status": "waiting_or_exhausted", "reason": reason[:120],
                    "budget": budget.totals(), "orders_submitted": 0})
                if "budget or deadline exhausted" in reason: break
                delay = 60
            until = min(now_ms()+int(delay*1000), lease["deadline_ms"])
            while now_ms() < until: time.sleep(max(0, min(1, (until-now_ms())/1000)))
    finally:
        atomic(control/"worker-final.json", {"budget": budget.totals(), "accepted_this_process": accepted,
                                           "actual_responses_this_process": responses,
                                           "deadline_ms": lease["deadline_ms"], "orders_submitted": 0})
        budget.db.close(); os.close(lock)


if __name__ == "__main__": main()
