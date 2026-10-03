"""Resumable, bounded remote qualification. Completed duration is never inferred."""

import argparse
import ctypes as c
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from economic_machine.values import MachineError, digest
from machine_engine.solution import Edge, graph
from machine_engine.solution_operations import MAGIC, Frame, Result

ROOT = Path(__file__).resolve().parents[1]


class Checkpoint:
    def __init__(self, path, binding):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise MachineError("QUALIFICATION_PATH")
        self.lock = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            raise MachineError("QUALIFICATION_ALREADY_RUNNING") from None
        try:
            self.initialize(binding)
        except Exception:
            os.close(self.lock)
            raise

    def initialize(self, binding):
        if self.path.exists():
            if self.path.stat().st_size > 65536:
                raise MachineError("QUALIFICATION_CHECKPOINT_BOUND")
            raw = json.loads(self.path.read_bytes())
            fingerprint = raw.pop("checkpoint_sha256")
            if digest(raw) != fingerprint or raw["binding"] != binding:
                raise MachineError("QUALIFICATION_CHECKPOINT_BINDING")
            self.state = raw
            if raw["state"] == "FAILED":
                raise MachineError("QUALIFICATION_FAILED_REQUIRES_NEW_RUN")
            self.state["resumes"] += 1
        else:
            self.state = {
                "schema": "solution-qualification-1",
                "binding": binding,
                "state": "RUNNING",
                "verified_elapsed_ns": 0,
                "jobs": 0,
                "batches": 0,
                "injected_worker_terminations": 0,
                "resumes": 0,
                "score_or_frame_failures": 0,
                "peak_child_rss_kib": 0,
                "round_trip_buckets_ns": {},
                "native_buckets_ns": {},
                "maximum_round_trip_ns": 0,
                "maximum_native_ns": 0,
                "paid_api_calls": 0,
                "chain_transactions": 0,
            }
        if self.state["state"] != "COMPLETE":
            self.state["state"] = "RUNNING"
        self.save()

    def save(self):
        body = dict(self.state)
        body["checkpoint_sha256"] = digest(body)
        staging = Path(str(self.path) + ".staged")
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(body, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, self.path)
        parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def close(self):
        os.close(self.lock)


def record(histogram, value):
    # Fixed-size power-of-two histogram; percentile upper bounds, not exact quantiles.
    bucket = str(1 << max(0, (value - 1).bit_length()))
    histogram[bucket] = histogram.get(bucket, 0) + 1


def percentile(histogram, percent):
    total = sum(histogram.values())
    target = (total * percent + 99) // 100
    seen = 0
    for bucket, count in sorted(histogram.items(), key=lambda row: int(row[0])):
        seen += count
        if seen >= target:
            return int(bucket)
    return 0


def fixtures():
    rows = []
    edges = []
    for index in range(16):
        items = graph(12345, index)
        edges.append(items)
        frame = Frame()
        frame.magic = MAGIC
        frame.version = 1
        frame.id = index
        frame.graph.nodes = 32
        frame.graph.count = len(items)
        frame.seed = index + 42
        frame.budget = 100000
        frame.algorithm = 1
        for at, item in enumerate(items):
            frame.graph.edges[at] = Edge(*item)
        rows.append(bytes(frame))
    return b"".join(rows), edges


def verify(raw, edges):
    size = c.sizeof(Result)
    if len(raw) != len(edges) * size:
        raise MachineError("QUALIFICATION_FRAME_COUNT")
    timings = []
    for index, items in enumerate(edges):
        result = Result.from_buffer_copy(raw[index * size : (index + 1) * size])
        bits = result.answer.bits
        score = sum(w for u, v, w in items if ((bits >> u) ^ (bits >> v)) & 1)
        if (
            result.id != index
            or result.status
            or result.reserved
            or result.magic != MAGIC
            or result.version != 1
            or not result.answer.valid
            or result.answer.complete
            or bits & 1
            or bits >= 2**32
            or result.answer.score != score
            or result.answer.edge_visits > 100000
        ):
            raise MachineError("QUALIFICATION_SCORE_OR_ORDER")
        timings.append(result.elapsed_ns)
    return timings


