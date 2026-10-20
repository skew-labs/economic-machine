"""Discover token-account leads from retained transaction-boundary metadata.

An SPL authority observed before/after a transaction is not an instruction-time
owner, a present owner, a person, or a payment endpoint. Account follow-up jobs
keep their actual account address and do not inherit an owner identity.
"""
from contextlib import closing
import hashlib
import json
import sqlite3

from .precision import owner_evidence
from .store import address, canonical, decode, encode


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS token_owner_trace_source(id INTEGER PRIMARY KEY,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS token_owner_scan(root TEXT PRIMARY KEY,tx INTEGER NOT NULL,
        account TEXT NOT NULL,phase TEXT NOT NULL,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS token_owner_scan_receipts(id INTEGER PRIMARY KEY,root TEXT NOT NULL,
        from_cursor TEXT NOT NULL,to_cursor TEXT NOT NULL,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS token_owner_observations(id TEXT PRIMARY KEY,root TEXT NOT NULL,
        tx INTEGER NOT NULL,account TEXT NOT NULL,phase TEXT NOT NULL,edge TEXT NOT NULL,
        job TEXT,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS token_owner_account_jobs(job TEXT NOT NULL,observation TEXT NOT NULL,
        PRIMARY KEY(job,observation)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS token_owner_trace_rotation(id INTEGER PRIMARY KEY,last_root TEXT NOT NULL);
    ''')


def _evidence(row, paired, wallet):
    if (not address(row['account']) or not address(row['mint']) or row['owner'] != wallet
            or row['phase'] not in ('pre', 'post') or type(row['slot']) is not int
            or not 0 <= row['slot'] < 1_000_000_000):
        raise ValueError('TOKEN_OWNER_METADATA_IDENTITY')
    signature = encode(row['signature'])
    if len(decode(signature)) != 64:
        raise ValueError('TOKEN_OWNER_SIGNATURE')
    digest = row['hash']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('TOKEN_OWNER_SOURCE_HASH')
    if not isinstance(row['receipt'], str) or len(row['receipt'].encode()) > 65536:
        raise ValueError('TOKEN_OWNER_SOURCE_RECEIPT')
    receipt = json.loads(row['receipt'])
    if not isinstance(receipt, dict):
        raise ValueError('TOKEN_OWNER_SOURCE_RECEIPT')
    source = receipt.get('source', 'reconstructed_public_block')
    if not isinstance(source, str) or not source or len(source) > 120:
        raise ValueError('TOKEN_OWNER_SOURCE_RECEIPT')
    amount = row['amount']
    if not isinstance(amount, str) or not amount.isascii() or not amount.isdigit() or len(amount) > 128:
        raise ValueError('TOKEN_OWNER_BOUNDARY_AMOUNT')
    decimals = row['decimals']
    if decimals is not None and (type(decimals) is not int or not 0 <= decimals <= 255):
        raise ValueError('TOKEN_OWNER_DECIMALS')
    boundary = owner_evidence(paired, row['account'], row['mint'])
    return dict(source='retained_token_owner_boundary_metadata', original_source=source,
        source_sha256=digest, signature=signature,
        index_block_id=row['block_id'], index_transaction_id=row['tx'], tx_order=row['tx_order'],
        finality=row['state'], transaction_success=True,
        source_transaction_signatures_verified=receipt.get('transaction_signatures_verified') is True,
        phase=row['phase'], token_account=row['account'], mint=row['mint'],
        observed_boundary_authority=wallet, boundary_amount_raw=amount, boundary_decimals=decimals,
        owner_evidence=boundary, observation_type='token_account_transaction_boundary_authority',
        owner_authority_kind='spl_token_authority_at_transaction_boundary',
        transfer_or_payment=False, instruction_time_owner_proven=False,
        current_owner_verified=False, current_balance_verified=False, same_person_proven=False,
        same_funds_continuity_proven=False, token_account_not_owner=True,
        independent_consensus_verified=False, whole_wallet_history=False,
        followup_scope='Actual token account after observed slot; later owner authority may differ.',
        same_slot_followup_excluded=True)


def seed_owner_accounts(graph, scope, limit=100):
    """Append at most ``limit`` observations, rotating explicit wallet roots.

    The cursor is a local metadata row key, not a chain-history frontier. Old
    transactions enriched below it remain an explicit reconciliation gap.
    Existing trading/source cursors, roots and immutable job anchors are kept.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('TOKEN_OWNER_BATCH_BOUND')
    if graph.db.in_transaction:
        raise ValueError('TOKEN_OWNER_OPEN_TRANSACTION')
    scope.guard()
    path = graph.out / 'address-index.sqlite3'
    result = dict(scanned=0, observations_added=0, account_jobs_added=0, roots_checked=0,
        skipped=0, skip_reasons={}, source_complete=False, complete_wallet_history=False,
        late_enrichment_below_cursor_reconciled=False, current_owner_verified=False,
        same_funds_continuity_proven=False, network_calls=0, model_calls=0)
    if not path.exists():
        return dict(result, waiting_for='retained_address_index')
    _schema(graph.db)
    stat = path.stat()
    corpus = canonical(dict(path=str(path.resolve()), device=stat.st_dev, inode=stat.st_ino,
        source_scope='retained_successful_transaction_boundary_metadata'))
    with graph.db:
        previous = graph.db.execute('SELECT body FROM token_owner_trace_source WHERE id=1').fetchone()
        if previous and previous[0] != corpus:
            raise ValueError('TOKEN_OWNER_SOURCE_REPLACED')
        graph.db.execute('INSERT OR IGNORE INTO token_owner_trace_source VALUES(1,?)', (corpus,))
    rotation = graph.db.execute('SELECT last_root FROM token_owner_trace_rotation WHERE id=1').fetchone()
    after = rotation[0] if rotation else ''
    # At most ten roots per call; root rotation prevents one busy wallet from
    # monopolizing an append-only source scan.
    root_limit = min(10, limit)
    roots = graph.db.execute('''SELECT mint FROM roots WHERE json_valid(body)
        AND json_extract(body,'$.root_kind')='wallet' AND mint>?
        ORDER BY mint LIMIT ?''', (after, root_limit)).fetchall()
    if len(roots) < root_limit:
        roots += graph.db.execute('''SELECT mint FROM roots WHERE json_valid(body)
            AND json_extract(body,'$.root_kind')='wallet' AND mint<=?
            ORDER BY mint LIMIT ?''', (after, root_limit-len(roots))).fetchall()
    if not roots:
        return result
    per_root = max(1, limit // len(roots))
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as index:
        index.row_factory = sqlite3.Row
        index.execute('PRAGMA query_only=ON'); index.execute('PRAGMA cache_size=-4096')
        index.set_progress_handler(lambda: (scope.guard() or 0), 10000)
        index.execute('BEGIN')
        result['index_transaction_upper_bound'] = index.execute('SELECT coalesce(max(id),0) FROM transactions').fetchone()[0]
        for root_row in roots:
            wallet = root_row[0]
            if not address(wallet):
                continue
            scope.guard()
            prior = graph.db.execute('SELECT tx,account,phase FROM token_owner_scan WHERE root=?', (wallet,)).fetchone()
            cursor = tuple(prior) if prior else (0, '', '')
            if cursor[0] > result['index_transaction_upper_bound']:
                raise ValueError('TOKEN_OWNER_SOURCE_CURSOR_REGRESSION')
            rows = index.execute('''SELECT o.*,t.slot,t.tx_order,t.stamp,t.signature,b.id AS block_id,
                b.hash,b.state,substr(b.receipt,1,65537) AS receipt
                FROM token_owners o JOIN transactions t ON t.id=o.tx JOIN blocks b ON b.id=t.block
                WHERE o.owner=? AND (o.tx,o.account,o.phase)>(?,?,?)
                AND t.successful=1 AND b.state!='dead_branch'
                ORDER BY o.tx,o.account,o.phase LIMIT ?''', (wallet,*cursor,per_root)).fetchall()
            next_cursor = cursor
            local_added = local_jobs = local_skipped = 0
            reasons = {}
            with graph.db:
                for row in rows:
                    scope.guard()
                    next_cursor = (row['tx'],row['account'],row['phase'])
                    paired = [dict(r) for r in index.execute('''SELECT account,phase,owner,mint,amount,decimals
                        FROM token_owners WHERE tx=? AND account=? AND mint=? ORDER BY phase LIMIT 3''',
                        (row['tx'],row['account'],row['mint']))]
                    try:
                        evidence = _evidence(row,paired,wallet)
                    except (ValueError,TypeError,AttributeError) as error:
                        reason = str(error) if str(error).startswith('TOKEN_OWNER_') else 'TOKEN_OWNER_MALFORMED_METADATA'
                        local_skipped += 1; reasons[reason] = reasons.get(reason,0)+1
                        continue
                    ident = _hash([wallet,row['tx'],row['account'],row['phase'],evidence])
                    if graph.db.execute('SELECT 1 FROM token_owner_observations WHERE id=?',(ident,)).fetchone():
                        continue
                    stamp = row['stamp']*1000 if type(row['stamp']) is int else None
                    graph.node(wallet,'account','observed_token_boundary_authority')
                    graph.node(row['account'],'account','observed_token_account')
                    edge = graph.edge(wallet,wallet,row['account'],'token_account_owner_observation',
                        row['slot'],stamp,**evidence)
                    job_key = _hash([wallet,row['account'],row['slot'],stamp,'indexed_forward',None])[:24]
                    already_queued = graph.db.execute('SELECT 1 FROM frontier WHERE id=?',(job_key,)).fetchone()
                    job = graph.enqueue(wallet,row['account'],row['slot'],stamp,1,'indexed_forward')
                    local_jobs += int(bool(job) and not already_queued)
                    graph.db.execute('INSERT INTO token_owner_observations VALUES(?,?,?,?,?,?,?,?)',
                        (ident,wallet,row['tx'],row['account'],row['phase'],edge,job,canonical(evidence)))
                    if job:
                        graph.db.execute('INSERT OR IGNORE INTO token_owner_account_jobs VALUES(?,?)',(job,ident))
                    local_added += 1
                status = dict(scanned=len(rows), skipped=local_skipped, skip_reasons=reasons,
                    caught_up_to_current_append_scan=len(rows)<per_root,
                    index_transaction_upper_bound=result['index_transaction_upper_bound'],
                    late_enrichment_below_cursor_reconciled=False, source_complete=False)
                scope.guard()
                # Empty rechecks do not create an ever-growing duplicate journal.
                if rows:
                    graph.db.execute('''INSERT INTO token_owner_scan VALUES(?,?,?,?,?) ON CONFLICT(root)
                        DO UPDATE SET tx=excluded.tx,account=excluded.account,phase=excluded.phase,body=excluded.body''',
                        (wallet,*next_cursor,canonical(status)))
                    graph.db.execute('INSERT INTO token_owner_scan_receipts(root,from_cursor,to_cursor,body) VALUES(?,?,?,?)',
                        (wallet,canonical(cursor),canonical(next_cursor),canonical(status)))
                graph.db.execute('''INSERT INTO token_owner_trace_rotation VALUES(1,?) ON CONFLICT(id)
                    DO UPDATE SET last_root=excluded.last_root''',(wallet,))
            result['roots_checked'] += 1; result['scanned'] += len(rows)
            result['observations_added'] += local_added; result['account_jobs_added'] += local_jobs
            result['skipped'] += local_skipped
            for reason,count in reasons.items():
                result['skip_reasons'][reason] = result['skip_reasons'].get(reason,0)+count
    return result
