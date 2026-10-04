"""Bounded remote soak of real native frames. Not a production duration certification."""
import ctypes as c
import json
import platform
import resource
import statistics
import subprocess
import time
from pathlib import Path

from machine_engine.solution import Edge, graph
from machine_engine.solution_operations import Frame, MAGIC, Result

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote verification only')
    build=json.loads((ROOT/'artifacts/solution-operations/native-build.json').read_text())
    inputs=[];edges_by_problem=[]
    for i in range(16):
        edges=graph(12345,i);edges_by_problem.append(edges)
        frame=Frame();frame.magic=MAGIC;frame.version=1;frame.id=i
        frame.graph.nodes=32;frame.graph.count=496;frame.seed=42+i;frame.budget=100000;frame.algorithm=1
        for index,e in enumerate(edges):frame.graph.edges[index]=Edge(*e)
        inputs.append(bytes(frame))
    payload=b''.join(inputs);size=c.sizeof(Result);start=time.monotonic();latencies=[];native=[];jobs=0
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    while time.monotonic()-start<120:
        at=time.perf_counter_ns()
        run=subprocess.run([build['executable']],input=payload,capture_output=True,timeout=10)
        elapsed=time.perf_counter_ns()-at
        if run.returncode or len(run.stdout)!=16*size:raise RuntimeError('Native soak frame failure')
        for i,edges in enumerate(edges_by_problem):
            result=Result.from_buffer_copy(run.stdout[i*size:(i+1)*size]);bits=result.answer.bits
            expected=sum(w for u,v,w in edges if ((bits>>u)^(bits>>v))&1)
            if (result.id!=i or result.status or result.magic!=MAGIC or result.version!=1 or not result.answer.valid
                    or bits&1 or bits>=2**32 or expected!=result.answer.score or result.answer.edge_visits>100000):
                raise RuntimeError('Native soak score/order failure')
            native.append(result.elapsed_ns)
        jobs+=16;latencies.append(elapsed);time.sleep(0.01)
    elapsed=time.monotonic()-start;after=resource.getrusage(resource.RUSAGE_CHILDREN)
    def summary(samples):
        rows=sorted(samples)
        return {'p50_ns':int(statistics.median(rows)),'p99_ns':rows[min(len(rows)-1,len(rows)*99//100)],'maximum_ns':max(rows)}
    report={'scope':'120_SECOND_FIXED_FRAME_NATIVE_SOAK_NOT_24H_OR_MULTIUSER_CERTIFICATION',
        'duration_seconds':round(elapsed,3),'jobs':jobs,'frame_or_score_errors':0,'maximum_batch':16,
        'batch_round_trip_includes_spawn_and_verify':summary(latencies),'native_search':summary(native),
        'peak_child_rss_kib':after.ru_maxrss,'child_cpu_seconds':round(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,3),
        'platform':platform.platform(),'pipeline_sha256':build['sha256'],'paid_api_calls':0,'chain_transactions':0,
        'comparability_to_firedancer_or_ore':'NOT_MEASURED_DIFFERENT_WORKLOADS'}
    (ROOT/'artifacts/solution-operations/soak.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
