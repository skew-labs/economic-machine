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

    @router.get('/assistant/provider', dependencies=owner)
    def provider_status():
        import os
        selected=os.environ.get('SKEW_ASSISTANT_PROVIDER','bedrock')
        blocked=os.environ.get('SKEW_BEDROCK_ACCESS_STATUS')=='ORGANIZATION_DENY'
        return {'provider':selected,'status':'ORGANIZATION_DENY' if selected=='bedrock' and blocked else 'CONFIGURED_NOT_PROBED',
                'fallback':False,'model_response_verified':False}

    @router.get('/assistant/work', dependencies=owner)
    def work_status(work=Depends(workspace_dependency)):
        from .task_results import LocalWork
        return {'tasks':work.tasks.status()['tasks'],'purchases':work.task_checkout.status()['purchases'],
                'local':LocalWork(work).status()['jobs']}

    @router.post('/assistant', dependencies=owner)
    def send(raw: dict, work=Depends(workspace_dependency)):
        return Assistant(work).send(raw)

    @router.post('/assistant/{rid}/task', dependencies=owner)
    def accept(rid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, set(), 'reviewed task')
        return Assistant(work).accept_task(rid)

    return router
