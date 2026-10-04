"""Research Max-Cut jobs; fixed state, bounded CPU search, no live mint/sign/provider call."""
import ctypes as c
import hashlib
import os
import time
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
from economic_machine.values import MachineError, digest, require_keys
from .mining import integer

DOMAIN = keccak(text='SKEW_MAXCUT_V1')
ALGORITHMS = {'random': 0, 'integer_anneal': 1, 'exact_small': 2, 'greedy': 3}


def graph(seed, problem, nodes=32):
    if type(seed) is not int or not 0 <= seed < 2**256 or type(problem) is not int or not 0 <= problem < 16 or type(nodes) is not int or not 2 <= nodes <= 64:
        raise MachineError('SOLUTION_GRAPH_BOUND')
    edges=[]
    for u in range(nodes):
        for v in range(u+1,nodes):
            weight=int.from_bytes(keccak(encode(['bytes32','uint256','uint256','uint256'],[DOMAIN,seed,problem,len(edges)])),'big')%1024+1
            edges.append([u,v,weight])
    return edges


class Edge(c.Structure):
    _fields_=[('u',c.c_uint32),('v',c.c_uint32),('weight',c.c_uint32)]


class Graph(c.Structure):
    _fields_=[('edges',Edge*2016),('nodes',c.c_uint32),('count',c.c_uint32)]


class Answer(c.Structure):
    _fields_=[(k,c.c_uint64) for k in ('bits','score','edge_visits','candidates')]+[('valid',c.c_uint32),('complete',c.c_uint32)]


