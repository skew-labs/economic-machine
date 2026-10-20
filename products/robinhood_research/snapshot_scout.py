"""Bounded tail/member scouting under the original snapshot request ledger."""
import argparse
import json
from pathlib import Path
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.robinhood_research.snapshot_scout import Scout, MAX_READ


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base', required=True)
    p.add_argument('--catalogue', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--tail-mib', type=int, default=8)
    p.add_argument('--start', type=int)
    a = p.parse_args()
    if not 1 <= a.tail_mib <= 64:
        raise ValueError('SCOUT_FINITE_BOUND')
    cloud = Cloud(a.base)
    s = Scout(a.catalogue, a.out, cloud)
    try:
        length = a.tail_mib*1024**2
        start = a.start if a.start is not None else max(0, (s.total-length)//512*512)
        end = min(s.total, start+length)
        for pos in range(start, end, MAX_READ):
            _, row = s.read(pos, min(MAX_READ, end-pos))
            print(json.dumps(dict(start=row['start'], bytes=row['bytes'], candidates=row['candidates'])), flush=True)
    finally:
        s.close(); cloud.close()


if __name__ == '__main__':
    main()
