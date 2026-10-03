"""Owner-local bounded worker and crash-safe submission journal. Never signs or sends."""
import ctypes as c
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak

from economic_machine.values import MachineError, digest

from .solution import ALGORITHMS, Answer, Edge, Graph, graph
from .solution_client import reveal, seal

MAGIC = 0x534f4c32
_ADMISSION = threading.BoundedSemaphore(2)


class Frame(c.Structure):
    _fields_ = [('magic', c.c_uint32), ('version', c.c_uint32), ('id', c.c_uint64),
                ('graph', Graph), ('seed', c.c_uint64), ('budget', c.c_uint64),
                ('algorithm', c.c_uint32), ('reserved', c.c_uint32)]


class Result(c.Structure):
    _fields_ = [('magic', c.c_uint32), ('version', c.c_uint32), ('id', c.c_uint64),
                ('answer', Answer), ('elapsed_ns', c.c_uint64), ('status', c.c_uint32), ('reserved', c.c_uint32)]


class NativePipeline:
    def __init__(self, executable, expected_sha256, *, cpus=None):
        path = Path(executable)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 10000000:
            raise MachineError('SOLUTION_PIPELINE_PATH')
        if not expected_sha256 or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise MachineError('SOLUTION_PIPELINE_HASH')
        if c.sizeof(Frame) != 24240 or c.sizeof(Result) != 72:
            raise MachineError('SOLUTION_PIPELINE_ABI')
        self.executable, self.sha256 = str(path), expected_sha256
        self.cpus = cpus
        if cpus is not None and (not isinstance(cpus, (list, tuple)) or len(cpus) != 2
                or any(type(cpu) is not int or cpu not in os.sched_getaffinity(0) for cpu in cpus)):
            raise MachineError('SOLUTION_PIPELINE_CPU_AFFINITY')

    def search(self, jobs):
        if not _ADMISSION.acquire(blocking=False):
            raise MachineError('SOLUTION_PIPELINE_BUSY')
        try:
            return self._search(jobs)
        finally:
            _ADMISSION.release()

    def _search(self, jobs):
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 128:
            raise MachineError('SOLUTION_PIPELINE_JOB_BOUND')
        frames, all_edges = [], []
        for index, job in enumerate(jobs):
            if (not isinstance(job,dict) or set(job) != {'seed', 'problem', 'budget', 'algorithm', 'search_seed'}
                    or type(job['budget']) is not int or not 496 <= job['budget'] <= 10000000
                    or type(job['search_seed']) is not int or not 0 <= job['search_seed'] < 2**64
                    or not isinstance(job['algorithm'],str) or job['algorithm'] not in {'random', 'greedy', 'integer_anneal'}):
                raise MachineError('SOLUTION_PIPELINE_JOB')
            edges = graph(job['seed'], job['problem'])
            frame = Frame(); frame.magic = MAGIC; frame.version = 1; frame.id = index
            frame.graph.nodes = 32; frame.graph.count = len(edges)
            for i, edge in enumerate(edges):
                frame.graph.edges[i] = Edge(*edge)
            frame.seed = job['search_seed']; frame.budget = job['budget']; frame.algorithm = ALGORITHMS[job['algorithm']]
            frames.append(bytes(frame)); all_edges.append(edges)
        command = [self.executable]
        if self.cpus is not None:
            command += ['--search-cpu', str(self.cpus[0]), '--verify-cpu', str(self.cpus[1])]
        try:
            run = subprocess.run(command, input=b''.join(frames), capture_output=True, timeout=30, check=False,
                                 env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise MachineError('SOLUTION_PIPELINE_TIMEOUT') from None
        if run.returncode or len(run.stdout) != len(jobs) * c.sizeof(Result):
            raise MachineError('SOLUTION_PIPELINE_PROCESS_FAILED')
        results = []
        for index, (job, edges) in enumerate(zip(jobs, all_edges)):
            offset = index * c.sizeof(Result)
            output = Result.from_buffer_copy(run.stdout[offset:offset+c.sizeof(Result)])
            bits, score = output.answer.bits, output.answer.score
            replay = sum(w for u, v, w in edges if ((bits >> u) ^ (bits >> v)) & 1)
            if (output.magic != MAGIC or output.version != 1 or output.id != index or output.status or output.reserved
                    or not output.answer.valid or bits & 1 or bits >= 2**32 or score != replay
                    or output.answer.edge_visits > job['budget'] or output.answer.complete):
                raise MachineError('SOLUTION_PIPELINE_RESULT_REJECTED')
            result = {'seed': str(job['seed']), 'problem': str(job['problem']), 'bits': str(bits), 'score': str(score),
                      'total_weight': str(sum(w for _, _, w in edges)), 'graph_sha256': digest(edges),
                      'edge_visits': output.answer.edge_visits, 'elapsed_native_ns': str(output.elapsed_ns),
                      'candidates':output.answer.candidates,
                      'global_optimum_proven': False, 'pipeline_sha256': self.sha256, 'language_model_calls': 0}
            result['receipt_sha256'] = digest(result); results.append(result)
        return results


def private_directory(path):
    directory = Path(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise MachineError('SOLUTION_PRIVATE_DIRECTORY_REQUIRED')
    return directory


class MiningJournal:
    """FULL WAL transactions reserve work once; unknown handoffs are never auto-retried."""
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.path = self.directory / 'miner.sqlite'
        if self.path.is_symlink():
            raise MachineError('SOLUTION_JOURNAL_SYMLINK')
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise MachineError('SOLUTION_PRIVATE_JOURNAL_REQUIRED')
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS budgets(day INTEGER PRIMARY KEY, reserved INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, error TEXT, updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id),
                action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, tx_hash TEXT UNIQUE,
                outcome TEXT, UNIQUE(job,action));
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, intent TEXT NOT NULL REFERENCES intents(id),
                seen_at INTEGER NOT NULL, outcome TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def require_active(self):
        # A restored snapshot cannot prove that a second machine stopped or that
        # later handoffs/spending did not occur. Reads and reconciliation remain
        # available; new work/export needs an explicit recovery review.
        marker = self.directory / 'RECOVERY_HOLD'
        persisted = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_control'").fetchone()
        if (marker.exists() or marker.is_symlink() or (persisted and
                self.db.execute('SELECT held FROM recovery_control WHERE id=1').fetchone() != (0,))):
            raise MachineError('SOLUTION_RECOVERY_RECONCILIATION_REQUIRED')

    def reserve(self, binding, budget, daily_limit, *, now=None):
        self.require_active()
        if type(budget) is not int or not 496 <= budget <= 10000000 or type(daily_limit) is not int or not budget <= daily_limit <= 1000000000:
            raise MachineError('SOLUTION_DAILY_WORK_BOUND')
        now = int(time.time()) if now is None else now
        if type(now) is not int or now < 0:
            raise MachineError('SOLUTION_CLOCK')
        day = now // 86400; job = digest(binding)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM jobs WHERE id=?', (job,)).fetchone():
                self.db.execute('ROLLBACK'); return None
            last = self.db.execute('SELECT MAX(day) FROM budgets').fetchone()[0]
            if last is not None and day < last:
                raise MachineError('SOLUTION_CLOCK_ROLLBACK')
            reserved = self.db.execute('SELECT reserved FROM budgets WHERE day=?', (day,)).fetchone()
            if (reserved[0] if reserved else 0) + budget > daily_limit:
                raise MachineError('SOLUTION_DAILY_BUDGET_EXHAUSTED')
            self.db.execute('INSERT INTO budgets VALUES(?,?) ON CONFLICT(day) DO UPDATE SET reserved=reserved+excluded.reserved', (day,budget))
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,NULL,NULL,?)', (job,json.dumps(binding,sort_keys=True),'RESERVED',now))
            self.db.execute('COMMIT'); return job
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def solved(self, job, result, *, qualified=True):
        state = 'SOLVED' if qualified else 'BELOW_THRESHOLD'
        changed = self.db.execute('UPDATE jobs SET result=?,state=?,updated=? WHERE id=? AND state=?',
                                 (json.dumps(result,sort_keys=True),state,int(time.time()),job,'RESERVED')).rowcount
        if changed != 1:
            raise MachineError('SOLUTION_JOB_TRANSITION')

    def fail(self, job):
        self.db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state=?',
                        ('FAILED','WORKER_FAILED',int(time.time()),job,'RESERVED'))

    def job(self, job):
        row = self.db.execute('SELECT binding,state,result FROM jobs WHERE id=?', (job,)).fetchone()
        if not row:
            raise MachineError('SOLUTION_JOB_REQUIRED')
        return {'binding':json.loads(row[0]),'state':row[1],'result':json.loads(row[2]) if row[2] else None}

    def prepare(self, job, action, payload):
        self.require_active()
        if action not in {'commit','reveal','claim'}:
            raise MachineError('SOLUTION_INTENT_ACTION')
        identifier = digest({'job':job,'action':action})
        existing = self.db.execute('SELECT payload FROM intents WHERE id=?',(identifier,)).fetchone()
        if existing:
            if json.loads(existing[0]) != payload:
                raise MachineError('SOLUTION_INTENT_IMMUTABLE')
            return identifier
        if self.job(job)['state'] != 'SOLVED':
            raise MachineError('SOLUTION_SOLVED_JOB_REQUIRED')
        if action == 'reveal':
            commit = self.db.execute('SELECT state FROM intents WHERE job=? AND action=?',(job,'commit')).fetchone()
            if not commit or commit[0] not in {'CONFIRMED','PENDING_FINALITY'}:
                raise MachineError('SOLUTION_CANONICAL_COMMIT_REQUIRED')
        self.db.execute('INSERT INTO intents VALUES(?,?,?,?,?,NULL,NULL)',
                        (identifier,job,action,json.dumps(payload,sort_keys=True),'UNSIGNED'))
        return identifier

    def intent(self, identifier):
        row = self.db.execute('SELECT job,action,payload,state,tx_hash FROM intents WHERE id=?',(identifier,)).fetchone()
        if not row:
            raise MachineError('SOLUTION_INTENT_REQUIRED')
        return {'id':identifier,'job':row[0],'action':row[1],'payload':json.loads(row[2]),'state':row[3],'tx_hash':row[4]}

    def handoff(self, identifier):
        self.require_active()
        # Persist before any owner export. A crash after this cannot return to UNSIGNED automatically.
        if self.db.execute('UPDATE intents SET state=? WHERE id=? AND state=?',('HANDED_OFF',identifier,'UNSIGNED')).rowcount != 1:
            raise MachineError('SOLUTION_HANDOFF_ONCE')
        return self.intent(identifier)['payload']

    def record_hash(self, identifier, tx_hash):
        if not isinstance(tx_hash,str) or len(tx_hash)!=66 or not tx_hash.startswith('0x'):
            raise MachineError('SOLUTION_TX_HASH')
        try:
            bytes.fromhex(tx_hash[2:])
        except ValueError:
            raise MachineError('SOLUTION_TX_HASH') from None
        if self.db.execute('UPDATE intents SET state=?,tx_hash=? WHERE id=? AND state=? AND tx_hash IS NULL',
                           ('UNKNOWN',tx_hash.lower(),identifier,'HANDED_OFF')).rowcount != 1:
            raise MachineError('SOLUTION_TX_ALREADY_BOUND')

    def reconcile(self, identifier, chain):
        intent = self.intent(identifier)
        if intent['tx_hash'] is None:
            raise MachineError('SOLUTION_TX_HASH_REQUIRED_NO_RETRY')
        try:
            outcome = chain.outcome(intent['tx_hash'], intent['payload'])
        except MachineError as error:
            outcome={'state':'HELD','tx_hash':intent['tx_hash'],'reason':str(error)}
        if outcome['state'] not in {'HELD','UNKNOWN','PENDING_FINALITY','PENDING_REVERT','CONFIRMED','REVERTED','ORPHANED'}:
            raise MachineError('SOLUTION_OUTCOME_STATE')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            raw=json.dumps(outcome,sort_keys=True)
            self.db.execute('INSERT INTO observations(intent,seen_at,outcome) VALUES(?,?,?)',(identifier,int(time.time()),raw))
            self.db.execute('UPDATE intents SET state=?,outcome=? WHERE id=?',(outcome['state'],raw,identifier))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise
        return outcome

    def status(self):
        # Never return binding, calldata or salts through monitoring.
        return {'jobs':dict(self.db.execute('SELECT state,count(*) FROM jobs GROUP BY state')),
                'intents':dict(self.db.execute('SELECT state,count(*) FROM intents GROUP BY state')),
                'automatic_signing':False,'automatic_broadcast':False}


