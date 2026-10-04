"""Self-hosted useful-work client: example, CPU solve, external candidate verify, seal, reveal."""
import argparse
import json
from pathlib import Path

from machine_engine.mining import NativeMining, example
from machine_engine.mining_client import reveal, seal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('example')
    for command in ['solve', 'verify']:
        p = sub.add_parser(command); p.add_argument('--job', required=True)
        if command == 'solve':
            p.add_argument('--budget', type=int, default=70000)
        else:
            p.add_argument('--candidate', required=True, help='JSON containing a path list, from any local agent')
    p = sub.add_parser('seal')
    for flag in ['job', 'result', 'contract', 'miner', 'secret-file']:
        p.add_argument('--' + flag, required=True)
    p.add_argument('--job-id', required=True, type=int)
    p = sub.add_parser('reveal'); p.add_argument('--secret-file', required=True)
    args = parser.parse_args()
    def read(path):
        p = Path(path)
        if p.stat().st_size > 50000:
            raise ValueError('Input exceeds job size limit')
        return json.loads(p.read_text())
    if args.command == 'example':
        result = example()
    elif args.command in {'solve', 'verify'}:
        result = NativeMining().calculate(read(args.job), **({'budget': args.budget} if args.command == 'solve' else {'path': read(args.candidate)['path']}))
    elif args.command == 'seal':
        # Independently recompute even when an external AI supplied the result file.
        snapshot = read(args.job); candidate = read(args.result)
        verified = NativeMining().calculate(snapshot, path=candidate['path'])
        result = seal(snapshot, verified, contract=args.contract, jid=args.job_id, miner=args.miner, secret_file=args.secret_file)
    else:
        result = reveal(args.secret_file)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
