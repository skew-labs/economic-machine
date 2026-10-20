"""Append-only 365-day/current-cap research cohort, at most 1,000 mints.

Selection uses observed provider claims, never ATH, and never asserts a complete
token universe. Original ten-token and recent-month cohorts are not replaced.
This module performs no network, model, transaction or service operations.
"""
from decimal import Decimal

from .cohort import (
    DAY_MS, ORIGINAL_DEADLINE_MS, _SHA, _cap, _created, _integer, _json_copy,
    snapshot_sha256,
)
from .store import address, canonical

WINDOW_MS = 365 * DAY_MS
AUTHORIZATION = 'owner_recent365d_marketcap1000_and_existing_cohorts_forward_research'
SCHEMA = 'economic-machine.recent365d-marketcap-cohort.v1'


def build_snapshot(pages, as_of_ms, limit=1000):
    """Rank exact current-cap claims observed in an inclusive 365-day window."""
    _integer(as_of_ms, 'AS_OF')
    if as_of_ms < WINDOW_MS or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('ANNUAL_COHORT_WINDOW_OR_LIMIT')
    cutoff = as_of_ms - WINDOW_MS
    groups, rejects, provenance = {}, [], []
    rows_seen = 0
    for page_number, supplied in enumerate(pages):
        page = _json_copy(supplied)
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or not isinstance(page.get('provider'), str) or not page['provider'].strip()
                or not isinstance(page.get('sha256'), str) or not _SHA.fullmatch(page['sha256'])):
            raise ValueError('ANNUAL_COHORT_PAGE_PROVENANCE')
        _integer(page.get('fetched_ms'), 'FETCHED_MS')
        header = {k: v for k, v in page.items() if k != 'rows'}
        header.update(page_number=page_number, row_count=len(page['rows']),
            rows_sha256=snapshot_sha256(page['rows']), response_hash_verified_by_this_module=False)
        provenance.append(header)
        for row_number, row in enumerate(page['rows']):
            rows_seen += 1
            source = dict(page_number=page_number, row_number=row_number,
                page_sha256=page['sha256'], fetched_ms=page['fetched_ms'],
                provider=page['provider'], row_sha256=snapshot_sha256(row))
            mint = row.get('mint') if isinstance(row, dict) else None
            try:
                if not isinstance(row, dict) or not address(mint):
                    raise ValueError('COHORT_MINT_INVALID')
                created, timestamps = _created(row)
                cap = _cap(row.get('usd_market_cap'))
                creator_value = row.get('creator')
                creator = creator_value if address(creator_value) else None
                creator_state = 'provider_claim_unverified' if creator else 'missing' if creator_value in (None, '') else 'invalid'
                normalized = dict(mint=mint, symbol=str(row.get('symbol') or ''),
                    name=str(row.get('name') or ''), creator=creator,
                    creator_status=creator_state, creator_original=creator_value,
                    created_ms=created, usd_market_cap=cap)
                ident = snapshot_sha256(normalized)
                groups.setdefault(mint, []).append((ident, dict(normalized,
                    timestamp_originals=timestamps, provider_row=row, source=source)))
            except ValueError as error:
                rejects.append(dict(mint=mint, reason=str(error), source=source, provider_row=row))
                if address(mint):
                    groups.setdefault(mint, []).append((None, None))
    eligible = []; duplicate_rows = 0
    for mint, claims in groups.items():
        identities = {ident for ident, _ in claims}
        if None in identities or len(identities) != 1:
            rejects.append(dict(mint=mint, reason='COHORT_DUPLICATE_CONFLICT_OR_INVALID',
                sources=[item['source'] for _, item in claims if item],
                valid_claims=[item for _, item in claims if item], occurrence_count=len(claims)))
            continue
        item = claims[0][1]; duplicate_rows += len(claims) - 1
        item['observations'] = [dict(source=claim['source'], timestamp_originals=claim['timestamp_originals'],
            provider_row=claim['provider_row']) for _, claim in claims]
        if not cutoff <= item['created_ms'] <= as_of_ms:
            rejects.append(dict(mint=mint, reason='COHORT_CREATED_AFTER_CUTOFF' if item['created_ms'] > as_of_ms else 'COHORT_CREATED_BEFORE_WINDOW',
                created_ms=item['created_ms'], observations=item['observations']))
            continue
        eligible.append(item)
    eligible.sort(key=lambda item: item['mint'])
    eligible.sort(key=lambda item: Decimal(item['usd_market_cap']), reverse=True)
    selected = [dict(item, provider_rank=rank, observed_rank=rank,
        rank_scope='eligible_observed_rows_only', metric='current_usd_market_cap_provider_claim',
        chain_verified=False) for rank, item in enumerate(eligible[:limit], 1)]
    return dict(schema=SCHEMA, as_of_ms=as_of_ms, window_start_ms=cutoff,
        window_end_inclusive_ms=as_of_ms, window_days=365, limit=limit,
        metric='current_usd_market_cap_provider_claim', pages=provenance,
        selected=selected, rejections=rejects, rows_observed=rows_seen,
        eligible_distinct_mints=len(eligible), duplicate_equal_rows=duplicate_rows,
        eligible_unselected=len(eligible) - len(selected), ranking_verified=False,
        universe_complete=False, target_selection_filled=len(selected) == limit,
        valuation_time='provider_fetch_times_in_pages',
        gaps=['provider_universe_completeness_unproven', 'global_current_cap_ranking_unproven',
              'provider_valuations_not_synchronized_at_cutoff',
              'launch_and_creator_claims_require_original_transaction'])


