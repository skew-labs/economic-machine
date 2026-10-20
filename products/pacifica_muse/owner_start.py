#!/usr/bin/env python3
"""Owner-operated live supervisor. This command is never run by assistant tools.

Explicit --live reads a delegated agent key from hidden terminal input or the
operator-selected existing encrypted credential into a sealed memory descriptor.
No plaintext trading key is written to a file or model.
The operator must separately stop the former writer; this tool never stops it.
"""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"src"))
from economic_machine.muse_policy_worker import cost_limit


def agent_credential(encrypted_file=None):
    if encrypted_file is None:
        secret = getpass.getpass("Delegated Pacifica agent key (hidden; RAM only): ")
    else:
        # Executed only by the human-run launcher. Never print or persist the
        # decrypted output; do not restart the former financial service.
        command = ([] if os.geteuid() == 0 else ["sudo", "-n"]) + [
            "systemd-creds", "decrypt", "--name=agent.key", str(Path(encrypted_file).absolute()), "-"]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if result.returncode:
            raise ValueError("operator could not unlock existing encrypted agent credential")
        if len(result.stdout) > 101: raise ValueError("delegated agent credential size")
        secret = result.stdout.decode("ascii").strip()
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not 80 <= len(secret) <= 100 or any(c not in alphabet for c in secret):
        raise ValueError("delegated agent key encoding invalid")
    return secret


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", required=True)
    p.add_argument("--binary", type=Path, required=True); p.add_argument("--root", type=Path, required=True)
    p.add_argument("--observer", type=Path, required=True); p.add_argument("--model-key-file", type=Path, required=True)
    p.add_argument("--model-budget-usd", required=True)
    p.add_argument("--agent-encrypted-file", type=Path, help="Existing systemd encrypted delegated credential; unlocked only when the operator runs this launcher")
    p.add_argument("--ipc-socket", default="/dev/shm/ticking-sol-mm/model.sock")
    args = p.parse_args()
    if sys.platform != "linux" or not sys.stdin.isatty(): p.error("operator Linux TTY required")
    try: cost_limit(args.model_budget_usd)
    except ValueError as exc: p.error(str(exc))
    if not args.binary.is_file(): p.error("native binary file missing")
    if not args.observer.exists(): p.error("public observer directory missing")
    if not args.model_key_file.is_file():
        p.error("--model-key-file requires an existing file path, not the API key itself")
    state = subprocess.run(["systemctl", "show", "pacifica-ticking-agent.service", "-p", "LoadState", "-p", "ActiveState"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    if "LoadState=loaded" not in state or "ActiveState=inactive" not in state:
        p.error("previous trading writer is not inactive; it has not been stopped by this launcher")
    os.umask(0o077); resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Prevent ptrace/core access to this supervising process while it holds the
    # operator-provided credential. Children also set PR_SET_DUMPABLE=0.
    import ctypes
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0: p.error("cannot disable process dumps")
    root = args.root.resolve(); root.mkdir(parents=True, exist_ok=True)
    if "muse" not in root.name or "observe" in root.name: p.error("dedicated new Muse live directory required")
    (root/"control").mkdir(exist_ok=True)
    lock = os.open(root/"supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root/"session-start.json").exists():
        meta = json.loads((root/"session-start.json").read_text())
        if meta["live"] is not True or time.time()*1000 >= meta["deadline_ms"]:
            p.error("the original session is expired or is an observation session")
    try: secret = agent_credential(args.agent_encrypted_file)
    except (ValueError, OSError, subprocess.TimeoutExpired): p.error("operator agent credential unavailable or invalid; no native trader started")
    fd = os.memfd_create("muse-agent-credential", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    raw = bytearray(secret.encode()); del secret
    os.write(fd, raw); raw[:] = b"\x00"*len(raw); del raw
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    source = Path(__file__).resolve().parents[2]/"src"
    env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    stopping = False; native = None; worker = None
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if native and native.poll() is None: native.terminate()
        if worker and worker.poll() is None: worker.terminate()
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    try:
        with (root/"operator-native.log").open("a") as out, (root/"operator-model.log").open("a") as model_out:
            for restart in range(4):
                if stopping: break
                os.lseek(fd, 0, os.SEEK_SET)
                native = subprocess.Popen([str(args.binary.resolve()), "--live", str(root), str(root/"control"), str(fd), args.ipc_socket],
                                          pass_fds=(fd,), stdout=out, stderr=out, env=env)
                for _ in range(200):
                    if (root/"session-start.json").exists() or native.poll() is not None or stopping: break
                    time.sleep(0.1)
                if native.poll() is None and worker is None and not stopping:
                    worker = subprocess.Popen([sys.executable, "-m", "economic_machine.muse_policy_worker", "--root", str(root),
                        "--observer", str(args.observer.resolve()), "--key-file", str(args.model_key_file.resolve()),
                        "--budget-usd", args.model_budget_usd], stdout=model_out, stderr=model_out, env=env)
                code = native.wait()
                if stopping or code == 0: break
                # Reuse only this journal/deadline. Never reset state on failure.
                if not (root/"session-start.json").exists(): break
                meta = json.loads((root/"session-start.json").read_text())
                if time.time()*1000 >= meta["deadline_ms"]: break
                time.sleep(2*(restart+1))
            print("Native process ended; inspect summary.json/failure.json and reconcile residual inventory.")
    finally:
        if native and native.poll() is None: native.terminate(); native.wait(timeout=60)
        if worker and worker.poll() is None: worker.terminate(); worker.wait(timeout=60)
        os.close(fd); os.close(lock)


if __name__ == "__main__": main()