def qualify(executable, expected_sha, path, seconds, *, stop=lambda: False, inject_every=1000, interval=0.05):
    executable = Path(executable)
    if (
        executable.is_symlink()
        or not executable.is_absolute()
        or not executable.is_file()
        or executable.stat().st_size > 10000000
        or hashlib.sha256(executable.read_bytes()).hexdigest() != expected_sha
    ):
        raise MachineError("QUALIFICATION_EXECUTABLE_PIN")
    if (
        type(seconds) is not int
        or not 10 <= seconds <= 604800
        or type(inject_every) is not int
        or not 0 <= inject_every <= 100000
        or not 0.01 <= interval <= 1
    ):
        raise MachineError("QUALIFICATION_BOUNDS")
    payload, edges = fixtures()
    binding = {
        "qualifier_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pipeline_sha256": expected_sha,
        "fixture_sha256": hashlib.sha256(payload).hexdigest(),
        "requested_seconds": seconds,
        "fault_injection_every_batches": inject_every,
        "interval_milliseconds": int(interval * 1000),
    }
    checkpoint = Checkpoint(path, binding)
    state = checkpoint.state
    segment = time.monotonic_ns()
    base = state["verified_elapsed_ns"]
    last_saved = segment
    try:
        while state["verified_elapsed_ns"] < seconds * 10**9 and not stop():
            if time.monotonic_ns() - last_saved >= 10**9:
                if shutil.disk_usage(Path(path).parent).free < 5 * 1024**3:
                    raise MachineError("QUALIFICATION_DISK_RESERVE")
                checkpoint.save()
                last_saved = time.monotonic_ns()
            if inject_every and state["batches"] and state["batches"] % inject_every == 0:
                # Kill a real worker waiting for input; no partial result is counted.
                child = subprocess.Popen(
                    [str(executable)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={"PATH": "/usr/bin:/bin"},
                    start_new_session=True,
                )
                try:
                    child.kill()
                    child.communicate(timeout=5)
                    if child.returncode == 0:
                        raise MachineError("QUALIFICATION_KILL_NOT_OBSERVED")
                    state["injected_worker_terminations"] += 1
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
            started = time.perf_counter_ns()
            result = subprocess.run(
                [str(executable)],
                input=payload,
                capture_output=True,
                timeout=10,
                check=False,
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
            )
            elapsed = time.perf_counter_ns() - started
            if result.returncode:
                if stop():
                    # systemd may signal both parent and its active child. A
                    # requested pause discards that batch and remains resumable.
                    break
                raise MachineError("QUALIFICATION_WORKER_FAILED")
            samples = verify(result.stdout, edges)
            record(state["round_trip_buckets_ns"], elapsed)
            for sample in samples:
                record(state["native_buckets_ns"], sample)
            state["maximum_round_trip_ns"] = max(state["maximum_round_trip_ns"], elapsed)
            state["maximum_native_ns"] = max(state["maximum_native_ns"], max(samples))
            state["jobs"] += 16
            state["batches"] += 1
            time.sleep(interval)
            # Only completed live segments count; downtime and uncheckpointed crash work do not.
            state["verified_elapsed_ns"] = base + time.monotonic_ns() - segment
        state["state"] = "COMPLETE" if state["verified_elapsed_ns"] >= seconds * 10**9 else "PAUSED"
    except Exception as error:
        state["state"] = "FAILED"
        state["score_or_frame_failures"] += 1
        state["failure_type"] = type(error).__name__
        raise
    finally:
        import resource

        state["peak_child_rss_kib"] = max(
            state["peak_child_rss_kib"], resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        )
        state["p99_round_trip_upper_bound_ns"] = percentile(state["round_trip_buckets_ns"], 99)
        state["p99_native_upper_bound_ns"] = percentile(state["native_buckets_ns"], 99)
        checkpoint.save()
        checkpoint.close()
    return state


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote qualification only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--inject-every", type=int, default=1000)
    args = parser.parse_args()
    build = json.loads((ROOT / "artifacts/solution-operations/native-build.json").read_bytes())
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    report = qualify(
        build["executable"],
        build["sha256"],
        args.checkpoint,
        args.seconds,
        stop=lambda: stopped,
        inject_every=args.inject_every,
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
