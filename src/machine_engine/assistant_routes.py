"""Owner-only conversation, isolated in the existing wallet workspace."""
from fastapi import APIRouter, Depends
from economic_machine.values import require_keys
from .assistant import Assistant


def assistant_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter()
    owner = [Depends(require_owner)] if require_owner else []

    @router.get('/assistant', dependencies=owner)
    def history(work=Depends(workspace_dependency)):
        return Assistant(work).history()

    @router.post('/assistant', dependencies=owner)
    def send(raw: dict, work=Depends(workspace_dependency)):
        return Assistant(work).send(raw)

    @router.post('/assistant/{rid}/task', dependencies=owner)
    def accept(rid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), 'reviewed task')
        return Assistant(work).accept_task(rid)

    return router
