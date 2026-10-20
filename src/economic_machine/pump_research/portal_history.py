"""Bounded public index evaluation; provider rows are never graph evidence.

The Solana Portal index selects candidate signed transactions. Each candidate
must subsequently match a decoded CAR transaction and address membership. The
fixed public-source budget is shared by every root and every restart. This is
an evaluation adapter, not a commercial/customer/trading data feed.
"""
import hashlib
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import zlib

from .archive import NoRedirect
from .store import address, atomic, canonical, decode

HOST = 'https://portal.sqd.dev/datasets/solana-mainnet'
SOURCE_BYTES = 50_000_000
MAX_RESPONSE = 256 * 1024
WINDOW_SLOTS = 10_000
MAX_CANDIDATES = 128
MAX_HEADERS = 2048
PRIOR_PROBE_BYTES = 6745
METADATA_RESERVATION_CEILING = 8 * 1024 * 1024
RETRY_STATUSES = {429, 502, 503, 504, 529}


def continuation_scope(scope):
    """The outer Scope has validated and pinned this additive authority."""
    value = getattr(scope, 'continuation', None)
    if value is None:
        return None
    if (getattr(scope, 'effective_deadline_ms', 'missing') is not None
            or getattr(value, 'effective_deadline_ms', 'missing') is not None
            or not isinstance(value.hash, str) or not re.fullmatch('[0-9a-f]{64}', value.hash)
            or not callable(getattr(value, 'assert_slot', None))
            or not value.inventory_epochs):
        raise ValueError('PORTAL_CONTINUATION_SCOPE')
    return value


class PortalDeferred(ValueError):
    """Admission was deferred without sending or reserving a request."""
    def __init__(self, retry_at_ms, reason):
        self.retry_at_ms = retry_at_ms
        self.reason = reason
        super().__init__('PORTAL_DEFERRED_' + reason.upper())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _network_state(path):
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ValueError('PORTAL_NETWORK_STATE_BOUND')
    value = json.loads(path.read_bytes())
    until = value.get('not_before', 0) if isinstance(value, dict) else None
    if (not isinstance(value, dict) or type(until) not in (int, float)
            or not math.isfinite(until) or until < 0
            or type(value.get('attempts', 0)) is not int or not 0 <= value.get('attempts', 0) <= 1_000_000):
        raise ValueError('PORTAL_NETWORK_STATE_SCHEMA')
    return value


def _retry_delay(error, attempts):
    value = error.headers.get('Retry-After', '') if error.headers else ''
    try:
        delay = float(value)
        if not math.isfinite(delay):
            delay = 0
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 0
    return max(0, delay, min(1800, 60 * 2 ** min(max(0, attempts - 1), 5)))


