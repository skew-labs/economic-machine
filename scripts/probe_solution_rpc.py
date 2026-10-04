"""Public read-only Arbitrum heads. No mining deployment or VRF assurance follows."""
import json
import time
from pathlib import Path
from machine_engine.solution_chain import FinalizedChain, ReadRPC, quantity
from economic_machine.values import MachineError

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote evidence only')
    providers=[ReadRPC('https://sepolia-rollup.arbitrum.io/rpc'),ReadRPC('https://arbitrum-sepolia-rpc.publicnode.com')]
    report={'scope':'PUBLIC_READ_ONLY_CHAIN_HEADS_NOT_MINING_DEPLOYMENT','checked_at':int(time.time()),
        'providers':[],'mining_deployed':False,'vrf_verified':False,'chain_transactions':0}
    observations=[]
    for rpc in providers:
        try:
            chain=quantity(rpc('eth_chainId',[]));latest=FinalizedChain.block(rpc('eth_getBlockByNumber',['latest',False]))
            finalized=FinalizedChain.block(rpc('eth_getBlockByNumber',['finalized',False]))
            observations.append((rpc,latest,finalized))
            report['providers'].append({'host':rpc.identity,'chain':chain,'latest':latest,'finalized':finalized,
                'latest_age_seconds':int(time.time())-latest['timestamp'],'finalized_age_seconds':int(time.time())-finalized['timestamp']})
        except Exception as error:
            report['providers'].append({'host':rpc.identity,'state':'UNAVAILABLE_NO_FALLBACK',
                'reason':str(error) if isinstance(error,MachineError) else type(error).__name__})
    report['working_anchor_agrees']=False
    if len(observations)==2 and all(row['chain']==421614 for row in report['providers']):
        height=min(row[1]['number'] for row in observations)-4
        try:
            blocks=[FinalizedChain.block(row[0]('eth_getBlockByNumber',[hex(height),False])) for row in observations]
            report['working_anchor_agrees']=blocks[0]==blocks[1]
            report['working_anchor']=blocks[0]
        except Exception:report['working_anchor_state']='UNAVAILABLE_NO_FALLBACK'
    output=ROOT/'artifacts/solution-operations/rpc-public.json'
    output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
