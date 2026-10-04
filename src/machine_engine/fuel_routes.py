"""Authenticated engine routes. Agent keys propose; wallet owners authorize."""
from fastapi import APIRouter, Depends

from economic_machine.values import require_keys

from .fuel import Fuel


def fuel_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter()
    owner = [Depends(require_owner)] if require_owner else []

    @router.post("/fuel/policies", dependencies=owner)
    def policy(raw: dict, work=Depends(workspace_dependency)):
        return Fuel(work).policy(raw)

    @router.post("/fuel/policies/{pid}/pause", dependencies=owner)
    def pause(pid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "pause fuel policy")
        return Fuel(work).pause(pid)

    @router.post("/fuel/requests")
    def propose(raw: dict, work=Depends(workspace_dependency)):
        result = Fuel(work).propose(raw)
        if result.get("id"):
            result["wallet_review_url"] = "/commerce/console?fuel=" + result["id"] + "#overview"
        return result

    @router.get("/fuel/requests/{fid}")
    def get(fid: str, work=Depends(workspace_dependency)):
        return Fuel(work).get(fid)

    @router.post("/fuel/requests/{fid}/order", dependencies=owner)
    def order(fid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"signature"}, "wallet permit")
        return Fuel(work).wallet_step(fid, "order", raw["signature"])

    @router.post("/fuel/requests/{fid}/submit", dependencies=owner)
    def submit(fid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"signature"}, "wallet order")
        return Fuel(work).wallet_step(fid, "submit", raw["signature"])

    @router.post("/fuel/requests/{fid}/reconcile")
    def reconcile(fid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "fuel reconciliation")
        return Fuel(work).reconcile(fid)

    @router.post("/fuel/requests/{fid}/resume-review", dependencies=owner)
    def resume(fid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {"parent_action_hash"}, "bound parent review")
        return Fuel(work).resume_review(fid, raw["parent_action_hash"])

    @router.post("/fuel/requests/{fid}/cancel-parent", dependencies=owner)
    def cancel(fid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "cancel unhanded parent")
        return Fuel(work).cancel_parent(fid)

    return router
