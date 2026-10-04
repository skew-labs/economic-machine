"""Signed merchant events locate an existing purchase, never create a workspace."""
import sqlite3
from fastapi import APIRouter, Request
from economic_machine.values import MachineError
from .paypal import PayPalSandbox
from .task_recovery import TaskRecovery, verified_webhook
from .workspace import Workspace


def webhook_routes(hosted):
    router = APIRouter()

    @router.post('/api/task-webhooks/paypal')
    async def webhook(request: Request):
        provider = PayPalSandbox.configured()
        if provider is None or not provider.credentials_available():
            raise MachineError('PAYPAL_SANDBOX_NOT_CONFIGURED')
        event = await request.json()
        # Verify before consulting tenant files, including on replay.
        import asyncio
        oid = await asyncio.to_thread(verified_webhook,provider,request.headers,event)
        def apply():
            matches = []
            for path in sorted(hosted.root.glob('*.sqlite3'))[:256]:
                if path.is_symlink(): continue
                with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='engine_task_purchases'").fetchone():continue
                    row = db.execute('SELECT id FROM engine_task_purchases WHERE order_id=?',(oid,)).fetchone()
                    if row:matches.append((path,row[0]))
            if len(matches)>1:raise MachineError('AMBIGUOUS_MERCHANT_ORDER')
            if not matches:return {'accepted':True}
            path,pid=matches[0]
            work=Workspace(path,clock=hosted.clock,task_provider=provider,
                credential_prefix='ENGINE_'+path.stem[:20].upper()+'_')
            TaskRecovery(work).webhook(pid,event)
            return {'accepted':True}
        return await asyncio.to_thread(apply)

    return router