class SolutionLab:
    def __init__(self):
        self.library=None;self.sha256=None;self.pipeline=None
        pipeline=os.environ.get('ENGINE_SOLUTION_PIPELINE');pipeline_sha=os.environ.get('ENGINE_SOLUTION_PIPELINE_SHA256')
        if pipeline or pipeline_sha:
            if not pipeline or not pipeline_sha:raise MachineError('SOLUTION_PIPELINE_CONFIGURATION')
            from .solution_operations import NativePipeline
            self.pipeline=NativePipeline(pipeline,pipeline_sha)
        path=os.environ.get('ENGINE_SOLUTION_LIBRARY');expected=os.environ.get('ENGINE_SOLUTION_SHA256')
        if not path:return
        p=Path(path)
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_size>10000000:
            raise MachineError('SOLUTION_LIBRARY_PATH')
        actual=hashlib.sha256(p.read_bytes()).hexdigest()
        if not expected or actual!=expected:raise MachineError('SOLUTION_LIBRARY_SHA256')
        lib=c.CDLL(str(p))
        for name,restype in [('solution_abi',c.c_uint),('solution_graph_size',c.c_size_t),('solution_answer_size',c.c_size_t)]:
            fn=getattr(lib,name);fn.argtypes=[];fn.restype=restype
        if lib.solution_abi()!=1 or lib.solution_graph_size()!=c.sizeof(Graph) or lib.solution_answer_size()!=c.sizeof(Answer):raise MachineError('SOLUTION_ABI')
        lib.solution_score.argtypes=[c.POINTER(Graph),c.c_uint64,c.POINTER(Answer)];lib.solution_score.restype=c.c_int
        lib.solution_search.argtypes=[c.POINTER(Graph),c.c_uint64,c.c_uint64,c.c_uint,c.POINTER(Answer)];lib.solution_search.restype=c.c_int
        lib.solution_score_batch.argtypes=[c.POINTER(Graph),c.POINTER(c.c_uint64),c.c_size_t,c.POINTER(Answer)];lib.solution_score_batch.restype=c.c_int
        self.library=lib;self.sha256=actual

    def status(self):
        return {'enabled':self.library is not None,'library_sha256':self.sha256,'model':'MAXCUT_V1_RESEARCH',
                'nodes':32,'problems_per_round':16,'maximum_edge_visits':10000000,'algorithms':list(ALGORITHMS),
                'token':'SKEWSIM_UNDEPLOYED_RESEARCH_TOKEN','live_emission':False,'live_vrf_configured':False,
                'gpu_comparison':'NOT_RUN_NO_AUTHORIZED_GPU_LEASE','ai_comparison':'NOT_RUN_NO_APPROVED_PROVIDER_BUDGET',
                'execution_authority':'NONE','randomness_assurance':'LOCAL_FIXTURE_NOT_VRF',
                'execution_backend':'SANDBOXED_CPP_PIPELINE' if self.pipeline else 'SCALAR_CPP',
                'pipeline_sha256':self.pipeline.sha256 if self.pipeline else None}

    def calculate(self,raw):
        require_keys(raw,{'seed','problem','algorithm','budget','search_seed','bits'},'solution research request')
        seed=self.seed(raw['seed'])
        problem=integer(raw['problem'],0,15);budget=integer(raw['budget'],496,10000000);search_seed=integer(raw['search_seed'],0,2**63-1)
        if not isinstance(raw['algorithm'],str) or raw['algorithm'] not in ALGORITHMS:raise MachineError('SOLUTION_ALGORITHM')
        if self.library is None:raise MachineError('SOLUTION_LIBRARY_NOT_CONFIGURED')
        # ABI shape is fixed at 32 nodes for on-chain parity. Exact mode belongs to the separate small-graph benchmark.
        if raw['algorithm']=='exact_small':raise MachineError('SOLUTION_EXACT_SMALL_ONLY')
        edges=graph(seed,problem);request=Graph();request.nodes=32;request.count=len(edges)
        for i,e in enumerate(edges):request.edges[i]=Edge(*e)
        answer=Answer();start=time.perf_counter_ns()
        if raw['bits'] is None and self.pipeline is not None:
            row,=self.pipeline.search([{'seed':seed,'problem':problem,'budget':budget,
                'algorithm':raw['algorithm'],'search_seed':search_seed}])
            answer.bits=int(row['bits']);answer.score=int(row['score']);answer.edge_visits=row['edge_visits']
            answer.candidates=row['candidates'];answer.valid=1;code=0
            elapsed=int(row['elapsed_native_ns'])
        elif raw['bits'] is None:
            code=self.library.solution_search(c.byref(request),search_seed,budget,ALGORITHMS[raw['algorithm']],c.byref(answer))
        else:
            bits=integer(raw['bits'],0,2**32-1);code=self.library.solution_score(c.byref(request),bits,c.byref(answer))
        if raw['bits'] is not None or self.pipeline is None:elapsed=time.perf_counter_ns()-start
        if code or not answer.valid:raise MachineError('SOLUTION_NATIVE_REJECTED')
        total=sum(e[2] for e in edges)
        result={'model':'MAXCUT_V1_RESEARCH','input_sha256':digest(raw),'graph_sha256':digest(edges),
                'seed':raw['seed'],'problem':raw['problem'],'bits':str(answer.bits),'score':str(answer.score),'total_weight':str(total),
                'quality_bps':answer.score*10000//total,'edge_visits':answer.edge_visits,'candidates':answer.candidates,
                'global_optimum_proven':False,'elapsed_native_ns':str(elapsed),
                'library_sha256':self.sha256,'language_model_calls':0,'confirmed_reward':'0','chain_transaction':None,
                'execution_authority':'NONE','randomness_assurance':'CALLER_SEED_RESEARCH_FIXTURE_NOT_VRF'}
        result.update({'execution_backend':('SCALAR_CPP_VERIFIER' if raw['bits'] is not None else
                       'SANDBOXED_CPP_PIPELINE' if self.pipeline else 'SCALAR_CPP_SEARCH'),
                       'pipeline_sha256':self.pipeline.sha256 if self.pipeline else None})
        result['receipt_sha256']=digest(result);return result

    @staticmethod
    def seed(raw):
        if type(raw) is not str or not raw.isascii() or not raw.isdigit() or len(raw)>78 or (len(raw)>1 and raw[0]=='0') or int(raw)>=2**256:
            raise MachineError('SOLUTION_SEED_BOUND')
        return int(raw)
