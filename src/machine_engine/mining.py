"""Frozen useful-work jobs in the existing owner workspace; no signing authority."""
import ctypes as c
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, ident, require_keys

EDGE_FIELDS = ('pool_id', 'asset_in', 'asset_out', 'reserve_in', 'reserve_out', 'fee_ppm', 'cost')
JOB_FIELDS = ('asset_in', 'asset_out', 'amount_in', 'minimum_net', 'maximum_cost', 'maximum_impact_bps', 'maximum_hops')
LIMIT = (1 << 63) - 1
MODEL = 'SNAPSHOT_ROUTE_V1'


def integer(raw, minimum=0, maximum=LIMIT):
    if type(raw) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', raw):
        raise MachineError('MINING_CANONICAL_INTEGER_STRING_REQUIRED')
    value = int(raw)
    if not minimum <= value <= maximum:
        raise MachineError('MINING_INTEGER_BOUND')
    return value


def normalize(raw):
    require_keys(raw, {'model', 'title', 'source_root', 'source_description', 'constraints', 'edges', 'reward_units', 'bond_units'}, 'frozen mining job')
    if raw['model'] != MODEL:
        raise MachineError('MINING_MODEL_UNSUPPORTED')
    for key, limit in [('title', 100), ('source_description', 500)]:
        if not isinstance(raw[key], str) or not 1 <= len(raw[key]) <= limit or any(ord(ch) < 32 for ch in raw[key]):
            raise MachineError('MINING_TEXT_BOUND')
    if not isinstance(raw['source_root'], str) or not re.fullmatch(r'0x[0-9a-f]{64}', raw['source_root']) or int(raw['source_root'], 16) == 0:
        raise MachineError('MINING_SOURCE_ROOT_REQUIRED')
    require_keys(raw['constraints'], set(JOB_FIELDS), 'mining constraints')
    values = {k: integer(raw['constraints'][k]) for k in JOB_FIELDS}
    if not values['asset_in'] or not values['asset_out'] or values['asset_in'] == values['asset_out'] or not values['amount_in']:
        raise MachineError('MINING_ASSETS_REQUIRED')
    if not 1 <= values['maximum_hops'] <= 4 or values['maximum_impact_bps'] > 10000:
        raise MachineError('MINING_PATH_BOUND')
    if not isinstance(raw['edges'], list) or not 1 <= len(raw['edges']) <= 16:
        raise MachineError('MINING_EDGE_BOUND')
    for edge in raw['edges']:
        require_keys(edge, set(EDGE_FIELDS), 'mining edge')
        e = {k: integer(edge[k]) for k in EDGE_FIELDS}
        if not all(e[k] > 0 for k in EDGE_FIELDS[:5]) or e['asset_in'] == e['asset_out'] or e['fee_ppm'] >= 1000000:
            raise MachineError('MINING_EDGE_INVALID')
    integer(raw['reward_units'], 1); integer(raw['bond_units'], 1)
    return json.loads(canonical(raw))


def example():
    edges = [
        (1, 1, 3, 1000000000, 1000000000, 3000, 100),
        (2, 1, 2, 1000000000, 1200000000, 1000, 150),
        (3, 2, 3, 1000000000, 1000000000, 1000, 150),
    ]
    return {'model': MODEL, 'title': 'Find a lower-cost execution route',
            'source_root': '0x' + hashlib.sha256(b'SKEW synthetic frozen mining fixture v1').hexdigest(),
            'source_description': 'Synthetic fixture. Asset IDs 1/2/3 use normalized six-decimal units; not a live pool or token quote.',
            'constraints': dict(zip(JOB_FIELDS, map(str, (1, 3, 1000000, 1, 1000, 500, 4)), strict=True)),
            'edges': [dict(zip(EDGE_FIELDS, map(str, edge), strict=True)) for edge in edges],
            'reward_units': '10000', 'bond_units': '1000'}


class Edge(c.Structure):
    _fields_ = [(k, c.c_int64) for k in EDGE_FIELDS]


class Job(c.Structure):
    _fields_ = [('edges', Edge * 16), ('count', c.c_int64)] + [(k, c.c_int64) for k in JOB_FIELDS]


class Answer(c.Structure):
    _fields_ = [('path', c.c_int64 * 4)] + [(k, c.c_int64) for k in ('length', 'net', 'output', 'cost', 'visited', 'feasible', 'complete')]


