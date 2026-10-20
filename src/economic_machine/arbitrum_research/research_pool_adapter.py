"""Independent Muse investigations over bounded, already-retained EVM evidence.

This adapter is read-only with respect to every source and graph journal. Model
checks run against the same source identities, and outcomes are acknowledged in
the new pool journal. Neither a proposal nor a local index check fills an absent
receipt, trace, counterpart chain, or private exchange ledger.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from economic_machine.pump_research.store import canonical
from .muse import validate_summary, validate_investigation

HEX64 = re.compile(r'^[0-9a-f]{64}$')
ADDRESS = re.compile(r'^0x[0-9a-f]{40}$')
GRAPH_NAME = re.compile(r'^index/[0-9]{12}\.sqlite3$')
MAX_PAGE_BLOCKS = 16384
MAX_QUERY_SECONDS = .25
QUESTIONS = (
    ('coverage', 'header-continuity', 'Check the retained header range and parent links; identify unproved canonicality.'),
    ('coverage', 'trace-gaps', 'Inspect trace coverage and distinguish unavailable internal transfers from zero transfers.'),
    ('graph', 'address-participation', 'Examine sampled sender and recipient participation; identify a specific indexed follow-up without claiming ownership.'),
    ('protocol', 'event-semantics', 'Inspect retained event topics and method observations; distinguish known token events from unclassified protocol activity.'),
    ('custody', 'custody-boundary', 'Examine sampled address routes for follow-up; preserve the private exchange ledger gap and do not infer a common owner.'),
    ('bridge', 'counterpart-gap', 'Identify whether sampled records require counterpart chain evidence; never equate a transaction with a verified bridge message.'),
)


@contextmanager
def _read_db(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError('retained index is not locally available')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
    connection.row_factory = sqlite3.Row
    until = time.monotonic() + MAX_QUERY_SECONDS
    connection.execute('PRAGMA query_only=ON')
    connection.set_progress_handler(lambda: int(time.monotonic() > until), 1000)
    try:
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def _decode(value, maximum=65536):
    if not isinstance(value, str) or len(value.encode()) > maximum:
        raise ValueError('EVM_POOL_RETAINED_JSON_BOUND')
    return json.loads(value)


def _graph_path(base, relative):
    if not isinstance(relative, str) or not GRAPH_NAME.fullmatch(relative):
        raise ValueError('EVM_POOL_GRAPH_PATH')
    path = Path(base) / relative
    if path.resolve().parent != (Path(base) / 'index').resolve():
        raise ValueError('EVM_POOL_GRAPH_PATH')
    return path


def _proof(page, chain):
    proof = _decode(page['proof'])
    cloud = proof.get('cloud', {})
    if (proof.get('chain_id') != chain or proof.get('complete_cloud_readback') is not True
            or proof.get('raw_sha256') != page['sha'] or not HEX64.fullmatch(page['sha'])
            or not HEX64.fullmatch(str(cloud.get('sha256', '')))
            or type(cloud.get('bytes')) is not int or not 0 < cloud['bytes'] <= 64 * 1024**2):
        raise ValueError('EVM_POOL_SOURCE_RECEIPT_MISMATCH')
    return dict(source_sha256=page['sha'], cloud_sha256=cloud['sha256'],
                cloud_bytes=cloud['bytes'], retained_full_readback_receipt=True,
                fresh_blob_readback_performed=False)


def _identity(graph, chain):
    row = graph.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
    identity = _decode(row[0]) if row else {}
    if identity.get('chain_id') != chain or identity.get('schema') not in (1, 2):
        raise ValueError('EVM_POOL_GRAPH_CHAIN')
    return identity


def _sample(graph, page):
    start, end = page['start'], page['end']
    indexed = graph.execute('SELECT start,end FROM pages WHERE source_sha256=?', (page['sha'],)).fetchone()
    if indexed is None or tuple(indexed) != (start, end):
        raise ValueError('EVM_POOL_GRAPH_SOURCE_RANGE')
    headers = [dict(r) for r in graph.execute(
        'SELECT number,hash,parent_hash,trace_coverage FROM blocks WHERE number BETWEEN ? AND ? ORDER BY number LIMIT 3', (start, end))]
    txs = [dict(r) for r in graph.execute(
        'SELECT hash,block,tx_index,sender,recipient,status,value FROM transactions WHERE block BETWEEN ? AND ? ORDER BY block,tx_index LIMIT 4', (start, end))]
    records = []
    for tx in txs[:2]:
        records.extend(dict(r) for r in graph.execute(
            'SELECT block,tx_hash,kind,position,contract,topic0 FROM records WHERE tx_hash=? ORDER BY kind LIMIT 4', (tx['hash'],)))
    return dict(headers=headers, transactions=txs, records=records[:6])


def _candidate_jobs(base, chain):
    base = Path(base)
    with _read_db(base / 'evidence/runtime.sqlite3') as runtime:
        state_row = runtime.execute('SELECT body FROM state WHERE id=1').fetchone()
        if not state_row:
            return []
        state = _decode(state_row[0], 262144)
        shard = state.get('shard_start')
        if type(shard) is not int or shard < 0:
            raise ValueError('EVM_POOL_SHARD_ID')
        relative = f'index/{shard:012d}.sqlite3'
        # A bounded sample of committed source pages, not a claim to cover every
        # page. The active graph can roll over between snapshots; retry next tick.
        pages = [dict(r) for r in runtime.execute(
            "SELECT start,end,sha,proof FROM pages WHERE state='indexed' AND start>=? ORDER BY start DESC LIMIT 16", (shard,))]
    result = []
    with _read_db(_graph_path(base, relative)) as graph:
        _identity(graph, chain)
        for page in pages:
            if not 0 <= page['start'] <= page['end'] < page['start'] + MAX_PAGE_BLOCKS:
                raise ValueError('EVM_POOL_PAGE_BOUND')
            proof = _proof(page, chain)
            sample = _sample(graph, page)
            addresses = sorted({a for tx in sample['transactions'] for a in (tx['sender'], tx['recipient'])
                                if isinstance(a, str) and ADDRESS.fullmatch(a)})[:8]
            for kind, tag, question in QUESTIONS:
                # Evidence identity deliberately excludes timestamp and changing
                # whole-index counts so an unchanged page is not re-billed.
                key = f"evm:{chain}:{page['sha']}:{tag}:v1"
                summary = dict(job_id=f"{chain}:{page['start']}-{page['end']}:{tag}",
                    source_ids=[page['sha']], kind=kind, records=dict(
                        chain_id=chain, question=question, range=[page['start'], page['end']],
                        sample=sample, source_receipt=proof,
                        evidence_level='retained_provider_index_not_independent_consensus',
                        complete_wallet_history=False, private_exchange_ledger_available=False,
                        trace_completeness_verified=False,
                        stock_token_units=('raw_units_no_corporate_action_multiplier' if chain == 4663 else 'not_applicable')))
                validate_summary(summary)
                metadata = dict(adapter='evm-retained-v1', chain_id=chain, graph_relative=relative,
                    first_block=page['start'], last_block=page['end'], source_sha256=page['sha'],
                    addresses=addresses, transaction_samples=sample['transactions'],
                    header_samples=sample['headers'], evidence_key=key)
                result.append((summary, metadata, key))
    return result


def enqueue_jobs(queue, arbbase, rhbase, max_pending=100):
    """Offer distinct retained-source questions without waiting for model calls."""
    if type(max_pending) is not int or not 1 <= max_pending <= 200:
        raise ValueError('EVM_POOL_PENDING_BOUND')
    output = {'offered': 0, 'chains': {}}
    pending = sum(queue.pending_count(str(chain)) for chain in (42161, 4663))
    remaining = max(0, max_pending - pending)
    for chain, base in ((42161, arbbase), (4663, rhbase)):
        chain_stats = {'offered': 0, 'state': 'WAITING_FRESH_EVIDENCE'}
        output['chains'][str(chain)] = chain_stats
        if not remaining:
            chain_stats['state'] = 'QUEUE_BACKPRESSURE'
            continue
        try:
            # Reserve a share for both chains even if Arbitrum has more pages.
            share = max(1, remaining * 3 // 4) if chain == 42161 else remaining
            active = queue.active_evidence_keys(str(chain))
            for summary, metadata, key in _candidate_jobs(base, chain):
                if key in active:
                    continue
                queue.enqueue(str(chain), summary, metadata, key)
                chain_stats['offered'] += 1
                output['offered'] += 1
                # Core enqueue owns permanent deduplication. Count only pending
                # growth against backpressure, so completed keys cannot hide new
                # work in another retained page.
                new_pending = sum(queue.pending_count(str(c)) for c in (42161, 4663))
                remaining = max(0, max_pending - new_pending)
                if not remaining or queue.pending_count(str(chain)) >= share:
                    break
            chain_stats['state'] = 'RETAINED_EVIDENCE_OFFERED' if chain_stats['offered'] else 'WAITING_FRESH_EVIDENCE'
        except (FileNotFoundError, sqlite3.Error) as error:
            chain_stats.update(state='WAITING_LOCAL_INDEX', reason=type(error).__name__)
        except (ValueError, KeyError, TypeError) as error:
            chain_stats.update(state='SOURCE_EVIDENCE_REJECTED', reason=str(error)[:160])
    return output


def _page_for_result(base, metadata, chain):
    start, end, sha = metadata['first_block'], metadata['last_block'], metadata['source_sha256']
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < start + MAX_PAGE_BLOCKS:
        raise ValueError('EVM_POOL_RESULT_RANGE')
    with _read_db(Path(base) / 'evidence/runtime.sqlite3') as runtime:
        page = runtime.execute("SELECT start,end,sha,proof FROM pages WHERE start=? AND state='indexed'", (start,)).fetchone()
        if page is None:
            raise FileNotFoundError('source page journal unavailable')
        if page['end'] != end or page['sha'] != sha:
            raise ValueError('EVM_POOL_RESULT_SOURCE_CHANGED')
        return dict(page), _proof(dict(page), chain)


def _native_check(check, graph, page, metadata, proof):
    start, end = page['start'], page['end']
    if check == 'verify_source':
        return dict(check=check, status='LOCAL_RECEIPT_METADATA_MATCH', evidence=proof,
                    limitation='Previously retained readback receipt checked; source bytes not fetched again.')
    if check == 'fetch_blocks':
        rows = [dict(r) for r in graph.execute(
            'SELECT number,hash,parent_hash,source_sha256 FROM blocks WHERE number BETWEEN ? AND ? ORDER BY number LIMIT ?',
            (start, end, MAX_PAGE_BLOCKS + 1))]
        valid = len(rows) == end - start + 1 and all(r['number'] == start + i and r['source_sha256'] == page['sha']
            and (not i or r['parent_hash'] == rows[i - 1]['hash']) for i, r in enumerate(rows))
        return dict(check=check, status='LOCAL_HEADER_RANGE_MATCH' if valid else 'LOCAL_HEADER_RANGE_MISMATCH',
            checked_blocks=len(rows), range=[start, end], headers_sha256=hashlib.sha256(canonical(rows).encode()).hexdigest(),
            limitation='Retained provider headers; no independent canonicality or new network fetch.')
    if check == 'follow_address':
        records = []
        for address in metadata.get('addresses', [])[:4]:
            if not isinstance(address, str) or not ADDRESS.fullmatch(address):
                raise ValueError('EVM_POOL_RESULT_ADDRESS')
            membership = graph.execute('SELECT chain_id FROM addresses WHERE address=?', (address,)).fetchone()
            outgoing = [dict(r) for r in graph.execute(
                'SELECT block,tx_hash,tx_index,recipient,asset,amount,kind,source_sha256 FROM edges WHERE sender=? ORDER BY block DESC LIMIT 4', (address,))]
            incoming = [dict(r) for r in graph.execute(
                'SELECT block,tx_hash,tx_index,sender,asset,amount,kind,source_sha256 FROM edges WHERE recipient=? ORDER BY block DESC LIMIT 4', (address,))]
            records.append(dict(address=address, indexed_membership=bool(membership), outgoing=outgoing, incoming=incoming,
                                scope='one_retained_local_shard', result_limit_each_direction=4))
        return dict(check=check, status='LOCAL_ADDRESS_ROUTES_QUERIED', records=records,
                    limitation='Bounded indexed observations, not current balance, full history, ownership, or same-funds attribution.')
    if check == 'decode_protocol':
        records = []
        for tx in metadata.get('transaction_samples', [])[:2]:
            records.extend(dict(r) for r in graph.execute(
                'SELECT block,tx_hash,kind,position,contract,topic0,source_sha256 FROM records WHERE tx_hash=? ORDER BY kind LIMIT 4', (tx['hash'],)))
        return dict(check=check, status='LOCAL_PROTOCOL_RECORDS_QUERIED', records=records,
                    limitation='Existing native classifications only; unknown ABI and protocol semantics remain unverified.')
    if check == 'fetch_receipts':
        samples = []
        for tx in metadata.get('transaction_samples', [])[:4]:
            row = graph.execute('SELECT hash,block,tx_index,status FROM transactions WHERE block=? AND tx_index=?', (tx['block'], tx['tx_index'])).fetchone()
            if row is None or row['hash'] != tx['hash']:
                raise ValueError('EVM_POOL_RESULT_TX_CHANGED')
            samples.append(dict(row))
        return dict(check=check, status='WAITING_SOURCE', local_status_samples=samples,
                    reason='Independent complete receipt bytes are not provided by the derived transaction table.')
    if check == 'fetch_traces':
        rows = [dict(r) for r in graph.execute(
            'SELECT number,trace_coverage FROM blocks WHERE number BETWEEN ? AND ? ORDER BY number LIMIT 32', (start, end))]
        return dict(check=check, status='WAITING_SOURCE', local_trace_coverage_sample=rows,
                    reason='Full internal transfer traces and independent completeness require source acquisition.')
    if check == 'verify_bridge':
        return dict(check=check, status='WAITING_SOURCE',
                    reason='Counterpart chain source, trusted message identities, and redemption evidence not supplied by this local page.')
    return dict(check=check, status='GAP_RETAINED',
                reason='Unresolved source, private ledger, trace, and attribution boundaries remain explicit.')


def consume_results(queue, arbbase, rhbase, max_jobs=16):
    """Run finite native checks, then retain explicit outcomes in the pool only."""
    if type(max_jobs) is not int or not 1 <= max_jobs <= 100:
        raise ValueError('EVM_POOL_CONSUMER_BOUND')
    output = {'consumed': 0, 'local_checks': 0, 'waiting_source': 0, 'mismatches': 0}
    for chain, base in ((42161, arbbase), (4663, rhbase)):
        share = max(1, (max_jobs + 1) // 2) if chain == 42161 else max_jobs - output['consumed']
        for job in queue.completed(str(chain), limit=share):
            outcome = dict(schema='economic-machine.evm-pool-native-checks.v1', chain_id=chain,
                performed_ms=time.time_ns() // 1000000, model_proposal_applied_as='bounded_read_only_checks',
                source_journals_mutated=False, fresh_blob_readback=False, independent_consensus_verified=False,
                full_history_verified=False, checks=[])
            try:
                summary, metadata, proposal = job['summary'], job['metadata'], job['proposal']
                validate_summary(summary)
                validate_investigation(proposal, summary)
                if (metadata.get('adapter') != 'evm-retained-v1' or metadata.get('chain_id') != chain
                        or summary['source_ids'] != [metadata.get('source_sha256')]):
                    raise ValueError('EVM_POOL_RESULT_IDENTITY')
                page, proof = _page_for_result(base, metadata, chain)
                # Receipt checks remain possible after the local graph is sealed;
                # other checks are retained as waiting instead of redownloading a
                # large shard outside native source admission.
                path = _graph_path(base, metadata['graph_relative'])
                with _read_db(path) as graph:
                    _identity(graph, chain)
                    indexed = graph.execute('SELECT start,end FROM pages WHERE source_sha256=?', (page['sha'],)).fetchone()
                    if indexed is None or tuple(indexed) != (page['start'], page['end']):
                        raise ValueError('EVM_POOL_RESULT_GRAPH_CHANGED')
                    for check in proposal['next_checks']:
                        result = _native_check(check, graph, page, metadata, proof)
                        outcome['checks'].append(result)
                        output['local_checks'] += int(result['status'].startswith('LOCAL_'))
                        output['waiting_source'] += int(result['status'] == 'WAITING_SOURCE')
                        output['mismatches'] += int('MISMATCH' in result['status'])
                outcome['status'] = ('EVIDENCE_MISMATCH' if any('MISMATCH' in r['status'] for r in outcome['checks'])
                                     else 'NATIVE_CHECKS_RECORDED')
            except (FileNotFoundError, sqlite3.Error) as error:
                outcome.update(status='WAITING_SOURCE', reason='Retained local graph rotated or bounded query deferred: ' + type(error).__name__)
                output['waiting_source'] += 1
            except (ValueError, KeyError, TypeError) as error:
                outcome.update(status='EVIDENCE_REJECTED', reason=str(error)[:200])
                output['mismatches'] += 1
            if len(canonical(outcome).encode()) > 32000:
                # Preserve every result/status and a digest of its complete local
                # observation while keeping the shared queue acknowledgement
                # bounded. This never upgrades an omitted or unavailable record.
                outcome['checks'] = [dict(check=r['check'], status=r['status'],
                    result_sha256=hashlib.sha256(canonical(r).encode()).hexdigest(),
                    details_omitted_for_journal_bound=True) for r in outcome['checks']]
                outcome['truncated'] = True
            queue.ack(job['id'], outcome)
            output['consumed'] += 1
            if output['consumed'] >= max_jobs:
                return output
    return output
