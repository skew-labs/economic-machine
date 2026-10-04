"""Remote operator probe: exact unsigned quote, credential-free evidence only."""
import argparse
import json
import time
from pathlib import Path

from machine_commerce.gas_router import GasRouter, RPCS, read_json, rpc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    parser.add_argument('--amount-atoms', type=int, default=10000000)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    counts = {'rpc_reads': 0, 'quote_requests': 0}

    def read(url, method, params):
        counts['rpc_reads'] += 1
        return rpc(url, method, params)

    def quote(method, url, body=None):
        assert method == 'POST' and url.endswith('/quote'), 'QUOTE_ONLY_TRANSPORT'
        counts['quote_requests'] += 1
        return read_json(method, url, body)

    started = time.monotonic()
    intent = GasRouter(read, quote).prepare(args.owner, args.amount_atoms)
    payload = json.dumps(intent)
    assert all(url not in payload for url in RPCS), 'ENDPOINT_LEAK'
    assert intent['permit']['message']['value'] == str(args.amount_atoms)
    result = {'status': intent['status'], 'amount_atoms': intent['amount_atoms'],
              'preview_buy_wei': intent['preview_buy_wei'],
              'preview_fee_atoms': intent['preview_fee_atoms'],
              'price_guard': intent['price_guard'],
              'rpc_credentials_exposed': False,
              'latency_ms': round((time.monotonic() - started) * 1000),
              **counts, 'signatures': 0, 'order_submissions': 0, 'chain_writes': 0}
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