class NativeMining:
    def __init__(self, library=None, expected=None):
        self.library = None; self.sha256 = None
        path = library or os.environ.get('ENGINE_MINING_LIBRARY')
        wanted = expected or os.environ.get('ENGINE_MINING_SHA256')
        if not path:
            return
        p = Path(path)
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_size > 10000000:
            raise MachineError('MINING_LIBRARY_PATH_REJECTED')
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if not wanted or sha != wanted:
            raise MachineError('MINING_LIBRARY_SHA256_MISMATCH')
        lib = c.CDLL(str(p))
        for name, restype in [('machine_mining_abi', c.c_uint), ('machine_mining_job_size', c.c_size_t), ('machine_mining_answer_size', c.c_size_t)]:
            fn = getattr(lib, name); fn.argtypes = []; fn.restype = restype
        if lib.machine_mining_abi() != 1 or lib.machine_mining_job_size() != c.sizeof(Job) or lib.machine_mining_answer_size() != c.sizeof(Answer):
            raise MachineError('MINING_ABI_MISMATCH')
        lib.machine_mining_search.argtypes = [c.POINTER(Job), c.c_int64, c.POINTER(Answer)]
        lib.machine_mining_score.argtypes = [c.POINTER(Job), c.POINTER(c.c_int64), c.c_int64, c.POINTER(Answer)]
        lib.machine_mining_search.restype = lib.machine_mining_score.restype = c.c_int
        self.library = lib; self.sha256 = sha

    def status(self):
        return {'enabled': self.library is not None, 'abi': 1, 'library_sha256': self.sha256,
                'model': MODEL, 'maximum_edges': 16, 'maximum_hops': 4, 'maximum_expansions': 70000,
                'language_model_calls': 0, 'execution_authority': 'NONE', 'public_mining_deployment': None,
                'rewards': 'REQUESTER_FUNDED_TOKEN_ESCROW_NOT_INFLATION', 'input_assurance': 'REQUESTER_DECLARED_FROZEN_MODEL'}

    def calculate(self, raw, *, path=None, budget=70000):
        raw = normalize(raw)
        if self.library is None:
            raise MachineError('MINING_LIBRARY_NOT_CONFIGURED')
        if type(budget) is not int or not 1 <= budget <= 70000:
            raise MachineError('MINING_SEARCH_BOUND')
        request = Job(); request.count = len(raw['edges'])
        for i, edge in enumerate(raw['edges']):
            request.edges[i] = Edge(*(int(edge[k]) for k in EDGE_FIELDS))
        for k in JOB_FIELDS:
            setattr(request, k, int(raw['constraints'][k]))
        result = Answer(); start = time.perf_counter_ns()
        if path is None:
            code = self.library.machine_mining_search(c.byref(request), budget, c.byref(result))
        else:
            if not isinstance(path, list) or not 1 <= len(path) <= 4 or any(type(i) is not int or not 0 <= i < request.count for i in path):
                raise MachineError('MINING_PATH_BOUND')
            indices = (c.c_int64 * 4)(*path)
            code = self.library.machine_mining_score(c.byref(request), indices, len(path), c.byref(result))
        elapsed = time.perf_counter_ns() - start
        body = {'model': MODEL, 'input_sha256': digest(raw), 'library_sha256': self.sha256,
                'valid': code == 0 and result.length > 0, 'native_code': code,
                'path': list(result.path[:result.length]) if code == 0 else [],
                'net_output': str(result.net), 'gross_output': str(result.output), 'cost': str(result.cost),
                'expansions': result.visited, 'feasible_candidates': result.feasible,
                'search_complete': bool(result.complete) if path is None else None,
                'elapsed_ns': str(elapsed), 'mode': 'SEARCH' if path is None else 'VERIFY_CANDIDATE',
                'language_model_calls': 0, 'execution_authority': 'NONE', 'chain_transaction': None,
                'assurance': 'FROZEN_MODEL_RESULT_NOT_LIVE_EVM_SIMULATION_OR_FINANCIAL_ADVICE'}
        body['receipt_sha256'] = digest(body)
        return body


class Mining:
    def __init__(self, work):
        self.work = work; self.native = NativeMining()
        with work.runtime.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS engine_mining_jobs (id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL, result TEXT)')

    def get(self, jid):
        ident(jid, 'mining job ID')
        with self.work.runtime.connect() as db:
            row = db.execute('SELECT * FROM engine_mining_jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise MachineError('MINING_JOB_NOT_FOUND')
        return {'id': row['id'], 'input_sha256': row['input_hash'], 'snapshot': json.loads(row['body']),
                'created': row['created'], 'status': 'UNPUBLISHED_DRAFT', 'confirmed_reward': '0',
                'result': json.loads(row['result']) if row['result'] else None}

    def create(self, raw):
        body = normalize(raw); jid = 'mining-' + secrets.token_hex(12)
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM engine_mining_jobs').fetchone()[0] >= 64:
                raise MachineError('MINING_WORKSPACE_JOB_LIMIT')
            db.execute('INSERT INTO engine_mining_jobs VALUES (?,?,?,?,NULL)', (jid, digest(body), canonical(body).decode(), int(self.work.clock())))
            self.work.event(db, 'MINING_JOB_FROZEN', {'id': jid, 'input_sha256': digest(body), 'authority': 'NONE'})
        return self.get(jid)

    def solve(self, jid, raw):
        require_keys(raw, {'budget'}, 'mining search budget')
        job = self.get(jid); result = self.native.calculate(job['snapshot'], budget=raw['budget'])
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE engine_mining_jobs SET result=? WHERE id=?', (canonical(result).decode(), jid))
            self.work.event(db, 'MINING_CANDIDATE_SEARCHED', {'id': jid, 'result': result})
        return result

    def status(self):
        with self.work.runtime.connect() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM engine_mining_jobs ORDER BY created DESC,rowid DESC LIMIT 64')]
        return self.native.status() | {'jobs': [self.get(jid) for jid in ids], 'example': example(), 'confirmed_reward': '0'}
