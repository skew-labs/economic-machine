from fastapi import APIRouter, Depends
from economic_machine.values import require_keys


def mining_routes(workspace_dependency, *, require_owner=None):
    router = APIRouter()
    owner = [Depends(require_owner)] if require_owner else []

    @router.get('/mining', dependencies=owner)
    def status(work=Depends(workspace_dependency)):
        return work.mining.status()

    @router.post('/mining/jobs', dependencies=owner)
    def create(raw: dict, work=Depends(workspace_dependency)):
        return work.mining.create(raw)

    @router.get('/mining/jobs/{jid}', dependencies=owner)
    def get(jid: str, work=Depends(workspace_dependency)):
        return work.mining.get(jid)

    @router.post('/mining/jobs/{jid}/solve', dependencies=owner)
    def solve(jid: str, raw: dict, work=Depends(workspace_dependency)):
        return work.mining.solve(jid, raw)

    @router.post('/mining/jobs/{jid}/verify', dependencies=owner)
    def verify(jid: str, raw: dict, work=Depends(workspace_dependency)):
        require_keys(raw, {'path'}, 'mining candidate')
        return work.mining.native.calculate(work.mining.get(jid)['snapshot'], path=raw['path'])

    return router
