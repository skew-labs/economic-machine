"""Durable GSFA candidates -> signed CAR observations, through existing scope.

The compact address fingerprint is not account membership. A result is accepted
only after the exact indexed CAR section is linked by the reconstructed block,
its wire signatures verify, and its actual account keys contain the query key.
Archive integrity and metadata are not independent consensus/finality evidence.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .archive import dataframe, decompress, link, nodes
from .raw_history import ArchiveDeferred
from .store import address, canonical, decode, encode
from .wire import raw_transaction, varint


MIB = 1024 ** 2
VERIFIED = ('verified_success', 'verified_failed_transaction')


def _int(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError('GSFA_HISTORY_' + name)
    return value


def _candidate(value, epoch):
    out = {key: _int(value.get(key), low, high, key.upper()) for key, low, high in (
        ('slot', epoch * 432000, (epoch + 1) * 432000 - 1),
        ('car_offset', 0, 2 ** 48 - 1), ('car_size', 1, 16 * MIB))}
    for key in ('index_has_metadata', 'index_success', 'index_is_vote'):
        if type(value.get(key)) is not bool:
            raise ValueError('GSFA_HISTORY_FLAGS')
        out[key] = value[key]
    return out


def _unproven(state, **fields):
    return dict(state=state, complete_address_history=False,
                independent_consensus_verified=False,
                same_funds_continuity_proven=False, **fields)


def corroborate_candidate(wallet, candidate, block, receipt, meta_type):
    """Re-read one exact transaction section from the existing block receipt.

    This does no IO. ``block, receipt`` must come from ScopedArchive.read_slot,
    which delegates to read_indexed_slot and indexes all decoded account keys.
    This function verifies the candidate again, rather than trusting receipt
    booleans or accepting an unrelated transaction mentioning the same wallet.
    """
    if not address(wallet):
        raise ValueError('GSFA_HISTORY_WALLET')
    slot = _int(candidate.get('slot'), 0, 1000000000, 'SLOT')
    candidate = _candidate(candidate, slot // 432000)
    if block is None:
        return _unproven('unresolved_provider_index_absent', not_proven_skipped=True)
    if block.get('slot') != slot:
        return _unproven('rejected_block_slot_mismatch')
    if not isinstance(receipt, dict):
        raise ValueError('GSFA_HISTORY_RECEIPT')
    raw = receipt.get('raw')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 16 * MIB:
        raise ValueError('GSFA_HISTORY_RAW_BOUND')
    start = _int(receipt.get('car_offset'), 0, 2 ** 64 - 1, 'BLOCK_OFFSET')
    if receipt.get('car_size') != len(raw) or hashlib.sha256(raw).hexdigest() != receipt.get('car_sha256'):
        raise ValueError('GSFA_HISTORY_RECEIPT_HASH')
    relative = candidate['car_offset'] - start
    if relative < 0 or relative + candidate['car_size'] > len(raw):
        return _unproven('rejected_candidate_outside_block')
    # Reject an interior byte offset even if those bytes form another valid CBOR
    # node. Offsets/lengths are CAR sections including their uvarint prefix.
    p = 0
    while p < relative:
        size, body = varint(raw, p)
        if not 36 < size <= 8 * MIB or body + size > len(raw):
            raise ValueError('GSFA_HISTORY_CAR_SECTION')
        p = body + size
    if p != relative:
        return _unproven('rejected_candidate_not_section_boundary')
    size, body = varint(raw, relative)
    if body + size - relative != candidate['car_size']:
        return _unproven('rejected_candidate_size_mismatch')
    all_nodes = nodes(raw)
    selected = nodes(raw[relative:relative + candidate['car_size']])
    if len(selected) != 1:
        return _unproven('rejected_candidate_section_count')
    cid, node = next(iter(selected.items()))
    if len(node) < 4 or node[0] != 0 or node[3] != slot:
        return _unproven('rejected_candidate_transaction_slot')
    blocks = [v for v in all_nodes.values() if v[0] == 2]
    if len(blocks) != 1 or blocks[0][1] != slot:
        return _unproven('rejected_raw_block_slot_mismatch')
    tx_cids = []
    for el in blocks[0][3]:
        entry = all_nodes[link(el)]
        if entry[0] != 1:
            raise ValueError('GSFA_HISTORY_ENTRY_TYPE')
        tx_cids.extend(link(tl) for tl in entry[3])
    if tx_cids.count(cid) != 1:
        return _unproven('rejected_candidate_not_linked_once')
    try:
        signature, keys, _ = raw_transaction(dataframe(node[1], all_nodes))
    except ValueError as error:
        return _unproven('rejected_wire_transaction', reason=str(error)[:100])
    order = node[4] if len(node) > 4 and node[4] is not None else tx_cids.index(cid)
    proof = dict(slot=slot, signature=signature, tx_order=order,
                 signature_verified=True, content_hashes_verified=True,
                 candidate_offset_verified=True, block_link_verified=True,
                 transaction_cid_hex=cid.hex(),
                 transaction_section_sha256=hashlib.sha256(raw[relative:relative + candidate['car_size']]).hexdigest())
    try:
        meta_raw = dataframe(node[2], all_nodes)
        if not meta_raw:
            return _unproven('unresolved_missing_metadata', **proof)
        meta = meta_type.FromString(decompress(meta_raw))
        successful = not meta.HasField('err')
        account_keys = [encode(k) for k in keys] + [encode(bytes(k)) for k in
            [*meta.loaded_writable_addresses, *meta.loaded_readonly_addresses]]
        if len(account_keys) > 256 or any(len(decode(k)) != 32 for k in account_keys):
            raise ValueError('ACCOUNT_KEYS')
    except (ValueError, IndexError, KeyError) as error:
        return _unproven('unresolved_metadata_decode', reason=str(error)[:100], **proof)
    proof.update(transaction_success=successful, transaction_metadata_redecoded=True,
                 metadata_source='public_archive_execution_metadata',
                 loaded_account_resolution_verified_by_consensus=False)
    if wallet not in account_keys:
        return _unproven('rejected_address_mismatch', address_membership_verified=False, **proof)
    proof['address_membership_verified'] = True
    if not candidate['index_has_metadata'] or candidate['index_success'] != successful:
        return _unproven('rejected_index_metadata_flags_mismatch', **proof)
    matches = [t for t in block['transactions'] if t['signature'] == signature]
    if len(matches) != 1:
        return _unproven('unresolved_transaction_not_decoded_once',
                        decode_errors=block.get('decode_errors', []), **proof)
    tx = matches[0]
    if (tx['slot'] != slot or tx['index'] != order or tx['success'] != successful or
            tx['account_keys'] != account_keys):
        return _unproven('rejected_decoded_transaction_mismatch', **proof)
    incoming, outgoing, self_transfers = [], [], []
    if successful:
        for transfer in tx['transfers']:
            src, dst = transfer['source'] == wallet, transfer['destination'] == wallet
            if src and dst:
                self_transfers.append(transfer)
            elif src:
                outgoing.append(transfer)
            elif dst:
                incoming.append(transfer)
    return _unproven('verified_success' if successful else 'verified_failed_transaction',
                    incoming=incoming, outgoing=outgoing, self_transfers=self_transfers,
                    activity=[a for a in tx['activity'] if a['wallet'] == wallet] if successful else [],
                    launches=[a for a in tx['launches'] if wallet in (a['creator'], a['payer'])] if successful else [],
                    token_owner_snapshots=tx.get('token_owner_snapshots', []),
                    block_time=block.get('time'), **proof)


class GsfaHistory:
    """Single-worker durable adapter; callers own scheduling and CPU limits.

    Pass an already-open GsfaIndex, existing ScopedArchive (with AddressIndex),
    and the existing Scope.guard. No scope/lease or account is created here.
    One step queues at most one page and verifies max_candidates (default one).
    Candidate IO uses only archive.read_slot and its original metered cache.
    """
    def __init__(self, path, gsfa, archive, guard):
        if not callable(guard) or not hasattr(archive, 'index') or archive.index is None:
            raise ValueError('GSFA_HISTORY_EXISTING_SCOPE_AND_INDEX_REQUIRED')
        if getattr(archive, 'wait', False):
            raise ValueError('GSFA_HISTORY_NONBLOCKING_ARCHIVE_REQUIRED')
        self.gsfa, self.archive, self.guard = gsfa, archive, guard
        self._check(64 * 1024)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          PRAGMA cache_size=-4096;
          CREATE TABLE IF NOT EXISTS gsfa_history_schema(version INTEGER PRIMARY KEY CHECK(version=1));
          INSERT OR IGNORE INTO gsfa_history_schema VALUES(1);
          CREATE TABLE IF NOT EXISTS gsfa_history_jobs(
            id TEXT PRIMARY KEY,wallet TEXT NOT NULL,epoch INTEGER NOT NULL,index_identity TEXT NOT NULL,
            anchor TEXT NOT NULL,first_slot INTEGER NOT NULL,last_slot INTEGER NOT NULL,created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS gsfa_history_progress(
            job TEXT PRIMARY KEY REFERENCES gsfa_history_jobs(id),cursor TEXT NOT NULL,
            exhausted INTEGER NOT NULL DEFAULT 0,pages INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS gsfa_history_pages(
            id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES gsfa_history_jobs(id),
            cursor_before TEXT NOT NULL,cursor_after TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,
            UNIQUE(job,cursor_before));
          CREATE TABLE IF NOT EXISTS gsfa_history_candidates(
            id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES gsfa_history_jobs(id),
            slot INTEGER NOT NULL,car_offset INTEGER NOT NULL,car_size INTEGER NOT NULL,
            body TEXT NOT NULL,state TEXT NOT NULL,next_at REAL NOT NULL DEFAULT 0,
            UNIQUE(job,slot,car_offset,car_size));
          CREATE INDEX IF NOT EXISTS gsfa_history_work ON gsfa_history_candidates(job,state,next_at,id);
          CREATE TABLE IF NOT EXISTS gsfa_history_attempts(
            id INTEGER PRIMARY KEY,candidate INTEGER NOT NULL REFERENCES gsfa_history_candidates(id),
            started REAL NOT NULL,finished REAL,state TEXT NOT NULL,error_type TEXT,http_status INTEGER);
          CREATE TABLE IF NOT EXISTS gsfa_history_results(
            id INTEGER PRIMARY KEY,candidate INTEGER NOT NULL REFERENCES gsfa_history_candidates(id),
            attempt INTEGER NOT NULL UNIQUE REFERENCES gsfa_history_attempts(id),body TEXT NOT NULL,created REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS gsfa_history_result_candidate ON gsfa_history_results(candidate,id);
          CREATE TRIGGER IF NOT EXISTS gsfa_history_immutable_job_update BEFORE UPDATE ON gsfa_history_jobs
            BEGIN SELECT RAISE(ABORT,'GSFA_HISTORY_IMMUTABLE_ANCHOR'); END;
          CREATE TRIGGER IF NOT EXISTS gsfa_history_immutable_job_delete BEFORE DELETE ON gsfa_history_jobs
            BEGIN SELECT RAISE(ABORT,'GSFA_HISTORY_IMMUTABLE_ANCHOR'); END;
        ''')

    def _check(self, extra=0):
        self.gsfa.budget.check()
        self.guard(extra)

    def close(self):
        self.db.close()

    def register(self, wallet, anchor, first_slot=None, last_slot=None):
        self._check(16384)
        if not address(wallet) or not isinstance(anchor, dict) or not anchor:
            raise ValueError('GSFA_HISTORY_ANCHOR')
        anchor_json = canonical(anchor)
        if len(anchor_json.encode()) > 16384:
            raise ValueError('GSFA_HISTORY_ANCHOR_BOUND')
        epoch = self.gsfa.epoch
        first_slot = epoch * 432000 if first_slot is None else first_slot
        last_slot = (epoch + 1) * 432000 - 1 if last_slot is None else last_slot
        _int(first_slot, epoch * 432000, (epoch + 1) * 432000 - 1, 'FIRST_SLOT')
        _int(last_slot, first_slot, (epoch + 1) * 432000 - 1, 'LAST_SLOT')
        identity = [wallet, epoch, self.gsfa.identity, anchor, first_slot, last_slot]
        job = hashlib.sha256(canonical(identity).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO gsfa_history_jobs VALUES(?,?,?,?,?,?,?,?)',
                            (job, wallet, epoch, self.gsfa.identity, anchor_json, first_slot, last_slot, time.time()))
            self.db.execute('INSERT OR IGNORE INTO gsfa_history_progress(job,cursor) VALUES(?,?)', (job, 'null'))
        return job

    def _job(self, job_id):
        job = self.db.execute('SELECT j.*,p.cursor,p.exhausted,p.pages FROM gsfa_history_jobs j '
                             'JOIN gsfa_history_progress p ON p.job=j.id WHERE j.id=?', (job_id,)).fetchone()
        if job is None or job['index_identity'] != self.gsfa.identity or job['epoch'] != self.gsfa.epoch:
            raise ValueError('GSFA_HISTORY_JOB_IDENTITY')
        return job

    def queue_page(self, job_id, page_size=32):
        self._check(256 * 1024)
        _int(page_size, 1, 100, 'PAGE_BOUND')
        job = self._job(job_id)
        if job['exhausted'] or self.db.execute(
                "SELECT 1 FROM gsfa_history_candidates WHERE job=? AND state IN ('pending','retry') LIMIT 1", (job_id,)).fetchone():
            return False
        before = job['cursor']
        page = self.gsfa.page(job['wallet'], limit=page_size, cursor=json.loads(before))
        if (page.get('index_identity') != job['index_identity'] or page.get('wallet') != job['wallet'] or
                page.get('epoch') != job['epoch'] or not isinstance(page.get('candidates'), list) or
                len(page['candidates']) > page_size):
            raise ValueError('GSFA_HISTORY_PAGE_IDENTITY')
        after = canonical(page.get('next_cursor'))
        exhausted = page.get('index_chain_exhausted')
        if type(exhausted) is not bool or exhausted != (after == 'null') or (not exhausted and before == after):
            raise ValueError('GSFA_HISTORY_CURSOR_NOT_ADVANCING')
        candidates = [_candidate(c, job['epoch']) for c in page['candidates']]
        if len(canonical(page).encode()) > 256 * 1024:
            raise ValueError('GSFA_HISTORY_PAGE_BYTE_BOUND')
        # Queue and cursor commit atomically. A process death after this point
        # cannot lose this page's unverified candidates or change its anchor.
        with self.db:
            current = self.db.execute('SELECT cursor,exhausted FROM gsfa_history_progress WHERE job=?', (job_id,)).fetchone()
            if current['cursor'] != before or current['exhausted']:
                raise ValueError('GSFA_HISTORY_CONCURRENT_WORKER')
            if after != 'null' and self.db.execute('SELECT 1 FROM gsfa_history_pages WHERE job=? AND cursor_before=?', (job_id, after)).fetchone():
                raise ValueError('GSFA_HISTORY_CURSOR_CYCLE')
            self.db.execute('INSERT INTO gsfa_history_pages(job,cursor_before,cursor_after,body,created) VALUES(?,?,?,?,?)',
                            (job_id, before, after, canonical(page), time.time()))
            for c in candidates:
                body = canonical(c)
                old = self.db.execute('SELECT body FROM gsfa_history_candidates WHERE job=? AND slot=? AND car_offset=? AND car_size=?',
                                      (job_id, c['slot'], c['car_offset'], c['car_size'])).fetchone()
                if old and old['body'] != body:
                    raise ValueError('GSFA_HISTORY_CANDIDATE_CHANGED')
                state = 'pending' if job['first_slot'] <= c['slot'] <= job['last_slot'] else 'outside_requested_slot_range'
                self.db.execute('INSERT OR IGNORE INTO gsfa_history_candidates(job,slot,car_offset,car_size,body,state) VALUES(?,?,?,?,?,?)',
                                (job_id, c['slot'], c['car_offset'], c['car_size'], body, state))
            self.db.execute('UPDATE gsfa_history_progress SET cursor=?,exhausted=?,pages=pages+1 WHERE job=?',
                            (after, int(exhausted), job_id))
        return True

    def step(self, job_id, page_size=32, max_candidates=1):
        self._check()
        _int(max_candidates, 1, 16, 'STEP_BOUND')
        self.queue_page(job_id, page_size)
        job = self._job(job_id)
        work = self.db.execute("SELECT * FROM gsfa_history_candidates WHERE job=? AND state IN ('pending','retry') "
                               'AND next_at<=? ORDER BY id LIMIT ?', (job_id, time.time(), max_candidates)).fetchall()
        processed = []
        for item in work:
            self._check(16 * MIB)
            with self.db:
                attempt = self.db.execute('INSERT INTO gsfa_history_attempts(candidate,started,state) VALUES(?,?,?)',
                                          (item['id'], time.time(), 'started')).lastrowid
            try:
                block, receipt = self.archive.read_slot(item['slot'])
                candidate = json.loads(item['body'])
                result = corroborate_candidate(job['wallet'], candidate, block, receipt, self.archive.meta_type)
                result.update(wallet=job['wallet'], epoch=job['epoch'], index_identity=job['index_identity'],
                              index_root_cid_hex=self.gsfa.root_cid, candidate=candidate, anchor=json.loads(job['anchor']),
                              source='gsfa_candidate_to_public_car', paid_api=False)
                if receipt is not None:
                    result['receipt'] = {k: receipt[k] for k in ('car_offset', 'car_size', 'car_sha256', 'index') if k in receipt}
                encoded = canonical(result)
                if len(encoded.encode()) > 4 * MIB:
                    raise ValueError('GSFA_HISTORY_RESULT_BOUND')
                self._check(len(encoded.encode()) + 65536)
                with self.db:
                    self.db.execute('INSERT INTO gsfa_history_results(candidate,attempt,body,created) VALUES(?,?,?,?)',
                                    (item['id'], attempt, encoded, time.time()))
                    self.db.execute('UPDATE gsfa_history_candidates SET state=?,next_at=0 WHERE id=?', (result['state'], item['id']))
                    self.db.execute('UPDATE gsfa_history_attempts SET finished=?,state=? WHERE id=?', (time.time(), result['state'], attempt))
                processed.append(dict(candidate_id=item['id'], state=result['state']))
            except ArchiveDeferred as error:
                with self.db:
                    self.db.execute('UPDATE gsfa_history_attempts SET finished=?,state=? WHERE id=?',
                                    (time.time(), 'source_deferred', attempt))
                    self.db.execute('UPDATE gsfa_history_candidates SET state=?,next_at=? WHERE id=?',
                                    ('retry', error.retry_at_ms / 1000, item['id']))
                raise
            except Exception as error:
                # Unknown outcomes remain journaled. No error message/URL is
                # saved: provider exceptions can contain credential-bearing URLs.
                failures = 1 + self.db.execute("SELECT count(*) FROM gsfa_history_attempts WHERE candidate=? AND state='retryable_exception'", (item['id'],)).fetchone()[0]
                http_status = getattr(error, 'code', None)
                if type(http_status) is not int:
                    http_status = None
                with self.db:
                    self.db.execute('UPDATE gsfa_history_attempts SET finished=?,state=?,error_type=?,http_status=? WHERE id=?',
                                    (time.time(), 'retryable_exception', type(error).__name__, http_status, attempt))
                    self.db.execute('UPDATE gsfa_history_candidates SET state=?,next_at=? WHERE id=?',
                                    ('retry', time.time() + min(1800, 30 * 2 ** min(failures - 1, 6)), item['id']))
                raise
        return dict(self.status(job_id), processed=processed)

    def retry_unresolved(self, candidate_id):
        """Explicit reconciliation after a source/parser repair; preserves results."""
        self._check()
        row = self.db.execute('SELECT job,state FROM gsfa_history_candidates WHERE id=?', (candidate_id,)).fetchone()
        if row is None or not row['state'].startswith('unresolved_'):
            raise ValueError('GSFA_HISTORY_NOT_UNRESOLVED')
        self._job(row['job'])
        with self.db:
            self.db.execute("UPDATE gsfa_history_candidates SET state='pending',next_at=0 WHERE id=?", (candidate_id,))

    def results(self, job_id, after_id=0, limit=100):
        self._check()
        self._job(job_id)
        _int(after_id, 0, 2 ** 63 - 1, 'RESULT_CURSOR')
        _int(limit, 1, 100, 'RESULT_LIMIT')
        rows = self.db.execute('SELECT r.id,r.candidate,r.body FROM gsfa_history_results r JOIN gsfa_history_candidates c ON c.id=r.candidate '
                               'WHERE c.job=? AND r.id>? ORDER BY r.id LIMIT ?', (job_id, after_id, limit)).fetchall()
        return dict(results=[dict(id=r['id'], candidate_id=r['candidate'], result=json.loads(r['body'])) for r in rows],
                    next_after_id=rows[-1]['id'] if rows else after_id, complete_address_history=False)

    def status(self, job_id):
        self._check()
        job = self._job(job_id)
        states = dict(self.db.execute('SELECT state,count(*) FROM gsfa_history_candidates WHERE job=? GROUP BY state', (job_id,)))
        unknown = self.db.execute("SELECT count(*) FROM gsfa_history_attempts a JOIN gsfa_history_candidates c ON c.id=a.candidate "
                                  "WHERE c.job=? AND a.state='started'", (job_id,)).fetchone()[0]
        pending = states.get('pending', 0) + states.get('retry', 0)
        return _unproven('index_chain_drained' if job['exhausted'] and not pending else 'in_progress',
                         job_id=job_id, wallet=job['wallet'], epoch=job['epoch'], anchor=json.loads(job['anchor']),
                         first_slot=job['first_slot'], last_slot=job['last_slot'], pages=job['pages'],
                         index_chain_exhausted=bool(job['exhausted']), states=states,
                         unresolved_attempt_outcomes=unknown, candidate_queue_drained=not pending,
                         incoming_and_outgoing_account_keys_indexed=True,
                         token_owner_indirect_history_complete=False, all_epoch_transactions_proven=False)
