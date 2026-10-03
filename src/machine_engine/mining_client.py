"""Owner-operated offline candidate sealing. Never sends transactions or provider keys."""
import json
import os
import secrets
from pathlib import Path

from eth_abi import encode
from eth_utils import is_address, keccak, to_checksum_address

from economic_machine.values import MachineError, digest
from .mining import normalize


def binding(contract, chain, jid, miner):
    if not is_address(contract) or not is_address(miner) or int(contract, 16) == 0 or int(miner, 16) == 0:
        raise MachineError('MINING_ADDRESS_REQUIRED')
    if type(chain) is not int or chain != 421614 or type(jid) is not int or not 1 <= jid < 2**256:
        raise MachineError('MINING_ARBITRUM_SEPOLIA_JOB_REQUIRED')
    return to_checksum_address(contract), chain, jid, to_checksum_address(miner)


def commitment(contract, chain, jid, miner, path, salt):
    binding(contract, chain, jid, miner)
    if not isinstance(path, list) or not 1 <= len(path) <= 4 or any(type(i) is not int or not 0 <= i < 16 for i in path):
        raise MachineError('MINING_PATH_BOUND')
    if not isinstance(salt, bytes) or len(salt) != 32:
        raise MachineError('MINING_SALT_REQUIRED')
    return keccak(encode(['address', 'uint256', 'uint256', 'address', 'uint8[]', 'bytes32'],
                         [contract, chain, jid, miner, path, salt]))


def seal(snapshot, result, *, contract, jid, miner, secret_file):
    snapshot = normalize(snapshot)
    contract, chain, jid, miner = binding(contract, 421614, jid, miner)
    if result.get('input_sha256') != digest(snapshot) or result.get('valid') is not True:
        raise MachineError('MINING_RESULT_BINDING_REQUIRED')
    salt = secrets.token_bytes(32)
    fingerprint = commitment(contract, chain, jid, miner, result['path'], salt)
    private = {'schema': 'machine-mining-secret-1', 'contract': contract, 'chain': chain, 'job_id': str(jid),
               'miner': miner, 'input_sha256': digest(snapshot), 'path': result['path'], 'salt': '0x' + salt.hex(),
               'commitment': '0x' + fingerprint.hex()}
    target = Path(secret_file)
    # Never overwrite an existing salt or follow a final-component symlink.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(private, sort_keys=True) + '\n'); output.flush(); os.fsync(output.fileno())
    data = keccak(text='commit(uint256,bytes32)')[:4] + encode(['uint256', 'bytes32'], [jid, fingerprint])
    return {'transaction': {'to': contract, 'from': miner, 'chainId': chain, 'value': '0x0', 'data': '0x' + data.hex()},
            'commitment': private['commitment'], 'input_sha256': private['input_sha256'],
            'status': 'UNSIGNED_OFFLINE_REQUEST', 'chain_job_verified': False,
            'required_before_signature': 'Read frozen job, reward token, bond and deadline from the deployment; compare snapshot. Approve exact bond separately.'}


def reveal(secret_file):
    path = Path(secret_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192 or path.stat().st_mode & 0o077:
        raise MachineError('MINING_PRIVATE_SECRET_FILE_REQUIRED')
    raw = json.loads(path.read_text())
    contract, chain, jid, miner = binding(raw['contract'], raw['chain'], int(raw['job_id']), raw['miner'])
    salt = bytes.fromhex(raw['salt'].removeprefix('0x'))
    fingerprint = commitment(contract, chain, jid, miner, raw['path'], salt)
    if '0x' + fingerprint.hex() != raw['commitment']:
        raise MachineError('MINING_SECRET_INTEGRITY_FAILED')
    data = keccak(text='reveal(uint256,uint8[],bytes32)')[:4] + encode(['uint256', 'uint8[]', 'bytes32'], [jid, raw['path'], salt])
    return {'transaction': {'to': contract, 'from': miner, 'chainId': chain, 'value': '0x0', 'data': '0x' + data.hex()},
            'status': 'UNSIGNED_OFFLINE_REQUEST', 'chain_job_verified': False,
            'required_before_signature': 'Read commitment and current reveal phase from chain. This output exposes the salt; publish only during the reveal window.'}
