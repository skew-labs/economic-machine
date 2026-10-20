"""Public bridge leads with durable gaps; never an exchange customer ledger.

Wormhole chain IDs are namespaced and are NOT EVM chain IDs. A parsed VAA is
not a verified guardian quorum, canonical transaction, or completed redemption.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zlib

from .archive import NoRedirect
from .store import address, atomic, canonical, decode, encode

HOST = 'https://api.wormholescan.io'
MAX_RESPONSE = 256 * 1024
PAGE_SIZE = 20
WINDOW_MS = 7 * 86400000
START_MS = 1705590000000  # 2024-01-19 00:00 KST; the existing research window.
END_MS = 1792076399999  # 2026-10-15 23:59:59.999 KST.
# Only address encodings implemented here are eligible for recursive lookup.
EVM = {2, 4, 5, 6, 7, 10, 13, 14, 16, 23, 24, 30, 34, 35, 36, 37, 38, 39, 40, 43, 44, 45}
NAMES = {1: 'Solana', 2: 'Ethereum', 4: 'BNB Chain', 5: 'Polygon', 6: 'Avalanche',
         23: 'Arbitrum', 24: 'Optimism', 30: 'Base'}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def key(chain, value):
    if type(chain) is not int or not 0 < chain < 65536:
        raise ValueError('XCHAIN_CHAIN_ID')
    if chain == 1 and address(value):
        return value
    if chain in EVM and isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        return value.lower()
    raise ValueError('XCHAIN_ADDRESS_ENCODING_UNSUPPORTED')


def raw_key(chain, value):
    value = key(chain, value)
    return decode(value) if chain == 1 else bytes.fromhex(value[2:]).rjust(32, b'\0')


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(d.timestamp() * 1000) if d.tzinfo else None
    except ValueError:
        return None


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_vaa(value):
    if not isinstance(value, str) or len(value) > 100000:
        raise ValueError('XCHAIN_VAA_BOUND')
    raw = base64.b64decode(value, validate=True)
    if len(raw) < 57 or raw[0] != 1 or not 1 <= raw[5] <= 32:
        raise ValueError('XCHAIN_VAA_HEADER')
    pos = 6 + raw[5] * 66
    if len(raw) < pos + 51:
        raise ValueError('XCHAIN_VAA_TRUNCATED')
    indexes = [raw[6 + i * 66] for i in range(raw[5])]
    if indexes != sorted(set(indexes)):
        raise ValueError('XCHAIN_VAA_SIGNATURE_ORDER')
    body = raw[pos:]
    chain = int.from_bytes(body[8:10], 'big')
    emitter = body[10:42].hex()
    sequence = str(int.from_bytes(body[42:50], 'big'))
    return dict(id=f'{chain}/{emitter}/{sequence}', emitter_chain=chain,
                emitter_address=emitter, sequence=sequence, stamp=int.from_bytes(body[:4], 'big') * 1000,
                payload=body[51:], raw_sha256=sha(raw), body_sha256=sha(body),
                guardian_set=int.from_bytes(raw[1:5], 'big'), signature_count=raw[5])


def normalize(operation, query_chain, query_address, lower, upper):
    """Bind provider fields to decoded bytes; all results remain provider leads."""
    p = operation.get('content', {}).get('standarizedProperties', {})
    source, target = operation.get('sourceChain') or {}, operation.get('targetChain') or {}
    sc, dc = p.get('fromChain'), p.get('toChain')
    src, dst = key(sc, p.get('fromAddress')), key(dc, p.get('toAddress'))
    query_address = key(query_chain, query_address)
    if (query_chain, query_address) == (sc, src):
        direction = 'outbound'
    elif (query_chain, query_address) == (dc, dst):
        direction = 'inbound_context_only'
    else:
        raise ValueError('XCHAIN_QUERY_ADDRESS_MISMATCH')
    vaa = parse_vaa(operation.get('vaa', {}).get('raw'))
    if (operation.get('id') != vaa['id'] or operation.get('emitterChain') != sc or
            vaa['emitter_chain'] != sc or source.get('chainId') != sc or
            operation.get('emitterAddress', {}).get('hex', '').lower() != vaa['emitter_address'] or
            str(operation.get('sequence')) != vaa['sequence']):
        raise ValueError('XCHAIN_MESSAGE_ID_MISMATCH')
    payload = vaa.pop('payload')
    # Wrapped Token Transfer layouts only. NTT / application payloads stay in
    # the preserved page with an explicit unsupported reason, never guessed.
    if len(payload) < 133 or payload[0] not in (1, 3) or (payload[0] == 1 and len(payload) != 133):
        raise ValueError('XCHAIN_PAYLOAD_UNSUPPORTED')
    if (int.from_bytes(payload[99:101], 'big') != dc or payload[67:99] != raw_key(dc, dst) or
            int.from_bytes(payload[1:33], 'big') != int(p.get('amount', '-1'))):
        raise ValueError('XCHAIN_PAYLOAD_FIELDS_MISMATCH')
    if int.from_bytes(payload[65:67], 'big') != p.get('tokenChain'):
        raise ValueError('XCHAIN_TOKEN_CHAIN_MISMATCH')
    if payload[33:65] != raw_key(p['tokenChain'], p.get('tokenAddress')):
        raise ValueError('XCHAIN_TOKEN_ADDRESS_MISMATCH')
    # Payload 1 does not authenticate the depositor. Payload 3's sender may be
    # an application; it is retained but not treated as the final human owner.
    sent = timestamp(source.get('timestamp'))
    received = timestamp(target.get('timestamp'))
    if sent is not None and not lower <= sent <= upper:
        raise ValueError('XCHAIN_OUTSIDE_TIME_WINDOW')
    if target and target.get('chainId') != dc:
        raise ValueError('XCHAIN_TARGET_CHAIN_MISMATCH')
    if received is not None and (received > upper or sent is not None and received < sent):
        raise ValueError('XCHAIN_TARGET_TIME_MISMATCH')
    tx = source.get('transaction', {}).get('txHash')
    target_tx = target.get('transaction', {}).get('txHash')
    if not isinstance(tx, str) or not 32 <= len(tx) <= 128:
        raise ValueError('XCHAIN_SOURCE_TRANSACTION_REQUIRED')
    return dict(operation_id=vaa['id'], source_chain=sc, source_address=src, target_chain=dc,
                target_address=dst, source_tx=tx, target_tx=target_tx, source_stamp=sent,
                target_stamp=received, direction=direction, vaa=vaa,
                amount=p['amount'], token_chain=p['tokenChain'], token_address=p['tokenAddress'],
                provider_source_status=source.get('status'), provider_target_status=target.get('status'),
                provider_duplicate_vaa=operation.get('vaa', {}).get('isDuplicated', False),
                message_fields_match=True, guardian_signatures_verified=False,
                trusted_emitter_verified=False, canonical_source_verified=False,
                canonical_redemption_verified=False, customer_identity_proven=False,
                same_funds_continuity_proven=False, custody_gap_resolved=False,
                source_tier='public_bridge_index_claim', temporal_order_verified=False,
                status='UNVERIFIED_BRIDGE_LEAD')


class CrossChain:
    def __init__(self, graph):
        self.g = graph
        self.folder = graph.out / 'crosschain-objects'
        self.folder.mkdir(exist_ok=True)
        graph.db.executescript('''
          CREATE TABLE IF NOT EXISTS crosschain_jobs(job TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS crosschain_pages(job TEXT,page INTEGER,body TEXT,PRIMARY KEY(job,page));
          CREATE TABLE IF NOT EXISTS crosschain_legs(id TEXT PRIMARY KEY,job TEXT,root TEXT,operation TEXT,body TEXT);
          CREATE INDEX IF NOT EXISTS crosschain_message ON crosschain_legs(operation);
        ''')

    def enqueue(self, root, anchor, chain, account, after_ms=None, *, origin_edge=None, parent_leg=None, depth=0):
        account = key(chain, account)
        ident = sha(canonical(['wormhole', root, chain, account, after_ms, origin_edge, parent_leg]).encode())[:24]
        value = dict(chain=chain, address=account, after_ms=after_ms, origin_edge=origin_edge,
                     parent_leg=parent_leg, snapshot_end_ms=min(time.time_ns() // 1000000, END_MS),
                     temporal_anchor_known=after_ms is not None)
        self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                          (ident, root, anchor, None, after_ms, depth, 'pending', 'null', 'wormhole_forward'))
        self.g.db.execute('INSERT OR IGNORE INTO crosschain_jobs VALUES(?,?)', (ident, canonical(value)))
        return self.g.custody.attach(ident)

    def seed(self, guard=lambda: None):
        guard()
        with self.g.db:
            for case in self.g.db.execute('SELECT * FROM custody_cases').fetchall():
                if not self.g.custody.valid_case(case['id']):
                    continue
                c = json.loads(case['body'])
                j = self.enqueue(case['root'], case['account'], 1, case['account'], c.get('entry_stamp'),
                                 origin_edge=case['entry_edge'])
                self.g.custody.attach(j, [case['id']])
        # Incremental provenance rows: launch actors, early buyers, transfers,
        # and custody-associated observations. Ordinary later trading rows do
        # not cause millions of duplicate external requests.
        last = self.g.state('crosschain_seed_row', 0)
        rows = self.g.db.execute('SELECT rowid,* FROM edges WHERE rowid>? ORDER BY rowid LIMIT 2000', (last,)).fetchall()
        with self.g.db:
            for r in rows:
                b = json.loads(r['body'])
                if b.get('dead_branch'):
                    continue
                accounts = []
                if r['kind'] in ('create', 'launch_payer', 'early_buy', 'provider_creator_claim', 'dataset_early_buy'):
                    accounts.append(r['source'])
                if r['kind'] in ('funding_record', 'sol_transfer', 'indexed_transfer'):
                    if not b.get('token_account_not_owner'):
                        accounts.append(r['target'])
                for a in accounts:
                    if not address(a):
                        continue  # Legacy invalid-address observations stay preserved in graph.
                    # Preserve the first independent origin. Further source
                    # records stay in graph; they do not reset completed pages.
                    existing = self.g.db.execute('''SELECT 1 FROM crosschain_jobs c JOIN frontier f ON f.id=c.job
                        WHERE f.root=? AND json_extract(c.body,'$.chain')=1
                        AND json_extract(c.body,'$.address')=? LIMIT 1''', (r['root'], a)).fetchone()
                    if existing:
                        continue
                    j = self.enqueue(r['root'], a, 1, a, r['stamp'], origin_edge=r['id'])
                    cases = [x[0] for x in self.g.db.execute('SELECT case_id FROM custody_annotations WHERE edge=?', (r['id'],))]
                    self.g.custody.attach(j, cases)
            if rows:
                self.g.set('crosschain_seed_row', rows[-1]['rowid'])
        return len(rows)

    def valid_job(self, job, visited=None):
        visited = set() if visited is None else visited
        if job in visited:
            return False
        r = self.g.db.execute('SELECT body FROM crosschain_jobs WHERE job=?', (job,)).fetchone()
        if not r:
            return False
        b = json.loads(r[0])
        if b.get('origin_edge'):
            e = self.g.db.execute('SELECT body FROM edges WHERE id=?', (b['origin_edge'],)).fetchone()
            if not e or json.loads(e[0]).get('dead_branch'):
                return False
        if b.get('parent_leg'):
            p = self.g.db.execute('SELECT job,operation FROM crosschain_legs WHERE id=?', (b['parent_leg'],)).fetchone()
            if not p or self.conflicted(p[1]) or not self.valid_job(p[0], visited | {job}):
                return False
        return True

    def conflicted(self, operation):
        bodies = [json.loads(r[0]) for r in self.g.db.execute('SELECT body FROM crosschain_legs WHERE operation=?', (operation,))]
        return len({r['vaa']['body_sha256'] for r in bodies}) > 1 or any(r.get('provider_duplicate_vaa') for r in bodies)

    def fetch(self, url, scope):
        if not url.startswith(HOST + '/api/v1/operations?'):
            raise ValueError('XCHAIN_SOURCE_NOT_ALLOWED')
        scope.reserve(MAX_RESPONSE + 1, 'wormhole-operations:' + sha(url.encode()))
        req = urllib.request.Request(url, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                                                   'User-Agent': 'EconomicMachineResearch/0.2'})
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as r:
            raw = r.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('XCHAIN_RESPONSE_BOUND')
        digest = sha(raw)
        path = self.folder / (digest + '.json.zlib')
        if not path.exists():
            with path.open('xb') as f:
                f.write(zlib.compress(raw, 9))
                f.flush()
                import os
                os.fsync(f.fileno())
        elif sha(zlib.decompress(path.read_bytes())) != digest:
            raise ValueError('XCHAIN_CACHE_HASH')
        receipt = dict(url=url, sha256=digest, bytes=len(raw), at_ms=time.time_ns() // 1000000)
        # Preserve request provenance even if parsing or DB commit fails.
        name = f"{receipt['at_ms']}-{sha(url.encode())[:16]}-{digest[:16]}.receipt.json"
        atomic(self.folder / name, receipt)
        return raw, receipt

    def run(self, job, scope):
        scope.guard(MAX_RESPONSE, force=True)
        if not self.valid_job(job['id']):
            with self.g.db:
                self.g.db.execute("UPDATE frontier SET state='crosschain_anchor_revoked' WHERE id=?", (job['id'],))
            return dict(job_id=job['id'], state='crosschain_anchor_revoked')
        meta = json.loads(self.g.db.execute('SELECT body FROM crosschain_jobs WHERE job=?', (job['id'],)).fetchone()[0])
        cursor = json.loads(job['cursor']) or {}
        cursor = cursor if isinstance(cursor, dict) else dict(page=cursor, ordinal=cursor)
        page, ordinal = cursor.get('page', 0), cursor.get('ordinal', 0)
        lower = max(START_MS, meta.get('after_ms') or START_MS)
        upper = meta['snapshot_end_ms']
        window_end = min(cursor.get('window_end_ms', upper), upper)
        window_start = max(lower, window_end - WINDOW_MS)
        if window_start > window_end:
            with self.g.db:
                self.g.db.execute("UPDATE frontier SET state='bridge_snapshot_exhausted' WHERE id=?", (job['id'],))
            return dict(job_id=job['id'], operations=0, source_complete=False, outside_research_window=True)
        params = dict(address=meta['address'], includesChain=meta['chain'], page=page, pageSize=PAGE_SIZE,
                      **{'from': iso(window_start), 'to': iso(window_end)})
        raw, receipt = self.fetch(HOST + '/api/v1/operations?' + urllib.parse.urlencode(params), scope)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('operations'), list) or len(data['operations']) > PAGE_SIZE:
            raise ValueError('XCHAIN_PAGE_SCHEMA')
        observations = data['operations']
        prior = self.g.db.execute('SELECT body FROM crosschain_pages WHERE job=? AND page<?', (job['id'], ordinal)).fetchall()
        if observations and any(json.loads(r[0])['sha256'] == receipt['sha256'] and
                                json.loads(r[0]).get('window_end_ms') == window_end for r in prior):
            raise ValueError('XCHAIN_REPEATED_PAGE_PRESERVED')
        accepted, errors = [], []
        for i, item in enumerate(observations):
            try:
                lead = normalize(item, meta['chain'], meta['address'], window_start, window_end)
                lead.update(page_receipt=receipt, row=i, parent_leg=meta.get('parent_leg'),
                            temporal_anchor_known=meta['temporal_anchor_known'], custody_case_ids=sorted(self.g.custody.active))
                accepted.append(lead)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                errors.append(dict(row=i, reason=str(e)[:100], error_type=type(e).__name__))
        with self.g.db:
            for lead in accepted:
                ident = sha(canonical([job['id'], lead['operation_id'], lead['vaa']['body_sha256']]).encode())
                self.g.db.execute('INSERT OR IGNORE INTO crosschain_legs VALUES(?,?,?,?,?)',
                                  (ident, job['id'], job['root'], lead['operation_id'], canonical(lead)))
            for lead in accepted:
                ident = sha(canonical([job['id'], lead['operation_id'], lead['vaa']['body_sha256']]).encode())
                if (lead['direction'] != 'outbound' or not lead['target_tx'] or lead['target_stamp'] is None or
                        lead['source_stamp'] is None or self.conflicted(lead['operation_id']) or
                        lead['provider_source_status'] not in ('confirmed', 'completed', 'success', 'finalized') or
                        lead['provider_target_status'] not in ('confirmed', 'completed', 'success', 'finalized')):
                    continue
                # Continue only as an unverified lead. CEX gaps are inherited.
                self.enqueue(job['root'], job['wallet'], lead['target_chain'], lead['target_address'],
                             lead['target_stamp'], parent_leg=ident, depth=job['depth'] + 1)
                if lead['target_chain'] == 1:
                    # Source observation is a provider claim. Use a distinct
                    # provenance-bearing job instead of polluting verified graph.
                    self.g.set('crosschain_return:' + ident, dict(address=lead['target_address'],
                        after_ms=lead['target_stamp'], status='AWAITING_SOLANA_REDEMPTION_EVIDENCE',
                        same_funds_continuity_proven=False))
            receipt.update(page=page, ordinal=ordinal, window_start_ms=window_start, window_end_ms=window_end,
                           operations=len(observations), accepted_leads=len(accepted),
                           rejected_or_unsupported=errors, historical_completeness_proven=False)
            self.g.db.execute('INSERT OR IGNORE INTO crosschain_pages VALUES(?,?,?)', (job['id'], ordinal, canonical(receipt)))
            more = len(observations) == PAGE_SIZE
            next_cursor = dict(page=page + 1 if more else 0, ordinal=ordinal + 1,
                               window_end_ms=window_end if more else window_start - 1)
            self.g.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',
                              (canonical(next_cursor), 'pending' if more or window_start > lower else 'bridge_snapshot_exhausted', job['id']))
        return dict(job_id=job['id'], operations=len(observations), candidate_legs=len(accepted),
                    unsupported_or_rejected=len(errors), source_complete=False, custody_gap_resolved=False)

    def snapshot(self):
        rows = self.g.db.execute('SELECT * FROM crosschain_legs ORDER BY rowid DESC LIMIT 300').fetchall()
        legs = [dict(id=r['id'], root=r['root'], job=r['job'], **json.loads(r['body']),
                     revoked=not self.valid_job(r['job']), conflicting_message=self.conflicted(r['operation'])) for r in rows]
        path = self.g.out / 'address-index.sqlite3'
        if path.exists():
            db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
            try:
                for leg in legs:
                    if leg['target_chain'] != 1 or not leg.get('target_tx'):
                        continue
                    try:
                        signature = decode(leg['target_tx'])
                    except (ValueError, TypeError):
                        continue
                    if len(signature) != 64:
                        continue
                    found = db.execute('''SELECT t.slot,t.tx_order,t.successful,b.hash,b.state,t.id
                        FROM transactions t JOIN blocks b ON b.id=t.block WHERE t.signature=? LIMIT 8''',
                        (signature,)).fetchall()
                    leg['solana_return_index_matches'] = [dict(slot=r[0],tx_order=r[1],successful=r[2],
                        source_sha256=r[3],evidence_tier=r[4],index_transaction_id=r[5],
                        redemption_semantics_verified=False) for r in found]
            finally:
                db.close()
        states = dict(self.g.db.execute("SELECT state,count(*) FROM frontier WHERE source='wormhole_forward' GROUP BY state"))
        return dict(schema='economic-machine.public-crosschain-leads.v1', at_ms=time.time_ns() // 1000000,
                    summary=dict(job_states=states, pages=self.g.db.execute('SELECT count(*) FROM crosschain_pages').fetchone()[0],
                                 candidate_legs=self.g.db.execute('SELECT count(*) FROM crosschain_legs').fetchone()[0],
                                 independently_verified_routes=0, resolved_cex_customer_gaps=0),
                    legs=legs, chain_names={str(k): v for k, v in NAMES.items()},
                    scope=dict(provider='Wormholescan', chain_id_namespace='wormhole',
                               canonical_chain_records_connected=['solana_partial'],
                               cex_internal_ledger_available=False, all_bridges_supported=False,
                               complete_history=False, same_funds_continuity_proven=False, orders_submitted=0),
                    gaps=['Exchange customer ledger and private internal transfers are unavailable.',
                          'A parsed VAA is not a verified quorum, trusted emitter, canonical source or redemption.',
                          'CCTP, NTT, other bridges and ordinary EVM account transfers are not connected.',
                          'Provider snapshot pagination and time filters do not establish complete history.'])
