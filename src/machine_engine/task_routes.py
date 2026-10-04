"""Task routes shared by hosted wallet workspaces and the local owner console."""

from fastapi import APIRouter, Depends, Response

from economic_machine.values import require_keys


def task_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter()
    owner = [Depends(require_owner)] if require_owner else []

    @router.post('/tasks/{tid}/local-plans', dependencies=owner)
    def local_plans(tid: str, raw: dict, work=Depends(workspace_dependency)):
        from .task_results import LocalWork
        return LocalWork(work).plans(tid,raw)

    @router.post('/local-work/{lid}/run', dependencies=owner)
    def local_run(lid: str, raw: dict, work=Depends(workspace_dependency)):
        from .task_results import LocalWork
        return LocalWork(work).run(lid,raw)

    @router.get('/local-work/{lid}/result', dependencies=owner)
    def local_result(lid: str, work=Depends(workspace_dependency)):
        from .task_results import LocalWork
        return LocalWork(work).result(lid)

    @router.get("/task-checkout")
    def checkout(work=Depends(workspace_dependency)):
        return work.task_checkout.status()

    @router.post("/tasks/{tid}/purchase-plan", dependencies=owner)
    def purchase_plan(tid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.task_checkout.plan(tid, raw)

    @router.get("/task-purchases/{pid}")
    def purchase(pid: str, work=Depends(workspace_dependency)):
        return work.task_checkout.get(pid)

    @router.post("/task-purchases/{pid}/approve", dependencies=owner)
    def approve(pid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.task_checkout.approve(pid, raw)

    @router.post("/task-purchases/{pid}/capture", dependencies=owner)
    def capture(pid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.task_checkout.capture(pid, raw)

    @router.post("/task-purchases/{pid}/reconcile", dependencies=owner)
    def reconcile(pid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "purchase reconciliation")
        return work.task_checkout.reconcile(pid)

    @router.post("/task-purchases/{pid}/fulfill", dependencies=owner)
    def fulfill(pid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "purchase fulfillment")
        return work.task_checkout.fulfill(pid)

    @router.post('/task-purchases/{pid}/cancel', dependencies=owner)
    def cancel_purchase(pid: str, raw: dict, work=Depends(workspace_dependency)):
        from .task_recovery import TaskRecovery
        return TaskRecovery(work).cancel(pid,raw)

    @router.post('/task-purchases/{pid}/refund', dependencies=owner)
    def refund_purchase(pid: str, raw: dict, work=Depends(workspace_dependency)):
        from .task_recovery import TaskRecovery
        return TaskRecovery(work).refund(pid,raw)

    @router.get("/task-purchases/{pid}/delivery", dependencies=owner)
    def delivery(pid: str, work=Depends(workspace_dependency)):
        result = work.task_checkout.delivery(pid)
        return Response(
            result["content"],
            media_type=result["content_type"],
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Result-Hash": result["result_hash"],
                "Content-Disposition": 'attachment; filename="' + result["filename"] + '"',
            },
        )

    @router.get("/task-purchases/{pid}/result", dependencies=owner)
    def result(pid: str, work=Depends(workspace_dependency)):
        from fastapi.responses import JSONResponse

        return JSONResponse(work.task_checkout.delivery(pid), headers={"Cache-Control": "private, no-store"})

    @router.get("/tasks")
    def tasks(work=Depends(workspace_dependency)):
        return work.tasks.status()

    @router.post("/tasks")
    def create(raw: dict, work=Depends(workspace_dependency)):
        return work.tasks.create(raw)

    @router.get("/tasks/{tid}")
    def get(tid: str, work=Depends(workspace_dependency)):
        return work.tasks.get(tid)

    @router.post("/tasks/{tid}/revise")
    def revise(tid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.tasks.revise(tid, raw)

    @router.post("/tasks/{tid}/remember", dependencies=owner)
    def remember(tid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.tasks.remember(tid, raw)

    @router.post("/tasks/{tid}/cancel", dependencies=owner)
    def cancel(tid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.tasks.cancel(tid, raw)

    @router.post("/task-preferences/{pid}/revoke", dependencies=owner)
    def revoke(pid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), "preference revocation")
        return work.tasks.revoke(pid)

    return router