def _validate_activation(snapshot, grant, now_ms, deadline_ms):
    _integer(now_ms, 'NOW'); _integer(deadline_ms, 'DEADLINE')
    digest = snapshot_sha256(snapshot)
    if (not isinstance(snapshot, dict) or snapshot.get('schema') != SCHEMA
            or snapshot.get('window_days') != 365
            or snapshot.get('metric') != 'current_usd_market_cap_provider_claim'
            or snapshot.get('ranking_verified') is not False or snapshot.get('universe_complete') is not False):
        raise ValueError('ANNUAL_COHORT_SNAPSHOT_SCHEMA')
    as_of = _integer(snapshot.get('as_of_ms'), 'AS_OF')
    if (as_of < WINDOW_MS or snapshot.get('window_start_ms') != as_of - WINDOW_MS
            or snapshot.get('window_end_inclusive_ms') != as_of or as_of > now_ms):
        raise ValueError('ANNUAL_COHORT_SNAPSHOT_WINDOW')
    if (not isinstance(grant, dict) or grant.get('authorization') != AUTHORIZATION
            or grant.get('cutoff_ms') != as_of or grant.get('as_of_ms', as_of) != as_of
            or grant.get('window_start_ms') != as_of - WINDOW_MS
            or grant.get('snapshot_sha256') != digest or grant.get('deadline_ms') != deadline_ms
            or deadline_ms != ORIGINAL_DEADLINE_MS or now_ms >= deadline_ms
            or grant.get('trading') is not False or grant.get('paid_api') is not False):
        raise ValueError('ANNUAL_COHORT_SUPPLEMENTAL_GRANT')
    rows, limit = snapshot.get('selected'), snapshot.get('limit')
    if type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(rows, list) or len(rows) > limit:
        raise ValueError('ANNUAL_COHORT_SELECTED_BOUND')
    seen = set()
    for rank, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or not address(row.get('mint')) or row['mint'] in seen
                or row.get('observed_rank') != rank or row.get('provider_rank') != rank
                or row.get('rank_scope') != 'eligible_observed_rows_only'
                or row.get('metric') != snapshot['metric'] or row.get('chain_verified') is not False
                or row.get('creator') is not None and not address(row['creator'])):
            raise ValueError('ANNUAL_COHORT_SELECTED_ROW')
        if row.get('creator_status') not in ('provider_claim_unverified', 'missing', 'invalid'):
            raise ValueError('ANNUAL_COHORT_CREATOR_STATUS')
        if bool(row['creator']) != (row['creator_status'] == 'provider_claim_unverified'):
            raise ValueError('ANNUAL_COHORT_CREATOR_STATUS')
        created = _integer(row.get('created_ms'), 'CREATED_MS')
        if not as_of - WINDOW_MS <= created <= as_of:
            raise ValueError('ANNUAL_COHORT_SELECTED_WINDOW')
        _cap(row.get('usd_market_cap')); seen.add(row['mint'])
    ordered = sorted(rows, key=lambda row: row['mint'])
    ordered.sort(key=lambda row: Decimal(row['usd_market_cap']), reverse=True)
    if [r['mint'] for r in rows] != [r['mint'] for r in ordered]:
        raise ValueError('ANNUAL_COHORT_SELECTED_ORDER')
    return digest


