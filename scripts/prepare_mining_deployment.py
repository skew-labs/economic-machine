"""Two independent RPC reads and an unsigned testnet deployment review. Never signs."""
import argparse
import hashlib
import json
import time
from pathlib import Path

from web3 import Web3
from economic_machine.values import digest

ROOT = Path(__file__).resolve().parents[1]
RPCS = ('https://sepolia-rollup.arbitrum.io/rpc', 'https://arbitrum-sepolia.drpc.org')
TOKEN = '0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d'


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('RPC collection only on the authorized remote host')
    parser = argparse.ArgumentParser(); parser.add_argument('--owner', required=True); args = parser.parse_args()
    owner = Web3.to_checksum_address(args.owner)
    artifact = json.loads((ROOT / 'artifacts/mining/contracts.json').read_text())['MachineMining']
    observations = []; draft = None
    for url in RPCS:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 15}))
        if w3.eth.chain_id != 421614 or len(w3.eth.get_code(TOKEN)) == 0:
            raise RuntimeError('Arbitrum Sepolia and test USDC required')
        factory = w3.eth.contract(abi=artifact['abi'], bytecode=artifact['bytecode'])
        data = factory.constructor(TOKEN).data_in_transaction
        gas = w3.eth.estimate_gas({'from': owner, 'data': data, 'value': 0})
        observations.append({'rpc': url, 'chain_id': w3.eth.chain_id, 'nonce': w3.eth.get_transaction_count(owner, 'pending'),
                             'gas_estimate': gas, 'gas_price_wei': w3.eth.gas_price, 'balance_wei': w3.eth.get_balance(owner)})
        draft = {'from': owner, 'chainId': 421614, 'value': 0, 'data': data}
    if observations[0]['nonce'] != observations[1]['nonce']:
        raise RuntimeError('Independent pending nonces disagree; do not prepare a replacement')
    gas_limit = (max(o['gas_estimate'] for o in observations) * 120 + 99) // 100
    gas_price = max(o['gas_price_wei'] for o in observations) * 2
    draft |= {'nonce': observations[0]['nonce'], 'gas': gas_limit, 'gasPrice': gas_price}
    body = {'schema': 'machine-mining-deployment-review-1', 'created_at': int(time.time()),
            'contract': 'MachineMining', 'reward_token': TOKEN, 'observations': observations,
            'source_sha256': hashlib.sha256((ROOT / 'contracts/MachineMining.sol').read_bytes()).hexdigest(),
            'creation_data_sha256': hashlib.sha256(bytes.fromhex(draft['data'][2:])).hexdigest(),
            'maximum_test_gas_wei': gas_limit * gas_price, 'balance_sufficient': min(o['balance_wei'] for o in observations) >= gas_limit * gas_price,
            'unsigned_transaction': draft, 'status': 'UNSIGNED_REQUIRES_OWNER_APPROVAL',
            'token_transfer': False, 'mainnet': False, 'mining_job_created': False, 'authority': 'NONE'}
    body['review_sha256'] = digest(body)
    (ROOT / 'artifacts/mining/deployment-review.json').write_text(json.dumps(body, indent=2) + '\n')
    print(json.dumps({k: v for k, v in body.items() if k != 'unsigned_transaction'}))


if __name__ == '__main__': main()
