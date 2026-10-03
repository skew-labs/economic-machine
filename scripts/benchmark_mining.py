"""Remote measured calculation latency, explicitly separate from chain/network/LLM latency."""
import json
import platform
import statistics
from pathlib import Path

from machine_engine.mining import NativeMining, example

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Benchmark only on the authorized remote host')
    solver=NativeMining();job=example()
    for _ in range(100):solver.calculate(job)
    measurements=[solver.calculate(job) for _ in range(2000)]
    latencies=sorted(int(r['elapsed_ns']) for r in measurements)
    direct=solver.calculate(job,path=[0]);best=measurements[-1]
    report={'schema':'machine-mining-measurement-1','host':platform.machine(),'sample_count':len(latencies),
            'input_sha256':best['input_sha256'],'library_sha256':best['library_sha256'],
            'measured_region':'CTYPES_NATIVE_CALL_ONLY_EXCLUDES_MARSHAL_HASH_API_CHAIN',
            'native_median_ns':int(statistics.median(latencies)),'native_p95_ns':latencies[(len(latencies)*95)//100],
            'native_max_ns':max(latencies),'language_model_calls':0,
            'synthetic_comparison':{'direct_path':direct['path'],'direct_net':direct['net_output'],
                                    'selected_path':best['path'],'selected_net':best['net_output'],
                                    'extra_normalized_output_units':str(int(best['net_output'])-int(direct['net_output']))},
            'assurance':'SYNTHETIC_FROZEN_MODEL_NOT_LIVE_RETURN_OR_END_TO_END_NETWORK_BENCHMARK'}
    (ROOT/'artifacts/mining/benchmark.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
