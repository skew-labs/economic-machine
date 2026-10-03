"""Task routes shared by hosted wallet workspaces and the local owner console."""

from fastapi import APIRouter, Depends

from economic_machine.values import require_keys


def task_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter()
    owner = [Depends(require_owner)] if require_owner else []

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
