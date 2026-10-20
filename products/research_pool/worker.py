"""Run the explicitly authorized shared public-evidence Muse pool."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool-root', required=True)
    parser.add_argument('--solana-base', required=True)
    parser.add_argument('--arbitrum-base', required=True)
    parser.add_argument('--robinhood-base', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--policy-sha256', required=True)
    parser.add_argument('--once', action='store_true', help='Admit at most fifty new jobs, then drain and exit')
    args = parser.parse_args()
    # Each existing collector retains its own deployed source. Research uses
    # the EVM modules from its pool host and SOL modules from the SOL checkout.
    # No production package is copied over another service's implementation.
    arb = Path(args.arbitrum_base) / 'source/src/economic_machine'
    sol = Path(args.solana_base) / 'source/src/economic_machine'
    sys.path.insert(0, str(sol.parent))
    import economic_machine
    for path in (sol, arb):
        if str(path) not in economic_machine.__path__:
            economic_machine.__path__.insert(0, str(path))
    import economic_machine.pump_research
    sol_pump = str(sol / 'pump_research')
    if sol_pump not in economic_machine.pump_research.__path__:
        economic_machine.pump_research.__path__.insert(0, sol_pump)
    from economic_machine.research_pool.runner import Runner
    result = Runner(args.pool_root, args.solana_base, args.arbitrum_base, args.robinhood_base,
                    args.key_file, args.policy, args.policy_sha256).run(once=args.once)
    print(json.dumps(dict(state=result['state'], configured=result['configured'],
                         observed_peak_provider_calls=result['observed_peak_provider_calls'],
                         model_calls=result['model_calls']), sort_keys=True))


if __name__ == '__main__':
    main()