def activate(graph, snapshot, grant, now_ms, deadline_ms=ORIGINAL_DEADLINE_MS):
    """Append memberships while preserving every pre-existing root and cursor.

    Missing creator data may seed a mint/timestamp *discovery* job because
    Graph.recover_launch searches raw create instructions by root mint. The
    mint is never labeled as creator or used for a creator-funding query.
    """
    digest = _validate_activation(snapshot, grant, now_ms, deadline_ms)
    body, grant_body = canonical(snapshot), canonical(grant); db = graph.db
    db.execute('SAVEPOINT annual_cohort_activation')
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS cohorts(id TEXT PRIMARY KEY,body TEXT NOT NULL,
            grant_body TEXT NOT NULL,grant_sha256 TEXT NOT NULL,activated_ms INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS cohort_memberships(cohort_id TEXT NOT NULL,mint TEXT NOT NULL,
            observed_rank INTEGER NOT NULL,existing_root INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(cohort_id,mint))''')
        db.execute('''CREATE TABLE IF NOT EXISTS cohort_root_stages(cohort_id TEXT NOT NULL,mint TEXT NOT NULL,
            stage TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(cohort_id,mint,stage))''')
        old = db.execute('SELECT body,grant_body FROM cohorts WHERE id=?', (digest,)).fetchone()
        if old:
            if old[0] != body or old[1] != grant_body:
                raise ValueError('ANNUAL_COHORT_ACTIVATION_IDENTITY_CHANGED')
            db.execute('RELEASE SAVEPOINT annual_cohort_activation')
            return dict(cohort_id=digest, already_active=True, roots_added=0, existing_roots_preserved=0,
                launch_jobs_added=0, mint_discovery_jobs_added=0, core_jobs_added=0, missing_creator=0,
                selected=len(snapshot['selected']))
        db.execute('INSERT INTO cohorts VALUES(?,?,?,?,?)', (digest, body, grant_body, snapshot_sha256(grant), now_ms))
        added = existing = queued = discovery = missing = core_queued = 0
        for row in snapshot['selected']:
            mint = row['mint']; present = db.execute('SELECT body FROM roots WHERE mint=?', (mint,)).fetchone()
            job = None; core_jobs = {}; lookup_role = None
            if present:
                existing += 1; launch_state = 'existing_root_preserved'
            else:
                root = dict(row, cohort_id=digest, cohort_as_of_ms=snapshot['as_of_ms'],
                    evidence='cohort_snapshot:' + digest, ath_status='provider_claim_unverified',
                    ath_metric_used=False, cohort_kind='recent365d_current_marketcap',
                    valuation_metric='current_usd_market_cap_provider_claim')
                db.execute('INSERT INTO roots VALUES(?,?)', (mint, canonical(root))); added += 1
                graph.node(mint, 'token', 'research_launch_provider_claim')
                if row['creator']:
                    who = row['creator']; lookup_role = 'provider_creator_claim'
                    graph.node(who, 'account', 'provider_creator_unverified')
                    graph.edge(mint, who, mint, 'provider_creator_claim', None, row['created_ms'],
                        source='recent365d_marketcap_snapshot', cohort_id=digest, snapshot_sha256=digest,
                        chain_verified=False, creator_verified=False, ranking_verified=False, universe_complete=False)
                    for source in ('core_forward', 'core_early'):
                        core_jobs[source] = graph.enqueue(mint, who, None, row['created_ms'], 0, source)
                        core_queued += int(core_jobs[source] is not None)
                    launch_state = 'pending_original_launch'
                else:
                    who = mint; lookup_role = 'token_identifier_not_creator'; missing += 1
                    launch_state = 'pending_mint_launch_discovery'
                job = graph.enqueue(mint, who, None, row['created_ms'], 0, 'recover_launch')
                queued += int(job is not None); discovery += int(job is not None and row['creator'] is None)
                graph.set('cohort_launch_lookup:' + job, dict(cohort_id=digest, mint=mint,
                    lookup_role=lookup_role, creator_known=False, creator_status=row['creator_status'],
                    timestamp_is_provider_claim=True, raw_create_required=True))
            db.execute('INSERT INTO cohort_memberships VALUES(?,?,?,?,?)',
                (digest, mint, row['observed_rank'], int(present is not None), canonical(row)))
            for stage in ('launch', 'early_buyers', 'forward_funds', 'reentry', 'current_wallet'):
                detail = dict(job_id=job if stage == 'launch' else None, core_jobs=core_jobs,
                    lookup_role=lookup_role, time_anchor_is_provider_claim=not bool(present),
                    creator_status=row['creator_status'], existing_root_preserved=present is not None,
                    complete=False, same_funds_proven=False)
                db.execute('INSERT INTO cohort_root_stages VALUES(?,?,?,?,?)',
                    (digest, mint, stage, launch_state if stage == 'launch' else 'unverified', canonical(detail)))
        db.execute('RELEASE SAVEPOINT annual_cohort_activation')
        return dict(cohort_id=digest, already_active=False, roots_added=added, existing_roots_preserved=existing,
            launch_jobs_added=queued, mint_discovery_jobs_added=discovery, core_jobs_added=core_queued,
            missing_creator=missing, selected=len(snapshot['selected']))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT annual_cohort_activation')
        db.execute('RELEASE SAVEPOINT annual_cohort_activation')
        raise
