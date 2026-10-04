"""Pinned compiler; only the solution research protocol and test coordinator."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote compile required')
    compiler=ROOT/'tools/solc-0.8.24';sha=hashlib.sha256(compiler.read_bytes()).hexdigest()
    assert sha==json.loads((ROOT/'artifacts/contract-build.json').read_text())['compiler_sha256']
    sources={p:{'content':(ROOT/'contracts'/p).read_text()} for p in ['SolutionMining.sol','SolutionVRFMock.sol']}
    settings={'optimizer':{'enabled':True,'runs':200},'viaIR':True,'evmVersion':'shanghai','outputSelection':{'*':{'*':['abi','evm.bytecode.object','evm.deployedBytecode.object']}}}
    run=subprocess.run([str(compiler),'--standard-json'],input=json.dumps({'language':'Solidity','sources':sources,'settings':settings}),text=True,capture_output=True,check=True,timeout=90)
    result=json.loads(run.stdout)
    if any(e['severity']=='error' for e in result.get('errors',[])):raise RuntimeError(json.dumps(result['errors']))
    artifacts={name:{'abi':v['abi'],'bytecode':v['evm']['bytecode']['object'],'runtime':v['evm']['deployedBytecode']['object']} for group in result['contracts'].values() for name,v in group.items() if v['evm']['bytecode']['object']}
    folder=ROOT/'artifacts/solution';folder.mkdir(parents=True,exist_ok=True)
    (folder/'contracts.json').write_text(json.dumps(artifacts,sort_keys=True)+'\n')
    proof={'compiler_sha256':sha,'sources':{name:hashlib.sha256(v['content'].encode()).hexdigest() for name,v in sources.items()},'warnings':result.get('errors',[]),
           'runtime_sizes':{k:len(v['runtime'])//2 for k,v in artifacts.items()},'deployment':'NONE','vrf':'MOCK_TESTS_ONLY_NOT_PUBLIC_PROOF','emission':'LOCAL_EVM_RESEARCH_ONLY'}
    (folder/'contract-build.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))


if __name__=='__main__':main()