@contextmanager
def portal_network(base, scope, wait=True):
    """One shared admission/cooldown for metadata and wallet-index requests.

    The file lock covers a single request, preventing a second process from
    dispatching during the first process's 429/529 error handling. Waiting is
    interruptible by the original scope's absolute deadline. No retry resets
    the request ledger or job cursor. Serialized research jobs use wait=False
    so one provider cooldown cannot stall unrelated jobs in the current batch.
    """
    folder = base / 'evidence/wallet-map-v1'; folder.mkdir(exist_ok=True)
    path = folder / 'portal-network-state.json'
    with (folder / 'portal-network-state.lock').open('a+b') as lock:
        while True:
            scope.guard()
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not wait:
                    raise PortalDeferred(math.ceil(time.time() * 1000) + 1000, 'lock_busy')
                time.sleep(0.25)
                continue
            try:
                state = _network_state(path)
                delay = state.get('not_before', 0) - time.time()
                if delay <= 0:
                    break
                if not wait:
                    raise PortalDeferred(math.ceil(state['not_before'] * 1000), 'source_cooldown')
            except BaseException:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                raise
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            time.sleep(max(0, min(1, delay)))
        try:
            scope.guard()
            yield
        except urllib.error.HTTPError as error:
            if error.code in RETRY_STATUSES:
                attempts = state.get('attempts', 0) + 1
                atomic(path, dict(not_before=max(state.get('not_before', 0), time.time() + _retry_delay(error, attempts)),
                    attempts=attempts, status=error.code, failed_reservation_retained=True,
                    cursor_preserved=True, at_ms=time.time_ns() // 1_000_000))
            raise
        else:
            atomic(path, dict(not_before=time.time() + 1, attempts=0, status=200,
                at_ms=time.time_ns() // 1_000_000))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_cache(path, digest):
    packed = path.read_bytes()
    if len(packed) > MAX_RESPONSE + 1024:
        raise ValueError('PORTAL_CACHE_BOUND')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, MAX_RESPONSE + 2)
    if len(raw) > MAX_RESPONSE + 1 or not decoder.eof or decoder.unused_data or sha(raw) != digest:
        raise ValueError('PORTAL_CACHE_HASH')
    return raw


def signature(value):
    if not isinstance(value, str) or not 64 <= len(value) <= 88:
        return False
    try:
        return len(decode(value)) == 64
    except (ValueError, TypeError):
        return False


def query(wallet, start, end):
    if not address(wallet) or type(start) is not int or type(end) is not int or not 0 <= start <= end < 1_000_000_001:
        raise ValueError('PORTAL_QUERY_ARGUMENT')
    if end - start >= WINDOW_SLOTS:
        raise ValueError('PORTAL_QUERY_WINDOW')
    return dict(type='solana', fromBlock=start, toBlock=end,
        fields=dict(block=dict(number=True, hash=True), transaction=dict(
            transactionIndex=True, signatures=True, accountKeys=True, loadedAddresses=True, err=True)),
        transactions=[dict(mentionsAccount=[wallet])])


def parse_page(raw, wallet, start, end, *, truncated=False, http_status=200):
    """Only complete, ordered NDJSON block records can advance the cursor.

    A partial final block is discarded, never interpreted as an empty block.
    Provider gaps between headers are retained as claims about its filtered
    scan; they are not proof of absent transactions or consensus-skipped slots.
    """
    if http_status == 204:
        if raw.strip():
            raise ValueError('PORTAL_204_BODY')
        return dict(headers=[], candidates=[], next_slot=start, empty=True, truncated=False)
    if http_status != 200:
        raise ValueError('PORTAL_HTTP_STATUS')
    if len(raw) > MAX_RESPONSE + 1:
        raise ValueError('PORTAL_RESPONSE_BOUND')
    lines = raw.splitlines(keepends=True)
    if truncated and lines and not lines[-1].endswith(b'\n'):
        lines.pop()
    headers, candidates, seen = [], [], set()
    for line in lines:
        if not line.strip():
            continue
        if len(headers) >= MAX_HEADERS:
            break  # Unprocessed complete headers stay beyond the next cursor.
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError('PORTAL_NDJSON') from e
        if not isinstance(item, dict) or not isinstance(item.get('header'), dict):
            raise ValueError('PORTAL_BLOCK_SCHEMA')
        h = item['header']; slot = h.get('number')
        if type(slot) is not int or not start <= slot <= end or (headers and slot <= headers[-1]['number']):
            raise ValueError('PORTAL_BLOCK_ORDER')
        if not isinstance(h.get('hash'), str) or not 1 <= len(h['hash']) <= 128:
            raise ValueError('PORTAL_BLOCK_HASH')
        txs = item.get('transactions', [])
        if not isinstance(txs, list) or len(txs) > MAX_CANDIDATES:
            raise ValueError('PORTAL_CANDIDATE_BOUND')
        if len(candidates) + len(txs) > MAX_CANDIDATES:
            break  # Never acknowledge a block whose candidate list was cut.
        block_candidates = []; orders = set()
        for tx in txs:
            if not isinstance(tx, dict):
                raise ValueError('PORTAL_TRANSACTION_SCHEMA')
            order, sigs = tx.get('transactionIndex'), tx.get('signatures')
            keys, loaded = tx.get('accountKeys'), tx.get('loadedAddresses')
            if type(order) is not int or not 0 <= order <= 100_000 or order in orders:
                raise ValueError('PORTAL_TRANSACTION_ORDER')
            orders.add(order)
            if not isinstance(sigs, list) or not 1 <= len(sigs) <= 64 or not all(signature(s) for s in sigs):
                raise ValueError('PORTAL_SIGNATURE')
            if (not isinstance(keys, list) or len(keys) > 256 or not all(address(k) for k in keys)
                    or not isinstance(loaded, dict) or set(loaded) != {'readonly', 'writable'}
                    or any(not isinstance(v, list) or len(v) > 256 or not all(address(k) for k in v) for v in loaded.values())):
                raise ValueError('PORTAL_ACCOUNT_KEYS')
            if 'err' not in tx:
                raise ValueError('PORTAL_STATUS_MISSING')
            if wallet not in keys + loaded['readonly'] + loaded['writable']:
                raise ValueError('PORTAL_WRONG_WALLET')
            if sigs[0] in seen:
                raise ValueError('PORTAL_DUPLICATE_SIGNATURE')
            seen.add(sigs[0])
            block_candidates.append(dict(slot=slot, signature=sigs[0], signatures=sigs,
                transaction_index=order, provider_success=tx['err'] is None,
                wallet=wallet, account_keys=keys, loaded_addresses=loaded,
                provider_block_hash=h['hash'], evidence_tier='provider_index_claim',
                transaction_signature_verified=False, independent_consensus_verified=False))
        headers.append(dict(number=slot, hash=h['hash']))
        candidates.extend(block_candidates)
    return dict(headers=headers, candidates=candidates,
        next_slot=headers[-1]['number'] + 1 if headers else start,
        empty=not headers, truncated=truncated, whole_wallet_history=False,
        provider_filtered_gaps_verified=False)


def corroborate_candidates(candidates, block, wallet, slot):
    """Signature identity, slot, account membership, order and status must agree."""
    common = dict(slot=slot, wallet=wallet, independent_consensus_verified=False,
        whole_wallet_history=False, spl_owner_history_complete=False, same_funds_continuity_proven=False)
    if block is None:
        return dict(common, state='portal_archive_index_absent', not_proven_skipped=True, matches=[])
    if block.get('slot') != slot:
        raise ValueError('PORTAL_RAW_SLOT_MISMATCH')
    by_signature = {t['signature']: t for t in block['transactions']}
    matches = []
    for candidate in candidates:
        tx = by_signature.get(candidate['signature'])
        result = dict(signature=candidate['signature'], slot=slot, provider_claim_retained=True)
        if tx is None:
            result['state'] = 'signature_not_decoded'
        elif wallet not in tx.get('account_keys', []):
            result['state'] = 'raw_wallet_membership_mismatch'
        elif tx['index'] != candidate['transaction_index']:
            result['state'] = 'raw_transaction_order_mismatch'
        elif bool(tx['success']) != candidate['provider_success']:
            result['state'] = 'raw_status_mismatch'
        else:
            result.update(state='raw_transaction_corroborated' if tx['success'] else 'raw_failed_transaction_corroborated',
                transaction_signature_verified=True, raw_wallet_membership_verified=True,
                transaction_success=bool(tx['success']), tx_order=tx['index'])
        matches.append(result)
    verified = sum(m['state'] in ('raw_transaction_corroborated', 'raw_failed_transaction_corroborated') for m in matches)
    return dict(common, state='portal_raw_corroborated' if verified == len(matches) and matches else 'portal_raw_mismatch',
        matches=matches, verified_transactions=verified,
        successful_transactions=sum(m['state'] == 'raw_transaction_corroborated' for m in matches),
        decode_errors=block.get('decode_errors', []))


class PortalHistory:
    def __init__(self, graph):
        self.g = graph
        self.folder = graph.out / 'portal-responses'; self.folder.mkdir(exist_ok=True)
        graph.db.executescript('''
          CREATE TABLE IF NOT EXISTS portal_jobs(job TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_pages(id TEXT PRIMARY KEY,job TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_candidates(id TEXT PRIMARY KEY,job TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_results(job TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_source_io(id INTEGER PRIMARY KEY,bytes INTEGER,request TEXT,at INTEGER,status TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_continuation_io(id INTEGER PRIMARY KEY,grant_sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL,request TEXT,at INTEGER,status TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_history_traversals(id TEXT PRIMARY KEY,root TEXT,wallet TEXT,
            grant_sha256 TEXT,origin_job TEXT,next_window_end INTEGER,active_job TEXT,state TEXT,body TEXT,
            UNIQUE(root,wallet,grant_sha256));
          CREATE TABLE IF NOT EXISTS portal_history_windows(job TEXT PRIMARY KEY,traversal TEXT,
            lower_slot INTEGER,upper_slot INTEGER,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_unresolved_gaps(id TEXT PRIMARY KEY,job TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_gap_successors(parent TEXT PRIMARY KEY,child TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS portal_continuation_recoveries(job TEXT,grant_sha256 TEXT,body TEXT,
            PRIMARY KEY(job,grant_sha256));
        ''')

    def seed_wallet(self, root, wallet, scope, start=None, end=None):
        """Explicit public-address discovery, not a verified funding anchor.

        Default scope is the last 216,000 slots under the already pinned head.
        Earlier windows require an explicit caller-supplied start. Neither a
        query nor an empty page establishes full address history. Raw results
        populate AddressIndex, but this path never invents a fund-path edge.
        """
        scope.guard()
        continued = continuation_scope(scope)
        head = self.g.state('portal_finalized_head')
        if (not isinstance(head, dict) or type(head.get('number')) is not int
                or not 240_000_000 <= head['number'] <= 1_000_000_000
                or not isinstance(head.get('hash'), str) or not head['hash']
                or type(head.get('at_ms')) is not int):
            raise ValueError('PORTAL_DISCOVERY_HEAD_REQUIRED')
        if not address(root) or not address(wallet):
            raise ValueError('PORTAL_DISCOVERY_ADDRESS')
        end = head['number'] if end is None else end
        start = max(0, end - 216_000) if start is None and type(end) is int else start
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= head['number']:
            raise ValueError('PORTAL_DISCOVERY_WINDOW')
        if continued is not None:
            continued.assert_slot(start); continued.assert_slot(end)
        ident = sha(canonical(['portal_user_address', root, wallet, start, end, head]).encode())[:24]
        body = dict(parent_job=None, anchor_slot=start, anchor=None, cutoff_head=dict(head, number=end),
            provider_head=head, user_address_discovery=True, verified_funding_anchor=False,
            discovery_range_start=start, discovery_range_end=end, discovery_only=True,
            provider='sqd_public_portal', evaluation_only=True, commercial_feed=False,
            paid_api=False, whole_wallet_history=False, same_funds_continuity_proven=False)
        with self.g.db:
            self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                (ident, root, wallet, start, None, 0, 'pending', canonical(dict(next_slot=start, ordinal=0)), 'portal_forward'))
            self.g.db.execute('INSERT OR IGNORE INTO portal_jobs VALUES(?,?)', (ident, canonical(body)))
            self.g.custody.attach(ident)
        if continued is not None:
            self._ensure_history(ident, continued)
        return dict(job_id=ident, start_slot=start, end_slot=end, user_address_discovery=True,
            verified_funding_anchor=False, whole_wallet_history=False)

    def seed(self, scope):
        scope.guard()
        continued = continuation_scope(scope)
        history = self.maintain_history(scope) if continued is not None else None
        head = self.g.state('portal_finalized_head')
        if (not isinstance(head, dict) or type(head.get('number')) is not int
                or not 240_000_000 <= head['number'] <= 1_000_000_000
                or not isinstance(head.get('hash'), str) or not head['hash']
                or type(head.get('at_ms')) is not int):
            return dict(seeded=0, waiting_for='pinned_provider_finalized_head')
        path = self.g.out / 'address-index.sqlite3'
        if not path.exists():
            return dict(seeded=0, waiting_for='raw_address_index')
        index = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
        count = 0
        try:
            with self.g.db:
                for root, in self.g.db.execute('SELECT mint FROM roots').fetchall():
                    scope.guard()
                    if continued is not None and count>=64:break
                    if continued is None and self.g.db.execute("SELECT 1 FROM frontier WHERE root=? AND source='portal_forward' AND state IN ('pending','source_blocked','portal_waiting_gap')", (root,)).fetchone():
                        continue
                    rows = self.g.db.execute('''SELECT f.* FROM frontier f WHERE root=?
                        AND source IN ('local_forward','indexed_forward') AND slot IS NOT NULL
                        AND state NOT IN ('invalid_address','anchor_required')
                        AND NOT EXISTS(SELECT 1 FROM portal_jobs p WHERE json_extract(p.body,'$.parent_job')=f.id)
                        ORDER BY f.depth,f.rowid LIMIT 256''', (root,)).fetchall()
                    for row in rows:
                        if not address(row['wallet']) or not (0 if continued is not None else 240_000_000) <= row['slot'] <= head['number']:
                            continue
                        if continued is not None and row['slot'] // 432000 not in continued.inventory_epochs:
                            continue
                        # A provider row, a label or a guessed timestamp cannot
                        # establish the immutable public-chain starting point.
                        match = index.execute('''SELECT 1 FROM keys k JOIN appearances a ON a.account=k.id
                            JOIN transactions t ON t.id=a.tx JOIN blocks b ON b.id=t.block
                            WHERE k.key=? AND t.slot=? AND t.successful=1 AND b.state!='dead_branch' LIMIT 1''',
                            (decode(row['wallet']), row['slot'])).fetchone()
                        if not match:
                            continue
                        existing = self.g.db.execute('''SELECT 1 FROM portal_jobs p JOIN frontier f ON f.id=p.job
                            WHERE f.root=? AND f.wallet=? AND f.source='portal_forward' LIMIT 1''', (root, row['wallet'])).fetchone()
                        if existing:
                            continue
                        anchor = self.g.anchor(row)
                        if anchor and anchor['slot'] != row['slot']:
                            continue
                        ident = sha(canonical(['portal_forward', root, row['wallet'], row['id'], head]).encode())[:24]
                        body = dict(parent_job=row['id'], anchor_slot=row['slot'], anchor=anchor,
                            cutoff_head=head, provider='sqd_public_portal', evaluation_only=True,
                            paid_api=False, commercial_feed=False, same_funds_continuity_proven=False)
                        self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                            (ident, root, row['wallet'], row['slot'], row['stamp'], row['depth'], 'pending',
                             canonical(dict(next_slot=row['slot'], ordinal=0)), 'portal_forward'))
                        self.g.db.execute('INSERT OR IGNORE INTO portal_jobs VALUES(?,?)', (ident, canonical(body)))
                        self.g.custody.inherit(row['id'], ident)
                        count += 1
                        break
        finally:
            index.close()
        return dict(seeded=count, pinned_cutoff_head=head, whole_wallet_history=False, historical_traversal=history)

    def _ensure_history(self, origin_id, continued):
        row = self.g.db.execute('''SELECT f.*,p.body AS meta FROM frontier f JOIN portal_jobs p ON p.job=f.id
            WHERE f.id=? AND f.source='portal_forward' ''', (origin_id,)).fetchone()
        if row is None:
            raise ValueError('PORTAL_HISTORY_ORIGIN_MISSING')
        meta = json.loads(row['meta'])
        if meta.get('historical_traversal') or meta.get('continuation_parent_job'):
            return None
        end = meta['anchor_slot'] - 1
        ident = sha(canonical(['portal_past_windows', row['root'], row['wallet'], continued.hash]).encode())[:24]
        body = dict(origin_job=origin_id, upper_slot=end, lower_slot=0, provider_head=meta['cutoff_head'],
            continuation_grant_sha256=continued.hash, enumeration_only=True, whole_wallet_history=False,
            funding_anchor_unchanged=True, independent_consensus_verified=False)
        with self.g.db:
            self.g.db.execute('INSERT OR IGNORE INTO portal_history_traversals VALUES(?,?,?,?,?,?,?,?,?)',
                (ident,row['root'],row['wallet'],continued.hash,origin_id,end,None,
                 'pending' if end>=0 else 'inventory_enumerated_with_unverified_gaps',canonical(body)))
        return ident

    def dataset_floor(self):
        """Read an existing metered metadata receipt; never fetch or invent one."""
        path=self.g.out/'portal-dataset-metadata.json'
        if not path.exists():return None
        if path.is_symlink() or path.stat().st_size>65536:raise ValueError('PORTAL_METADATA_RECEIPT_BOUND')
        receipt=json.loads(path.read_bytes());name=receipt.get('raw_path','')
        if (receipt.get('schema')!='economic-machine.portal-dataset-metadata.v1'
            or receipt.get('endpoint')!=HOST+'/metadata' or type(receipt.get('start_block')) is not int
            or not 0<=receipt['start_block']<=1_000_000_000 or not isinstance(name,str)
            or '\\' in name or not name.startswith('evidence/')
            or any(part in ('','.','..') for part in name.split('/'))):
            raise ValueError('PORTAL_METADATA_RECEIPT_SCHEMA')
        source=self.g.base
        for part in name.split('/'):
            source=source/part
            if source.is_symlink():raise ValueError('PORTAL_METADATA_SOURCE_LINK')
        if not source.is_file() or source.stat().st_size>65536:raise ValueError('PORTAL_METADATA_SOURCE_BOUND')
        raw=source.read_bytes();value=json.loads(raw)
        if (sha(raw)!=receipt.get('raw_sha256') or not isinstance(value,dict)
            or value.get('dataset')!='solana-mainnet' or type(value.get('start_block')) is not int
            or value['start_block']!=receipt['start_block']):
            raise ValueError('PORTAL_METADATA_SOURCE_IDENTITY')
        return dict(slot=receipt['start_block'],raw_sha256=receipt['raw_sha256'],
            observed_ms=receipt.get('observed_ms'),provider_claim_only=True)

    def _record_gap(self, job, page_id, page, continued):
        reason=('provider_metadata_before_start' if page.get('metadata_gap') else
            'provider_head_unavailable_204' if page.get('http_status')==204 else
            'incomplete_provider_record' if page.get('truncated') else 'empty_provider_window_200')
        body = dict(start_slot=page['start_slot'],end_slot=page['requested_end_slot'],
            page_id=page_id,response_sha256=page['sha256'],continuation_grant_sha256=continued.hash,
            reason=reason,http_status=page.get('http_status'),
            unresolved=True,absence_proven=False,whole_wallet_history=False)
        ident = sha(canonical([job['id'],page_id,'unresolved']).encode())
        self.g.db.execute('INSERT OR IGNORE INTO portal_unresolved_gaps VALUES(?,?,?)',
            (ident,job['id'],canonical(body)))

    def _successor(self, job, meta, page_id, page, continued):
        """Append a new query segment; never move an old empty page cursor."""
        self._record_gap(job,page_id,page,continued)
        if meta.get('historical_traversal'):
            return None
        previous = self.g.db.execute('SELECT child FROM portal_gap_successors WHERE parent=?',(job['id'],)).fetchone()
        if previous is not None:
            return previous[0]
        start=page['requested_end_slot']+1;cutoff=meta['cutoff_head']['number'];child=None
        if start<=cutoff:
            epochs=[e for e in continued.inventory_epochs if e*432000<=cutoff and (e+1)*432000>start]
            if epochs:
                start=max(start,min(epochs)*432000);continued.assert_slot(start)
                child=sha(canonical(['portal_gap_successor',job['id'],page_id,start,continued.hash]).encode())[:24]
                body=dict(meta,continuation_parent_job=job['id'],query_start_slot=start,
                    continuation_grant_sha256=continued.hash,unresolved_preceding_gap=page_id)
                self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                    (child,job['root'],job['wallet'],job['slot'],job['stamp'],job['depth'],'pending',
                     canonical(dict(next_slot=start,ordinal=0)),'portal_forward'))
                self.g.db.execute('INSERT OR IGNORE INTO portal_jobs VALUES(?,?)',(child,canonical(body)))
                self.g.custody.inherit(job['id'],child)
        self.g.db.execute('INSERT INTO portal_gap_successors VALUES(?,?,?)',
            (job['id'],child,canonical(dict(page_id=page_id,continuation_grant_sha256=continued.hash,
                original_cursor_preserved=True,whole_wallet_history=False))))
        return child

    def maintain_history(self, scope, limit=64):
        """One backward window per wallet, with a separate durable scheduler cursor."""
        continued=continuation_scope(scope)
        if continued is None:return dict(seeded=0,legacy=True)
        if type(limit) is not int or not 1<=limit<=64:raise ValueError('PORTAL_HISTORY_BATCH_BOUND')
        scope.guard();created=0;floor=self.dataset_floor();floor_slot=floor['slot'] if floor else 0
        blocked=self.g.db.execute('''SELECT f.* FROM frontier f JOIN state s ON s.key='source_error:'||f.id
            WHERE f.source='portal_forward' AND f.state='source_blocked'
            AND json_extract(s.body,'$.error_type')='ValueError'
            AND json_extract(s.body,'$.error')='PORTAL_EVALUATION_IO_LIMIT'
            AND NOT EXISTS(SELECT 1 FROM portal_continuation_recoveries r WHERE r.job=f.id AND r.grant_sha256=?)
            ORDER BY f.rowid LIMIT ?''',(continued.hash,limit)).fetchall()
        for job in blocked:
            error=self.g.state('source_error:'+job['id'],{})
            if error.get('error_type')!='ValueError' or error.get('error')!='PORTAL_EVALUATION_IO_LIMIT':continue
            scope.guard()
            with self.g.db:
                self.g.db.execute('INSERT OR IGNORE INTO portal_continuation_recoveries VALUES(?,?,?)',
                    (job['id'],continued.hash,canonical(dict(original_error=error,original_cursor=job['cursor'],
                        resumed_at_ms=time.time_ns()//1_000_000,old_reservations_unchanged=True))))
                self.g.db.execute("UPDATE frontier SET state='pending' WHERE id=? AND state='source_blocked'",(job['id'],))
        origins=self.g.db.execute('''SELECT f.id FROM frontier f JOIN portal_jobs p ON p.job=f.id
            WHERE f.source='portal_forward' AND json_extract(p.body,'$.historical_traversal') IS NULL
            AND json_extract(p.body,'$.continuation_parent_job') IS NULL
            AND NOT EXISTS(SELECT 1 FROM portal_history_traversals h WHERE h.root=f.root AND h.wallet=f.wallet
                AND h.grant_sha256=?) ORDER BY f.rowid LIMIT ?''',(continued.hash,limit)).fetchall()
        for row in origins:scope.guard();self._ensure_history(row['id'],continued)
        # Recover old empty jobs after activation without rewriting their pages.
        waiting=self.g.db.execute('''SELECT f.*,p.body AS meta FROM frontier f JOIN portal_jobs p ON p.job=f.id
            WHERE f.source='portal_forward' AND f.state='portal_waiting_gap'
            AND json_extract(p.body,'$.historical_traversal') IS NULL
            AND NOT EXISTS(SELECT 1 FROM portal_gap_successors s WHERE s.parent=f.id)
            ORDER BY f.rowid LIMIT ?''',(limit,)).fetchall()
        for row in waiting:
            scope.guard()
            page=self.g.db.execute('SELECT id,body FROM portal_pages WHERE job=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone()
            if page is not None:
                value=json.loads(page['body'])
                if value.get('empty'):
                    with self.g.db:self._successor(dict(row),json.loads(row['meta']),page['id'],value,continued)
        rows=self.g.db.execute('''SELECT * FROM portal_history_traversals WHERE grant_sha256=?
            AND state='pending' AND (active_job IS NULL OR EXISTS(SELECT 1 FROM frontier f WHERE f.id=active_job
                AND f.state IN ('portal_waiting_gap','portal_snapshot_scanned')))
            ORDER BY coalesce(json_extract(body,'$.last_scheduled_ms'),0),rowid LIMIT ?''',
            (continued.hash,limit)).fetchall()
        for row in rows:
            scope.guard();row=dict(row);end=row['next_window_end']
            if floor_slot>0:
                gap=dict(start_slot=0,end_slot=min(json.loads(row['body'])['upper_slot'],floor_slot-1),
                    reason='provider_metadata_before_start',metadata_sha256=floor['raw_sha256'],
                    unresolved=True,absence_proven=False,whole_wallet_history=False)
                if gap['end_slot']>=0:
                    with self.g.db:self.g.db.execute('INSERT OR IGNORE INTO portal_unresolved_gaps VALUES(?,?,?)',
                        (sha(canonical([row['id'],'metadata_floor',floor['raw_sha256']]).encode()),row['origin_job'],canonical(gap)))
            if row['active_job']:
                active=self.g.db.execute('SELECT state FROM frontier WHERE id=?',(row['active_job'],)).fetchone()
                if active is None:raise ValueError('PORTAL_HISTORY_ACTIVE_MISSING')
                if active['state'] not in ('portal_waiting_gap','portal_snapshot_scanned'):continue
                window=self.g.db.execute('SELECT lower_slot FROM portal_history_windows WHERE job=?',(row['active_job'],)).fetchone()
                if window is None:raise ValueError('PORTAL_HISTORY_WINDOW_MISSING')
                end=window['lower_slot']-1
            epochs=[e for e in continued.inventory_epochs if e*432000<=end and (e+1)*432000>floor_slot]
            if not epochs:
                with self.g.db:self.g.db.execute("UPDATE portal_history_traversals SET next_window_end=?,active_job=NULL,state='inventory_enumerated_with_unverified_gaps' WHERE id=?",(end,row['id']))
                continue
            epoch=max(epochs);end=min(end,(epoch+1)*432000-1)
            if end<floor_slot:
                with self.g.db:self.g.db.execute("UPDATE portal_history_traversals SET next_window_end=?,active_job=NULL,state='inventory_enumerated_with_unverified_gaps' WHERE id=?",(end,row['id']))
                continue
            start=max(epoch*432000,end-WINDOW_SLOTS+1,floor_slot)
            continued.assert_slot(start);continued.assert_slot(end)
            origin=self.g.db.execute('SELECT f.*,p.body AS meta FROM frontier f JOIN portal_jobs p ON p.job=f.id WHERE f.id=?',(row['origin_job'],)).fetchone()
            if origin is None:raise ValueError('PORTAL_HISTORY_ORIGIN_MISSING')
            meta=json.loads(origin['meta']);job=sha(canonical(['portal_past_window',row['id'],start,end]).encode())[:24]
            body=dict(meta,historical_traversal=row['id'],anchor_slot=start,anchor=None,
                cutoff_head=dict(meta['cutoff_head'],number=end),provider_head=meta['cutoff_head'],cutoff_is_query_bound=True,
                original_forward_anchor_slot=meta['anchor_slot'],discovery_only=True,
                verified_funding_anchor=False,continuation_grant_sha256=continued.hash,
                provider_start_metadata=floor,provider_start_known=floor is not None)
            traversal_body=json.loads(row['body']);traversal_body['last_scheduled_ms']=time.time_ns()//1_000_000
            with self.g.db:
                self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                    (job,row['root'],row['wallet'],start,None,origin['depth'],'pending',canonical(dict(next_slot=start,ordinal=0)),'portal_forward'))
                self.g.db.execute('INSERT OR IGNORE INTO portal_jobs VALUES(?,?)',(job,canonical(body)))
                self.g.db.execute('INSERT OR IGNORE INTO portal_history_windows VALUES(?,?,?,?,?)',
                    (job,row['id'],start,end,canonical(dict(enumeration_only=True,whole_wallet_history=False))))
                self.g.custody.inherit(row['origin_job'],job)
                self.g.db.execute('UPDATE portal_history_traversals SET next_window_end=?,active_job=?,body=? WHERE id=?',
                    (end,job,canonical(traversal_body),row['id']))
            created+=1
        return dict(seeded=created,whole_wallet_history=False,empty_windows_are_unresolved=True)

    def _reserve(self, scope, request_hash):
        continued=continuation_scope(scope)
        if continued is not None:
            with self.g.db:
                ident=self.g.db.execute('INSERT INTO portal_continuation_io(grant_sha256,bytes,request,at,status,body) VALUES(?,?,?,?,?,?)',
                    (continued.hash,MAX_RESPONSE+1,request_hash,time.time_ns()//1_000_000,'reserved','{}')).lastrowid
            reservation=scope.reserve(MAX_RESPONSE+1,'portal-continuation:'+request_hash)
            with self.g.db:self.g.db.execute('UPDATE portal_continuation_io SET body=? WHERE id=?',
                (canonical(dict(outer_reservation=reservation,outer_ledger='evidence/solana-continuation-v1/io.sqlite3',
                    continuation_grant_sha256=continued.hash,original_source_ledger_preserved=True)),ident))
            return ident
        other = self._metadata_reserved()
        # Metadata acquisition has a separate writer. Leave its entire fixed
        # ceiling available instead of racing a concurrently appended receipt.
        other = max(other, PRIOR_PROBE_BYTES + METADATA_RESERVATION_CEILING)
        with self.g.db:
            self.g.db.execute('BEGIN IMMEDIATE')
            used = self.g.db.execute('SELECT coalesce(sum(bytes),0) FROM portal_source_io').fetchone()[0]
            if used + other + MAX_RESPONSE + 1 > SOURCE_BYTES:
                raise ValueError('PORTAL_EVALUATION_IO_LIMIT')
            ident = self.g.db.execute('INSERT INTO portal_source_io(bytes,request,at,status,body) VALUES(?,?,?,?,?)',
                (MAX_RESPONSE + 1, request_hash, time.time_ns() // 1_000_000, 'reserved', '{}')).lastrowid
        # Both reservations precede dispatch. Failures are not refunded in
        # either journal, including failure to reserve the broader scope.
        scope.reserve(MAX_RESPONSE + 1, 'portal-evaluation:' + request_hash)
        return ident

    def _metadata_reserved(self):
        total = PRIOR_PROBE_BYTES
        for path, schema in (
                (self.g.base / 'evidence/recent50-v1/portal-metadata-ledger.json', 'economic-machine.portal-metadata-ledger.v1'),
                (self.g.out / 'portal-launch-ledger.json', 'economic-machine.portal-launch-ledger.v1')):
            if not path.exists():
                continue
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('PORTAL_METADATA_LEDGER_BOUND')
            data = json.loads(path.read_bytes())
            if (not isinstance(data, dict) or data.get('schema') != schema
                    or not isinstance(data.get('requests'), list) or len(data['requests']) > 4096):
                raise ValueError('PORTAL_METADATA_LEDGER_SCHEMA')
            subtotal = 0
            for row in data['requests']:
                if not isinstance(row, dict) or type(row.get('reserved_bytes')) is not int or not 0 < row['reserved_bytes'] <= SOURCE_BYTES:
                    raise ValueError('PORTAL_METADATA_LEDGER_RESERVATION')
                subtotal += row['reserved_bytes']
            if subtotal > 4 * 1024 * 1024:
                raise ValueError('PORTAL_METADATA_LEDGER_CAP')
            total += subtotal
        return total

    def fetch(self, body, scope):
        continued=continuation_scope(scope)
        if continued is not None:
            continued.assert_slot(body['fromBlock']);continued.assert_slot(body['toBlock'])
        encoded = canonical(body).encode(); request_hash = sha(encoded)
        request = urllib.request.Request(HOST + '/finalized-stream', data=encoded, method='POST',
            headers={'Content-Type': 'application/json', 'Accept': 'application/x-ndjson',
                     'Accept-Encoding': 'identity', 'User-Agent': 'EconomicMachineResearch/0.3-evaluation'})
        with portal_network(self.g.base, scope, wait=False):
            ident = self._reserve(scope, request_hash)
            table='portal_continuation_io' if continued is not None else 'portal_source_io'
            reservation_body=json.loads(self.g.db.execute('SELECT body FROM '+table+' WHERE id=?',(ident,)).fetchone()[0])
            try:
                with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                    status = response.status
                    raw = response.read(MAX_RESPONSE + 1)
            except urllib.error.HTTPError as error:
                with self.g.db:
                    self.g.db.execute('UPDATE '+table+' SET status=?,body=? WHERE id=?',
                        ('http_error', canonical(dict(reservation_body,http_status=error.code, retry_after=error.headers.get('Retry-After') if error.headers else None,
                            failed_reservation_retained=True)), ident))
                raise
        digest = sha(raw); path = self.folder / (digest + '.ndjson.zlib')
        if path.exists():
            read_cache(path, digest)
        else:
            with path.open('xb') as handle:
                handle.write(zlib.compress(raw, 9)); handle.flush(); os.fsync(handle.fileno())
        receipt = dict(request_id=ident, request_sha256=request_hash, request=body, source=HOST + '/finalized-stream',
            sha256=digest, bytes=len(raw), http_status=status, truncated=len(raw) > MAX_RESPONSE,
            at_ms=time.time_ns() // 1_000_000, provider_claim_only=True)
        if continued is not None:
            receipt.update(reservation_body,source_ledger=table,continuation_grant_sha256=continued.hash)
        prefix=f'continuation-{continued.hash[:16]}-' if continued is not None else ''
        atomic(self.folder / f'{prefix}{ident}-{request_hash[:16]}.receipt.json', receipt)
        with self.g.db:
            self.g.db.execute('UPDATE '+table+' SET status=?,body=? WHERE id=?', ('received', canonical(receipt), ident))
        return raw, receipt

    def run(self, job, scope):
        scope.guard(MAX_RESPONSE + 1, force=True)
        continued=continuation_scope(scope)
        current = self.g.db.execute('SELECT * FROM frontier WHERE id=?', (job['id'],)).fetchone()
        row = self.g.db.execute('SELECT body FROM portal_jobs WHERE job=?', (job['id'],)).fetchone()
        if current is None or row is None:
            raise ValueError('PORTAL_JOB_MISSING')
        job, meta = dict(current), json.loads(row[0])
        if job['state'] not in ('pending', 'source_blocked'):
            return dict(job_id=job['id'], state=job['state'], no_request=True)
        if job['source'] == 'portal_verify':
            return self.verify(job, meta, scope)
        if job['source'] != 'portal_forward':
            raise ValueError('PORTAL_JOB_SOURCE')
        cursor = json.loads(job['cursor']); start = cursor['next_slot']; cutoff = meta['cutoff_head']['number']
        if type(start) is not int or not meta['anchor_slot'] <= start <= cutoff + 1:
            raise ValueError('PORTAL_CURSOR_BOUND')
        if start > cutoff:
            with self.g.db:
                self.g.db.execute("UPDATE frontier SET state='portal_snapshot_scanned' WHERE id=?", (job['id'],))
            return dict(job_id=job['id'], state='portal_snapshot_scanned', whole_wallet_history=False)
        end = min(start + WINDOW_SLOTS - 1, cutoff)
        if continued is not None:
            if meta.get('continuation_grant_sha256') not in (None,continued.hash):
                raise ValueError('PORTAL_JOB_CONTINUATION_CHANGED')
            continued.assert_slot(start)
            end=min(end,(start//432000+1)*432000-1);continued.assert_slot(end)
            floor=self.dataset_floor()
            if floor is not None and start<floor['slot']:
                page=dict(start_slot=start,requested_end_slot=min(floor['slot']-1,cutoff),
                    sha256=floor['raw_sha256'],metadata_gap=True,http_status=None)
                with self.g.db:
                    self.g.db.execute("UPDATE frontier SET state='portal_waiting_gap' WHERE id=?",(job['id'],))
                    self._successor(job,meta,'metadata:'+floor['raw_sha256'],page,continued)
                return dict(job_id=job['id'],state='portal_waiting_gap',no_request=True,
                    next_slot=start,provider_metadata_unavailable_range=True,whole_wallet_history=False)
        raw, receipt = self.fetch(query(job['wallet'], start, end), scope)
        page = parse_page(raw, job['wallet'], start, end, truncated=receipt['truncated'], http_status=receipt['http_status'])
        page_id = sha(canonical([job['id'], start, receipt['sha256']]).encode())
        if self.g.db.execute('SELECT 1 FROM portal_pages WHERE id=?', (page_id,)).fetchone():
            return dict(job_id=job['id'], state='portal_page_already_committed', no_cursor_change=True)
        with self.g.db:
            for slot in sorted({c['slot'] for c in page['candidates']}):
                candidates = [c for c in page['candidates'] if c['slot'] == slot]
                verify_id = sha(canonical(['portal_verify', job['id'], slot, [c['signature'] for c in candidates]]).encode())[:24]
                value = dict(parent_job=job['id'], origin_parent_job=meta['parent_job'],
                    anchor_slot=meta['anchor_slot'], anchor=meta.get('anchor'),
                    candidates=candidates, page_id=page_id, receipt_sha256=receipt['sha256'],
                    provider_block_hash=candidates[0]['provider_block_hash'],
                    discovery_only=meta.get('discovery_only', False),
                    user_address_discovery=meta.get('user_address_discovery', False))
                self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                    (verify_id, job['root'], job['wallet'], slot, job['stamp'], job['depth'], 'pending', 'null', 'portal_verify'))
                self.g.db.execute('INSERT OR IGNORE INTO portal_jobs VALUES(?,?)', (verify_id, canonical(value)))
                self.g.custody.inherit(job['id'], verify_id)
                for candidate in candidates:
                    cid = sha(canonical([job['id'], candidate['slot'], candidate['signature']]).encode())
                    self.g.db.execute('INSERT OR IGNORE INTO portal_candidates VALUES(?,?,?)',
                        (cid, verify_id, canonical(dict(candidate, page_id=page_id, raw_response_sha256=receipt['sha256']))))
            page_receipt = dict(receipt, start_slot=start, requested_end_slot=end, ordinal=cursor['ordinal'],
                next_slot=page['next_slot'], headers=page['headers'], candidates=len(page['candidates']),
                empty=page['empty'], provider_gap_claims_only=True, whole_wallet_history=False)
            self.g.db.execute('INSERT INTO portal_pages VALUES(?,?,?)', (page_id, job['id'], canonical(page_receipt)))
            state = 'portal_waiting_gap' if page['empty'] else 'portal_snapshot_scanned' if page['next_slot'] > cutoff else 'pending'
            self.g.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',
                (canonical(dict(next_slot=page['next_slot'], ordinal=cursor['ordinal'] + 1)), state, job['id']))
            if continued is not None and page['empty']:
                self._successor(job,meta,page_id,page_receipt,continued)
        return dict(job_id=job['id'], state=state, candidate_transactions=len(page['candidates']),
            next_slot=page['next_slot'], request_end_not_assumed_scanned=True,
            whole_wallet_history=False, private_ledger_recovered=False)

    def verify(self, job, meta, scope):
        from .address_index import AddressIndex
        from .cli import proto
        from .raw_history import ScopedArchive
        # Verify the exact provider bytes again before spending raw acquisition
        # budget. A stale/mutated candidate cannot silently authorize a block.
        path = self.folder / (meta['receipt_sha256'] + '.ndjson.zlib')
        if not path.exists():
            raise ValueError('PORTAL_VERIFY_CACHE_HASH')
        raw = read_cache(path, meta['receipt_sha256'])
        page_row = self.g.db.execute('SELECT body FROM portal_pages WHERE id=?', (meta['page_id'],)).fetchone()
        if page_row is None:
            raise ValueError('PORTAL_VERIFY_PAGE_MISSING')
        page_receipt = json.loads(page_row[0])
        parsed = parse_page(raw, job['wallet'], page_receipt['start_slot'], page_receipt['requested_end_slot'],
            truncated=page_receipt['truncated'], http_status=page_receipt['http_status'])
        original = [c for c in parsed['candidates'] if c['slot'] == job['slot']]
        if canonical(original) != canonical(meta['candidates']):
            raise ValueError('PORTAL_VERIFY_SOURCE_CHANGED')
        candidate_rows = self.g.db.execute('SELECT body FROM portal_candidates WHERE job=? ORDER BY id', (job['id'],)).fetchall()
        pinned = [json.loads(r[0]) for r in candidate_rows]
        if sorted((c['signature'], c['slot']) for c in pinned) != sorted((c['signature'], c['slot']) for c in meta['candidates']):
            raise ValueError('PORTAL_VERIFY_CANDIDATES_CHANGED')
        _, meta_type = proto('/srv/economic-machine/collector')
        index = AddressIndex(self.g.out / 'address-index.sqlite3')
        try:
            block, receipt = ScopedArchive(self.g, scope, None, meta_type, index, wait=False).read_slot(job['slot'])
        finally:
            index.close()
        result = corroborate_candidates(meta['candidates'], block, job['wallet'], job['slot'])
        result.update(job_id=job['id'], provider_page=meta['page_id'], provider_response_sha256=meta['receipt_sha256'])
        if receipt:
            result['raw_receipt'] = {k: v for k, v in receipt.items() if k != 'raw'}
        with self.g.db:
            self.g.db.execute('INSERT OR REPLACE INTO portal_results VALUES(?,?)', (job['id'], canonical(result)))
            self.g.db.execute('UPDATE frontier SET state=? WHERE id=?', (result['state'], job['id']))
            if result.get('successful_transactions') and meta.get('discovery_only'):
                # The explicit address-search UI may have created a slot-zero
                # local-index reader before this raw block arrived. Wake only
                # that existing reader; never create an inferred funding anchor
                # or reset the reader's monotonic row-ID cursor.
                self.g.db.execute("""UPDATE frontier SET state='pending'
                    WHERE root=? AND wallet=? AND source='indexed_forward' AND slot=0
                    AND stamp IS NULL AND state='indexed_source_exhausted'""", (job['root'], job['wallet']))
            if result.get('successful_transactions') and not meta.get('discovery_only'):
                parent = self.g.db.execute('SELECT * FROM frontier WHERE id=?', (meta['origin_parent_job'],)).fetchone()
                if parent is None or parent['slot'] != meta['anchor_slot']:
                    raise ValueError('PORTAL_ORIGINAL_ANCHOR_CHANGED')
                anchor = meta.get('anchor')
                child = self.g.enqueue(job['root'], job['wallet'], parent['slot'], parent['stamp'], parent['depth'],
                    'indexed_forward', after_order=(anchor['tx_order'], anchor['ix']) if anchor else None)
                self.g.custody.inherit(job['id'], child)
                # IDs in the local raw index admit late backfills. Preserve an
                # existing cursor; only wake an exhausted original-anchor job.
                self.g.db.execute("UPDATE frontier SET state='pending' WHERE id=? AND state='indexed_source_exhausted'", (child,))
        return result

    def snapshot(self):
        states = dict(self.g.db.execute("SELECT state,count(*) FROM frontier WHERE source IN ('portal_forward','portal_verify') GROUP BY state"))
        used = self.g.db.execute('SELECT coalesce(sum(bytes),0) FROM portal_source_io').fetchone()[0]
        other = self._metadata_reserved()
        continuation_used=self.g.db.execute('SELECT coalesce(sum(bytes),0) FROM portal_continuation_io').fetchone()[0]
        return dict(schema='economic-machine.public-portal-evaluation.v1', at_ms=time.time_ns() // 1_000_000,
            source=HOST, source_io_reserved_bytes=used + other, wallet_query_reserved_bytes=used,
            metadata_and_prior_probe_reserved_bytes=other, source_io_cap_bytes=SOURCE_BYTES,
            job_states=states, pages=self.g.db.execute('SELECT count(*) FROM portal_pages').fetchone()[0],
            candidate_transactions=self.g.db.execute('SELECT count(*) FROM portal_candidates').fetchone()[0],
            raw_results=[json.loads(r[0]) for r in self.g.db.execute('SELECT body FROM portal_results ORDER BY rowid DESC LIMIT 100')],
            continuation_query_reserved_bytes=continuation_used,
            continuation_authorities=[dict(grant_sha256=r[0],reserved_bytes=r[1]) for r in self.g.db.execute(
                'SELECT grant_sha256,sum(bytes) FROM portal_continuation_io GROUP BY grant_sha256')],
            historical_traversal_states=dict(self.g.db.execute('SELECT state,count(*) FROM portal_history_traversals GROUP BY state')),
            unresolved_provider_windows=self.g.db.execute('SELECT count(*) FROM portal_unresolved_gaps').fetchone()[0],
            provider_start_metadata=self.dataset_floor(),
            evaluation_only=True, commercial_feed=False, paid_api=False,
            whole_wallet_history=False, spl_owner_history_complete=False, private_ledger_recovered=False,
            independent_consensus_verified=False)
