"""Isolated Robinhood worker using shared native graph, Blob and budget admission."""
import time
from economic_machine.arbitrum_research.runtime import Runtime as BaseRuntime,metrics,apply_limits
from economic_machine.arbitrum_research.governor import govern,conservative_proposal,validate_metrics
from economic_machine.pump_research.store import atomic,canonical
from .source import RPC,SourceBackoff,SourceHTTPError
from .bridge import Bridge

UNIT='economic-machine-robinhood-research.service'
class Runtime(BaseRuntime):
    chain_id=4663
    source_name='Public Pinax Robinhood RPC with official Robinhood comparison'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.bridge=Bridge(self.base,self.storage);self.audit_rpc=RPC(4663,official=True)
    def manage(self,force=False):
        if not force and time.time_ns()//1000000-self.state['last_manager_ms']<60000:return
        self.reclamp()
    def reclamp(self):
        current=validate_metrics(metrics(self.base,self.storage.cloud.spend,
            (self.state.get('head') or 0)-self.state['next_block']+1,self.state['backoff_until']-time.time(),unit=UNIT))
        proposal=conservative_proposal(current)
        page_blocks=getattr(self.source,'page_blocks',16)
        proposal.update(download_batch_blocks=min(page_blocks,self.state.get('source_batch_cap',page_blocks)),
            schedule_delay_seconds=max(1,self.state.get('source_min_delay',1)),model_research_workers=10,
            io_priority='normal',reason='Bounded public RPC source with ten evidence investigators; shared native host admission.')
        policy=govern(proposal,current);policy.update(effective_download_concurrency=1,manager_response_applied=False,
            scope='isolated_robinhood_research_only',native_resource_manager=True)
        prior=self.state.get('applied_policy',{})
        if (self.resource_reapply_pending or any(abs(policy[k]-prior.get(k,0))>max(1,prior.get(k,0)*.25)
            for k in ('cpu_quota_percent','memory_max_bytes','io_weight'))):
            self.state['resource_application']=apply_limits(policy,self.resource_apply,unit=UNIT)
            self.state['applied_policy']=policy;self.resource_reapply_pending=False
        self.state.update(policy=policy,last_manager_ms=time.time_ns()//1000000)
        self.save();atomic(self.out/'resource-manager.json',dict(metrics=current,applied=policy))
    def audit(self,page):
        now=time.time()
        if now-self.state.get('last_audit_at',0)>=600:
            records=[self.audit_rpc.check()];checked=page['blocks'][-1]
            header,record=self.audit_rpc.result('eth_getBlockByNumber',[hex(checked['header']['number']),False]);records.append(record)
            mismatch=(header.get('hash')!=checked['header']['hash'])
            if checked['transactions']:
                tx=checked['transactions'][0]
                receipt,record=self.audit_rpc.result('eth_getTransactionReceipt',[tx['hash']]);records.append(record)
                mismatch=mismatch or not receipt or receipt.get('blockHash')!=checked['header']['hash'] or int(receipt.get('status','0x2'),16)!=tx['status']
            proof=self.bridge.retain(records,'robinhood_official_provider_comparison')
            outcome='MISMATCH' if mismatch else 'TWO_PROVIDERS_AGREE'
            self.state['last_audit_at']=now;self.save()
            with self.db:self.db.execute('INSERT OR REPLACE INTO audits VALUES(?,?,?,?)',
                (page['sha256'],outcome,time.time_ns()//1000000,canonical(proof)))
            atomic(self.out/'latest-audit.json',dict(outcome=outcome,block_number=checked['header']['number'],proof=proof,
                independent_consensus_verified=False))
            if mismatch:
                self.state['integrity_halt']='ROBINHOOD_PROVIDER_MISMATCH';self.save();raise ValueError('ROBINHOOD_PROVIDER_MISMATCH')
    def publish(self,state,**extra):
        value=super().publish(state,**extra);value['schema']='economic-machine.robinhood-live-status.v1'
        value['crosschain']=self.bridge.snapshot() if hasattr(self,'bridge') else None
        value['trace_semantics']='callTracer when available; Nitro system transfers retained but unclassified'
        atomic(self.out/'live-status.json',value);return value
    def close(self):
        if hasattr(self,'bridge'):self.bridge.close()
        super().close()
