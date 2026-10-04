"""Same CPU budgets, score replay, small exact reference, verification cost and open failure gates."""
import ctypes as c
import json
import statistics
import time
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from web3 import Web3, EthereumTesterProvider
from machine_engine.solution import ALGORITHMS, Answer, Edge, Graph, SolutionLab, graph

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote benchmark required')
    lab=SolutionLab();rows=[];verification=[];wins={k:0 for k in ['random','greedy','integer_anneal']}
    for problem in range(16):
        g=Graph();g.nodes=32;edges=graph(987654,problem);g.count=len(edges)
        for i,e in enumerate(edges):g.edges[i]=Edge(*e)
        results=[]
        for algorithm in wins:
            answer=Answer();start=time.perf_counter_ns();code=lab.library.solution_search(c.byref(g),42,1000000,ALGORITHMS[algorithm],c.byref(answer));elapsed=time.perf_counter_ns()-start
            assert code==0 and answer.valid and answer.edge_visits<=1000000
            checked=Answer();start=time.perf_counter_ns();code=lab.library.solution_score(c.byref(g),answer.bits,c.byref(checked));verify_ns=time.perf_counter_ns()-start
            assert code==0 and checked.score==answer.score;verification.append(verify_ns)
            # A second bounded contest uses the same native-call wall-clock allowance.
            start=time.perf_counter_ns();calls=0;time_best=0
            while time.perf_counter_ns()-start<20000000:
                candidate=Answer();assert lab.library.solution_search(c.byref(g),42+calls,50000,ALGORITHMS[algorithm],c.byref(candidate))==0
                time_best=max(time_best,candidate.score);calls+=1
            wall_elapsed=time.perf_counter_ns()-start
            row={'problem':problem,'algorithm':algorithm,'fixed_edge_budget':1000000,'edge_visits':answer.edge_visits,'score':answer.score,
                 'quality_bps':answer.score*10000//sum(e[2] for e in edges),'native_search_ns':elapsed,'native_verify_ns':verify_ns,
                 'fixed_wall_budget_ns':20000000,'wall_elapsed_ns':wall_elapsed,'wall_calls':calls,'wall_score':time_best}
            rows.append(row);results.append(row)
        top=max(r['score'] for r in results);winners=[r for r in results if r['score']==top]
        for r in winners:wins[r['algorithm']]+=1/len(winners)
    # Real EVM gas for the same graph family. VRF proof is mocked and excluded from these gas figures.
    w3=Web3(EthereumTesterProvider(EthereumTester(PyEVMBackend())));who=w3.eth.accounts[0]
    artifacts=json.loads((ROOT/'artifacts/solution/contracts.json').read_text())
    def deploy(name,*args):
        a=artifacts[name];r=w3.eth.wait_for_transaction_receipt(w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']).constructor(*args).transact({'from':who}));return w3.eth.contract(address=r.contractAddress,abi=a['abi'])
    coordinator=deploy('SolutionVRFMock');contract=deploy('SolutionMining',coordinator.address,b'k'*32,1)
    w3.eth.wait_for_transaction_receipt(contract.functions.request().transact({'from':who}));w3.eth.wait_for_transaction_receipt(coordinator.functions.deliver(1,[987654]).transact({'from':who}))
    raw={'seed':'987654','problem':'0','algorithm':'integer_anneal','budget':'1000000','search_seed':'42','bits':None}
    bits=int(lab.calculate(raw)['bits']);evm_gas=contract.functions.score(1,0,bits).estimate_gas({'from':who})
    exact=[]
    for problem in range(8):
        g=Graph();g.nodes=12;edges=graph(987654,problem,nodes=12);g.count=len(edges)
        for i,e in enumerate(edges):g.edges[i]=Edge(*e)
        optimum=Answer();assert lab.library.solution_search(c.byref(g),42,1000000,2,c.byref(optimum))==0 and optimum.complete
        for algorithm in ['greedy','integer_anneal']:
            a=Answer();assert lab.library.solution_search(c.byref(g),42,1000000,ALGORITHMS[algorithm],c.byref(a))==0
            assert a.score<=optimum.score;exact.append({'problem':problem,'nodes':12,'algorithm':algorithm,'score':a.score,'proven_optimum':optimum.score,'gap':optimum.score-a.score})
    proof={'scope':'REMOTE_CPU_LOCAL_EVM_RESEARCH_NOT_ECONOMIC_USEFULNESS_OR_PUBLIC_VRF',
           'library_sha256':lab.sha256,'rows':rows,'small_exact_reference':exact,'algorithm_winner_share':{k:v/16 for k,v in wins.items()},
           'native_verify_median_ns':int(statistics.median(verification)),'local_evm_score_gas_estimate':evm_gas,
           'equal_budget_assurance':'SEPARATE_EDGE_VISIT_AND_NATIVE_WALL_TIME_BUDGETS_NOT_EQUAL_DOLLAR_COST',
           'gpu':{'status':'NOT_RUN','reason':'NO_AUTHORIZED_GPU_LEASE'},'ai':{'status':'NOT_RUN','reason':'NO_APPROVED_PAID_PROVIDER_BUDGET'},
           'attack_gates':{'slot_saturation':'CONFIRMED_64_WALLETS_CAN_EXCLUDE_NEXT_MINER_NO_EXTRA_MINT','withholding':'BOUNDED_CHANGE_NOT_IMMUNE','randomness':'MOCK_TESTS_ONLY'},
           'public_emission':False,'mainnet_ready':False}
    (ROOT/'artifacts/solution/benchmark.json').write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps({k:v for k,v in proof.items() if k not in {'rows','small_exact_reference'}}))


if __name__=='__main__':main()
