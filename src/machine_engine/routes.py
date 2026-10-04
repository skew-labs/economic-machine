"""Shared console operations; hosted owner isolation is supplied by a dependency."""

from fastapi import APIRouter, Depends

from economic_machine.compiler import compile_program
from economic_machine.values import require_keys


def engine_routes(workspace_dependency, *, require_owner=None):
    from .task_routes import task_routes

    router = APIRouter(prefix="/api/engine")
    from .assistant_routes import assistant_routes
    router.include_router(assistant_routes(workspace_dependency, require_owner=require_owner))
    router.include_router(task_routes(workspace_dependency, require_owner=require_owner))
    from .fuel_routes import fuel_routes
    router.include_router(fuel_routes(workspace_dependency, require_owner=require_owner))
    from .mining_routes import mining_routes
    router.include_router(mining_routes(workspace_dependency, require_owner=require_owner))
    owner = [Depends(require_owner)] if require_owner else []

    @router.get("/control")
    def control_status(work=Depends(workspace_dependency)):
        return work.control.status()

    @router.get("/live")
    def live_status(work=Depends(workspace_dependency)):
        return work.live.status()

    @router.post("/trade/orders/{oid}/readback", dependencies=owner)
    def trade_readback(oid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "post-trade account readback")
        return work.trading.readback(oid)

    @router.post("/live/watches", dependencies=owner)
    def live_watch(raw: dict, work=Depends(workspace_dependency)):
        return work.live.policy(raw)

    @router.post("/live/watches/{wid}/evaluate", dependencies=owner)
    def live_evaluate(wid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "observed native decision")
        return work.live.evaluate(wid)

    @router.post("/live/decisions/{did}/plan", dependencies=owner)
    def live_plan(did: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "observed decision plan")
        return work.live.plan(did)

    @router.get("/economics")
    def economic_catalogue(work=Depends(workspace_dependency)):
        return work.economics.catalogue()

    @router.post("/economics/evaluate")
    def economic_evaluate(raw: dict, work=Depends(workspace_dependency)):
        return work.economics.evaluate(raw)

    @router.post("/control-policies", dependencies=owner)
    def control_policy(raw: dict, work=Depends(workspace_dependency)):
        return work.control.policy(raw)

    @router.post("/control-policies/{pid}/pause", dependencies=owner)
    def control_pause(pid: str, work=Depends(workspace_dependency)):
        return work.control.pause_policy(pid)

    @router.post("/agents", dependencies=owner)
    def create_agent(raw: dict, work=Depends(workspace_dependency)):
        return work.control.register(raw)

    @router.get("/agents/{aid}")
    def get_agent(aid: str, work=Depends(workspace_dependency)):
        return work.control.agent(aid)

    @router.post("/agents/{aid}/pause", dependencies=owner)
    def pause_agent(aid: str, work=Depends(workspace_dependency)):
        return work.control.pause_agent(aid)

    @router.post("/agents/{aid}/runs")
    def run_agent(aid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.control.run(aid, raw)

    @router.get("/agents/{aid}/runs/{rid}")
    def agent_run(aid: str, rid: str, work=Depends(workspace_dependency)):
        return work.control.get_run(aid, rid)

    @router.post("/agents/{aid}/runs/{rid}/withdraw", dependencies=owner)
    def withdraw_run(aid: str, rid: str, work=Depends(workspace_dependency)):
        return work.control.withdraw(aid, rid)

    @router.get("/overview")
    def overview(work=Depends(workspace_dependency)):
        return work.overview()

    @router.get("/profiles")
    def profiles(work=Depends(workspace_dependency)):
        from .connections import PROFILES

        return {"profiles": PROFILES, "credential_namespace": work.credential_prefix}

    @router.post("/native/evaluate")
    def native_evaluate(raw: dict, work=Depends(workspace_dependency)):
        return work.native.evaluate(raw)

    @router.post("/native/programs/compile")
    def native_compile(raw: dict, work=Depends(workspace_dependency)):
        return work.native_program.compile(raw)

    @router.post("/native/programs/evaluate")
    def native_program_evaluate(raw: dict, work=Depends(workspace_dependency)):
        return work.native_program.evaluate(raw)

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
        from .fuel import recover_fuel
        from .task_recovery import TaskRecovery

        # Bound each pass. No unbounded tasks or implicit financial dispatch.
        count = 0
        for path in sorted(self.root.glob("*.sqlite3"))[:256]:
            work = Workspace(
                path, clock=self.clock, credential_prefix="ENGINE_" + path.stem[:20].upper() + "_"
            )
            recover_fuel(work)
            TaskRecovery(work).tick()
            work.scheduler.run_once()
            work.control.recover_once()
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
