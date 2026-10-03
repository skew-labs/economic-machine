"""Private durable commitment state; no signer/network or central API credential custody."""
import json
import os
import secrets
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
from economic_machine.values import MachineError
from .mining_client import binding
from .solution import SolutionLab


def seal(raw, *, contract, round_id, miner, secret_file):
    contract,chain,round_id,miner=binding(contract,421614,round_id,miner)
    # Recompute; neither a model's claimed score nor a server result is accepted as evidence.
    result=SolutionLab().calculate(raw)
    bits=int(result['bits']);problem=int(raw['problem']);salt=secrets.token_bytes(32)
    fingerprint=keccak(encode(['address','uint256','uint256','uint8','address','uint32','bytes32'],[contract,chain,round_id,problem,miner,bits,salt]))
    secret={'schema':'solution-secret-1','contract':contract,'chain':chain,'round':str(round_id),'problem':problem,'miner':miner,
            'bits':str(bits),'salt':salt.hex(),'commitment':fingerprint.hex(),'graph_sha256':result['graph_sha256']}
    fd=os.open(Path(secret_file),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:out.write(json.dumps(secret,sort_keys=True)+'\n');out.flush();os.fsync(out.fileno())
    directory=os.open(Path(secret_file).parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    data=keccak(text='commit(uint256,uint8,bytes32)')[:4]+encode(['uint256','uint8','bytes32'],[round_id,problem,fingerprint])
    return {'status':'UNSIGNED_OFFLINE','transaction':{'to':contract,'from':miner,'chainId':chain,'value':'0x0','data':'0x'+data.hex()},
            'commitment':'0x'+fingerprint.hex(),'graph_sha256':result['graph_sha256'],'chain_round_verified':False,
            'required_before_signature':'Read the actual VRF-backed round seed/threshold/window, compare graph; owner signing only.'}


def reveal(secret_file):
    p=Path(secret_file)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077 or p.stat().st_size>8192:raise MachineError('SOLUTION_PRIVATE_SECRET_REQUIRED')
    raw=json.loads(p.read_text());contract,chain,round_id,miner=binding(raw['contract'],raw['chain'],int(raw['round']),raw['miner'])
    problem=raw['problem'];bits=int(raw['bits']);salt=bytes.fromhex(raw['salt'])
    if type(problem) is not int or not 0<=problem<16 or not 0<=bits<2**32 or bits&1 or len(salt)!=32:raise MachineError('SOLUTION_SECRET_BOUND')
    fingerprint=keccak(encode(['address','uint256','uint256','uint8','address','uint32','bytes32'],[contract,chain,round_id,problem,miner,bits,salt]))
    if fingerprint.hex()!=raw['commitment']:raise MachineError('SOLUTION_SECRET_INTEGRITY')
    data=keccak(text='reveal(uint256,uint8,uint32,bytes32)')[:4]+encode(['uint256','uint8','uint32','bytes32'],[round_id,problem,bits,salt])
    return {'status':'UNSIGNED_OFFLINE','chain_round_verified':False,'transaction':{'to':contract,'from':miner,'chainId':chain,'value':'0x0','data':'0x'+data.hex()},
            'required_before_signature':'Confirm chain inclusion of the commitment and current reveal phase; salt is now present in calldata.'}