class LocalMiner:
    def __init__(self, journal, chain, pipeline, miner, *, budget=100000, daily_limit=10000000):
        from .mining_client import binding
        _, _, _, self.miner = binding(chain.contract,chain.chain_id,1,miner)
        self.journal,self.chain,self.pipeline = journal,chain,pipeline
        self.budget,self.daily_limit = budget,daily_limit

    def tick(self, *, now=None):
        now = int(time.time()) if now is None else now
        snapshot = self.chain.snapshot()
        if (not snapshot['round'] or snapshot.get('state') != 2 or snapshot['admission_paused']
                or now + 60 >= snapshot['commit_end'] or now < snapshot['anchor']['timestamp']
                or now - snapshot['anchor']['timestamp'] > 120):
            return {'state':'WAITING_FOR_FRESH_COMMIT_WINDOW', 'authority':'NONE'}
        completed = 0
        for problem in range(16):
            binding = {k:snapshot[k] for k in ['chain','contract','code_sha256','round','seed','threshold']}
            binding.update({'miner':self.miner,'problem':problem})
            job = self.journal.reserve(binding,self.budget,self.daily_limit,now=now)
            if job is None:
                continue
            try:
                result, = self.pipeline.search([{'seed':snapshot['seed'],'problem':problem,'budget':self.budget,
                    'algorithm':'integer_anneal','search_seed':int(job[:16],16)}])
                qualified = int(result['score'])*10000 >= int(result['total_weight'])*snapshot['threshold']
                self.journal.solved(job,result,qualified=qualified); completed += 1
            except Exception:
                self.journal.fail(job); raise
        return {'state':'CANDIDATES_SAVED','jobs':completed,'authority':'NONE'}

    def prepare(self, job, action, *, now=None):
        self.journal.require_active()
        now = int(time.time()) if now is None else now
        record = self.journal.job(job); bound = record['binding']
        snapshot = self.chain.snapshot(round_id=bound['round'],finalized=action=='claim')
        if (record['state'] != 'SOLVED' or any(snapshot.get(k) != bound[k]
                for k in ['chain','contract','code_sha256','round','seed','threshold']) or snapshot['state'] != (3 if action=='claim' else 2)
                or now < snapshot['anchor']['timestamp'] or now - snapshot['anchor']['timestamp'] > (3600 if action=='claim' else 120)):
            raise MachineError('SOLUTION_FRESH_BOUND_ROUND_REQUIRED')
        existing = self.journal.db.execute('SELECT id,state FROM intents WHERE job=? AND action=?',(job,action)).fetchone()
        path = self.journal.directory / (job + '.secret.json')
        if action == 'claim':
            winner=self.chain.winner(snapshot,bound['problem'])
            if (winner['miner'].lower()!=self.miner.lower() or winner['claimed']
                    or winner['bits']!=int(record['result']['bits']) or winner['score']!=int(record['result']['score'])):
                raise MachineError('SOLUTION_UNCLAIMED_WINNER_REQUIRED')
            if existing:
                return {'intent_id':existing[0],'state':existing[1],'authority':'NONE'}
            reward=self.chain.rewards(snapshot['anchor'],self.miner)
            data=keccak(text='claim(uint256,uint8)')[:4]+encode(['uint256','uint8'],[bound['round'],bound['problem']])
            payload={'status':'UNSIGNED_OFFLINE','transaction':{'to':bound['contract'],'from':self.miner,
                'chainId':bound['chain'],'value':'0x0','data':'0x'+data.hex()},
                'expected_reward':{'token':reward['token'],'round':bound['round'],'problem':bound['problem'],'amount':str(10**18)}}
        elif action == 'commit':
            if snapshot['admission_paused'] or now + 60 >= snapshot['commit_end']:
                raise MachineError('SOLUTION_COMMIT_WINDOW_MARGIN')
            if existing:
                self.chain.check_anchor(snapshot['anchor'])
                return {'intent_id':existing[0],'state':existing[1],'authority':'NONE'}
            raw = {'seed':str(bound['seed']),'problem':str(bound['problem']),'budget':str(self.budget),
                   'search_seed':'42','algorithm':'integer_anneal','bits':record['result']['bits']}
            if not path.exists():
                payload = seal(raw,contract=bound['contract'],round_id=bound['round'],miner=self.miner,secret_file=path)
            else:
                # Recovery after fsync(secret) but before database intent commit. Validate secret before reuse.
                reveal(path)
                secret = json.loads(path.read_text())
                if any(secret[k] != v for k,v in {'contract':bound['contract'],'chain':bound['chain'],
                    'round':str(bound['round']),'problem':bound['problem'],'miner':self.miner,
                    'bits':record['result']['bits'],'graph_sha256':record['result']['graph_sha256']}.items()):
                    raise MachineError('SOLUTION_RECOVERY_BINDING')
                data = keccak(text='commit(uint256,uint8,bytes32)')[:4] + encode(['uint256','uint8','bytes32'],
                    [bound['round'],bound['problem'],bytes.fromhex(secret['commitment'])])
                payload = {'status':'UNSIGNED_OFFLINE','transaction':{'to':bound['contract'],'from':self.miner,
                    'chainId':bound['chain'],'value':'0x0','data':'0x'+data.hex()},'commitment':'0x'+secret['commitment'],
                    'graph_sha256':secret['graph_sha256'],'chain_round_verified':False,
                    'required_before_signature':'Read the actual VRF-backed round seed/threshold/window, compare graph; owner signing only.'}
        elif action == 'reveal':
            if now < snapshot['commit_end'] or now + 60 >= snapshot['reveal_end']:
                raise MachineError('SOLUTION_REVEAL_WINDOW_MARGIN')
            commit_id = digest({'job':job,'action':'commit'})
            outcome = self.journal.reconcile(commit_id,self.chain)
            if outcome['state'] not in {'CONFIRMED','PENDING_FINALITY'} or outcome['block'] > snapshot['anchor']['number']:
                raise MachineError('SOLUTION_CANONICAL_COMMIT_REQUIRED')
            canonical = self.chain.block(self.chain.pair('eth_getBlockByNumber',[hex(outcome['block']),False]))
            if canonical['hash'] != outcome['block_hash']:
                raise MachineError('SOLUTION_COMMIT_REORG_BEFORE_REVEAL')
            if existing:
                self.chain.check_anchor(snapshot['anchor'])
                return {'intent_id':existing[0],'state':existing[1],'authority':'NONE'}
            payload = reveal(path)
        else:
            raise MachineError('SOLUTION_INTENT_ACTION')
        # Offline payload stays explicitly unsigned; the finalized anchor is part of the intent.
        payload = payload | {'verified_anchor':snapshot['anchor'],'chain_round_verified':True}
        self.chain.check_anchor(snapshot['anchor'])
        return {'intent_id':self.journal.prepare(job,action,payload),'state':'UNSIGNED', 'authority':'NONE'}
