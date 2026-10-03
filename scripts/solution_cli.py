"""Research worker: CPU search, verify an external agent's bits, privately seal/reveal."""
import argparse
import json
from pathlib import Path

from machine_engine.solution import SolutionLab
from machine_engine.solution_client import reveal, seal


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for cmd in ['search','verify','seal']:
        p=sub.add_parser(cmd);p.add_argument('--seed',required=True);p.add_argument('--problem',default='0');p.add_argument('--budget',default='1000000');p.add_argument('--search-seed',default='42');p.add_argument('--algorithm',default='integer_anneal')
        if cmd!='search':p.add_argument('--candidate',required=True,help='JSON with an exact decimal bits string from your local agent')
        if cmd=='seal':
            for name in ['contract','miner','secret-file']:p.add_argument('--'+name,required=True)
            p.add_argument('--round',type=int,required=True)
    p=sub.add_parser('reveal');p.add_argument('--secret-file',required=True);args=parser.parse_args()
    if args.command=='reveal':result=reveal(args.secret_file)
    else:
        bits=None
        if args.command!='search':
            path=Path(args.candidate)
            if path.stat().st_size>10000:raise ValueError('Candidate file bound')
            bits=json.loads(path.read_text())['bits']
        raw={'seed':args.seed,'problem':args.problem,'budget':args.budget,'search_seed':args.search_seed,'algorithm':args.algorithm,'bits':bits}
        if args.command=='seal':result=seal(raw,contract=args.contract,round_id=args.round,miner=args.miner,secret_file=args.secret_file)
        else:result=SolutionLab().calculate(raw)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
