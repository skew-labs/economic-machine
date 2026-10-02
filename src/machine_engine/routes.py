"""Shared console operations; hosted owner isolation is supplied by a dependency."""

from fastapi import APIRouter, Depends

from economic_machine.compiler import compile_program
from economic_machine.values import require_keys


def engine_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter(prefix="/api/engine")
    owner = [Depends(require_owner)] if require_owner else []

    @router.get("/overview")
    def overview(work=Depends(workspace_dependency)):
        return work.overview()

    @router.get("/profiles")
    def profiles(work=Depends(workspace_dependency)):
        from .connections import PROFILES

        return {"profiles": PROFILES, "credential_namespace": work.credential_prefix}

    @router.post("/connections", dependencies=owner)
    def connect(raw: dict, work=Depends(workspace_dependency)):
        return work.connect(raw)

    @router.post("/connections/{cid}/sync")
    def sync(cid: str, work=Depends(workspace_dependency)):
        return work.sync(cid)

    @router.post("/connections/{cid}/schedule", dependencies=owner)
    def schedule(cid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"enabled", "interval_seconds"}, "sync schedule")
        return work.scheduler.configure(cid, **raw)

    @router.post("/connections/{cid}/disconnect", dependencies=owner)
    def disconnect(cid: str, work=Depends(workspace_dependency)):
        return work.disconnect(cid)

    @router.post("/usage")
    def usage(raw: dict, work=Depends(workspace_dependency)):
        return work.record_usage(raw)

    @router.post("/programs/compile")
    def compile_only(raw: dict, work=Depends(workspace_dependency)):
        return {"program": compile_program(raw), "execution_authority": "NONE"}

    @router.post("/programs", dependencies=owner)
    def register(raw: dict, work=Depends(workspace_dependency)):
        return work.runtime.register_program(raw)

    @router.post("/state", dependencies=owner)
    def install_state(raw: dict, work=Depends(workspace_dependency)):
        return work.runtime.install_state(raw)

    @router.post("/deltas", dependencies=owner)
    def delta(raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"event_id", "delta"}, "state event")
        return work.runtime.ingest(raw["delta"], event_id=raw["event_id"])

    @router.post("/programs/{pid}/{operation}", dependencies=owner)
    def control(pid: str, operation: str, raw: dict, work=Depends(workspace_dependency)):
        from economic_machine.values import MachineError

        if operation == "evaluate":
            require_keys(raw, {"at"}, "evaluation")
            return work.runtime.evaluate(pid, at=raw["at"])
        if operation in {"pause", "resume"}:
            require_keys(raw, {"reason"}, "program control")
            method = work.runtime.pause_program if operation == "pause" else work.runtime.resume_program
            return method(pid, reason=raw["reason"])
        raise MachineError("SUPPORTED_PROGRAM_OPERATION_REQUIRED")

    @router.post("/trade/policies", dependencies=owner)
    def policy(raw: dict, work=Depends(workspace_dependency)):
        return work.trading.policy(raw)

    @router.post("/trade/policies/{pid}/pause", dependencies=owner)
    def pause(pid: str, work=Depends(workspace_dependency)):
        return work.trading.pause(pid)

    @router.post("/trade/orders")
    def plan(raw: dict, work=Depends(workspace_dependency)):
        return work.trading.plan(raw)

    @router.get("/trade/orders/{oid}")
    def get_order(oid: str, work=Depends(workspace_dependency)):
        return work.trading.order(oid)

    @router.post("/trade/orders/{oid}/approve", dependencies=owner)
    def approve(oid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"plan_hash"}, "owner approval")
        return work.trading.approve(oid, raw["plan_hash"])

    @router.post("/trade/orders/{oid}/dispatch", dependencies=owner)
    def dispatch(oid: str, work=Depends(workspace_dependency)):
        return work.trading.dispatch(oid)

    @router.post("/trade/orders/{oid}/reconcile")
    def reconcile(oid: str, work=Depends(workspace_dependency)):
        return work.trading.reconcile(oid)

    @router.post("/trade/orders/{oid}/cancel", dependencies=owner)
    def cancel(oid: str, work=Depends(workspace_dependency)):
        return work.trading.cancel(oid)

    return router


class HostedWorkspaces:
    """Stable wallet-owned storage, without exposing server/operator environment keys."""

    def __init__(self, root, clock):
        import os
        from pathlib import Path

        self.root, self.clock = Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.last_error = None

    def get(self, subject):
        import hashlib

        from .workspace import Workspace

        key = hashlib.sha256(subject.encode()).hexdigest()
        path = self.root / (key + ".sqlite3")
        if not path.exists() and len(list(self.root.glob("*.sqlite3"))) >= 256:
            from economic_machine.values import MachineError

            raise MachineError("HOSTED_WORKSPACE_CAPACITY_REACHED")
        return Workspace(path, clock=self.clock, credential_prefix="ENGINE_" + key[:20].upper() + "_")

    def cycle(self):
        from .workspace import Workspace

        # Bound each pass. No unbounded tasks or implicit financial dispatch.
        count = 0
        for path in sorted(self.root.glob("*.sqlite3"))[:256]:
            work = Workspace(
                path, clock=self.clock, credential_prefix="ENGINE_" + path.stem[:20].upper() + "_"
            )
            work.scheduler.run_once()
            for oid in work.trading.pending_ids(limit=1):
                work.trading.reconcile(oid)
            count += 1
        return count

    async def loop(self):
        import asyncio

        while True:
            try:
                await asyncio.to_thread(self.cycle)
                self.last_error = None
            except Exception:  # noqa: BLE001 - retain durable jobs and retry on next cycle.
                self.last_error = "ENGINE_SYNC_WORKER_DEGRADED"
            await asyncio.sleep(5)
