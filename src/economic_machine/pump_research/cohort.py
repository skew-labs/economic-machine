"""Frozen, provenance-bearing recent-launch research cohorts.

This module performs no network, model, or transaction calls. Provider market
capitalizations and creator fields remain claims. A sorted observed sample is
not evidence that the provider's complete token universe was searched.
"""
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re

from .store import address, canonical

DAY_MS = 86_400_000
WINDOW_MS = 30 * DAY_MS
ORIGINAL_DEADLINE_MS = 1792078753482
AUTHORIZATION = 'owner_recent30d_marketcap50_and_original10_forward_research'
SCHEMA = 'economic-machine.recent30d-marketcap-cohort.v1'
_SHA = re.compile(r'^[0-9a-f]{64}$')


def snapshot_sha256(snapshot):
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('COHORT_' + name)
    return value


def normalize_timestamp(value, unit):
    """Normalize an explicitly identified seconds/milliseconds field exactly."""
    if unit not in ('s', 'ms') or isinstance(value, bool):
        raise ValueError('COHORT_TIMESTAMP_UNIT')
    if not isinstance(value, (int, float, str, Decimal)) or len(str(value)) > 80:
        raise ValueError('COHORT_TIMESTAMP_VALUE')
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or
                len(number.as_tuple().digits) > 40 or
                abs(number.as_tuple().exponent) > 40):
            raise ValueError('COHORT_TIMESTAMP_VALUE')
        with localcontext() as ctx:
            ctx.prec = 80
            millis = number * (1000 if unit == 's' else 1)
            if millis != millis.to_integral_value() or millis > 2**63 - 1:
                raise ValueError('COHORT_TIMESTAMP_PRECISION')
            return int(millis)
    except InvalidOperation as error:
        raise ValueError('COHORT_TIMESTAMP_VALUE') from error


def _created(row):
    fields = []
    for field in ('created_ms', 'created_timestamp', 'created_at'):
        if field not in row:
            continue
        unit = 'ms' if field == 'created_ms' else row.get('timestamp_unit')
        fields.append(dict(field=field, value=row[field], unit=unit,
                           normalized_ms=normalize_timestamp(row[field], unit)))
    if not fields:
        raise ValueError('COHORT_TIMESTAMP_MISSING')
    if len({entry['normalized_ms'] for entry in fields}) != 1:
        raise ValueError('COHORT_TIMESTAMP_CONFLICT')
    return fields[0]['normalized_ms'], fields


def _cap(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('COHORT_USD_CAP_INVALID')
    if len(str(value)) > 512:
        raise ValueError('COHORT_USD_CAP_INVALID')
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('COHORT_USD_CAP_INVALID') from error
    if (not number.is_finite() or number <= 0 or
            len(number.as_tuple().digits) > 128 or
            abs(number.as_tuple().exponent) > 256):
        raise ValueError('COHORT_USD_CAP_INVALID')
    text = format(number, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text


def _json_copy(value):
    # Reject non-JSON data and non-finite floats rather than silently changing
    # the original provider record in the evidence snapshot.
    return json.loads(canonical(value))


def build_snapshot(pages, as_of_ms, limit=50):
    """Select up to 50 current-cap claims in [as_of - 30 days, as_of].

    A page contains ``provider``, ``fetched_ms``, ``sha256`` (retained response
    bytes), ``rows`` and optional JSON fetch metadata. Each row uses ``mint``,
    ``usd_market_cap`` and an explicit timestamp field: ``created_ms`` or
    ``created_timestamp``/``created_at`` with ``timestamp_unit`` s/ms. Page
    response hashes are retained assertions; ``rows_sha256`` binds the actual
    parsed rows supplied here. No ATH field is read as market capitalization.
    """
    _integer(as_of_ms, 'AS_OF')
    if as_of_ms < WINDOW_MS or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('COHORT_WINDOW_OR_LIMIT')
    cutoff = as_of_ms - WINDOW_MS
    groups, rejects, provenance = {}, [], []
    rows_seen = 0
    for page_number, supplied in enumerate(pages):
        page = _json_copy(supplied)
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list) or
                not isinstance(page.get('provider'), str) or not page['provider'].strip() or
                not isinstance(page.get('sha256'), str) or not _SHA.fullmatch(page['sha256'])):
            raise ValueError('COHORT_PAGE_PROVENANCE')
        _integer(page.get('fetched_ms'), 'FETCHED_MS')
        header = {k: v for k, v in page.items() if k != 'rows'}
        header.update(page_number=page_number, row_count=len(page['rows']),
                      rows_sha256=snapshot_sha256(page['rows']),
                      response_hash_verified_by_this_module=False)
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
                creator_state = ('provider_claim_unverified' if creator else
                                 'missing' if creator_value in (None, '') else 'invalid')
                normalized = dict(mint=mint, symbol=str(row.get('symbol') or ''),
                                  name=str(row.get('name') or ''), creator=creator,
                                  creator_status=creator_state, creator_original=creator_value,
                                  created_ms=created, usd_market_cap=cap)
                # Formatting differences (e.g. seconds vs milliseconds or
                # 1.0 vs 1) do not create conflicts in otherwise equal claims.
                identity = snapshot_sha256(normalized)
                item = dict(normalized, timestamp_originals=timestamps,
                            provider_row=row, source=source)
                groups.setdefault(mint, []).append((identity, item))
            except ValueError as error:
                rejects.append(dict(mint=mint, reason=str(error), source=source,
                                    provider_row=row))
                # A valid address with an unusable second snapshot is not
                # silently retained from another page's convenient value.
                if address(mint):
                    groups.setdefault(mint, []).append((None, None))

    eligible = []
    duplicate_rows = 0
    for mint, claims in groups.items():
        identities = {identity for identity, _ in claims}
        if None in identities or len(identities) != 1:
            rejects.append(dict(mint=mint, reason='COHORT_DUPLICATE_CONFLICT_OR_INVALID',
                                sources=[item['source'] for _, item in claims if item],
                                valid_claims=[item for _, item in claims if item],
                                occurrence_count=len(claims)))
            continue
        item = claims[0][1]
        duplicate_rows += len(claims) - 1
        item['observations'] = [dict(source=claim['source'],
                                     timestamp_originals=claim['timestamp_originals'],
                                     provider_row=claim['provider_row'])
                                for _, claim in claims]
        if not cutoff <= item['created_ms'] <= as_of_ms:
            rejects.append(dict(mint=mint, reason=('COHORT_CREATED_AFTER_CUTOFF'
                            if item['created_ms'] > as_of_ms else 'COHORT_CREATED_BEFORE_WINDOW'),
                                created_ms=item['created_ms'], observations=item['observations']))
            continue
        eligible.append(item)
    # Stable sort avoids Decimal unary negation rounding very precise claims.
    eligible.sort(key=lambda item: item['mint'])
    eligible.sort(key=lambda item: Decimal(item['usd_market_cap']), reverse=True)
    selected = []
    for rank, item in enumerate(eligible[:limit], 1):
        selected.append(dict(item, provider_rank=rank, observed_rank=rank,
                             rank_scope='eligible_observed_rows_only',
                             metric='current_usd_market_cap_provider_claim',
                             chain_verified=False))
    return dict(schema=SCHEMA, as_of_ms=as_of_ms, window_start_ms=cutoff,
                window_end_inclusive_ms=as_of_ms, window_days=30, limit=limit,
                metric='current_usd_market_cap_provider_claim', pages=provenance,
                selected=selected, rejections=rejects, rows_observed=rows_seen,
                eligible_distinct_mints=len(eligible), duplicate_equal_rows=duplicate_rows,
                eligible_unselected=len(eligible)-len(selected),
                ranking_verified=False, universe_complete=False,
                valuation_time='provider_fetch_times_in_pages',
                gaps=['provider_universe_completeness_unproven',
                      'global_current_cap_ranking_unproven',
                      'provider_valuations_not_synchronized_at_cutoff',
                      'launch_and_creator_claims_require_original_transaction'])


def _validate_activation(snapshot, grant, now_ms, deadline_ms):
    _integer(now_ms, 'NOW')
    _integer(deadline_ms, 'DEADLINE')
    digest = snapshot_sha256(snapshot)
    if (snapshot.get('schema') != SCHEMA or
            snapshot.get('window_days') != 30 or
            snapshot.get('metric') != 'current_usd_market_cap_provider_claim' or
            snapshot.get('ranking_verified') is not False or
            snapshot.get('universe_complete') is not False):
        raise ValueError('COHORT_SNAPSHOT_SCHEMA')
    as_of = _integer(snapshot.get('as_of_ms'), 'AS_OF')
    if (snapshot.get('window_start_ms') != as_of-WINDOW_MS or
            snapshot.get('window_end_inclusive_ms') != as_of or as_of > now_ms):
        raise ValueError('COHORT_SNAPSHOT_WINDOW')
    if (not isinstance(grant, dict) or grant.get('authorization') != AUTHORIZATION or
            grant.get('cutoff_ms') != as_of or
            grant.get('as_of_ms', as_of) != as_of or
            grant.get('window_start_ms') != as_of-WINDOW_MS or
            grant.get('snapshot_sha256') != digest or
            grant.get('deadline_ms') != deadline_ms or
            deadline_ms > ORIGINAL_DEADLINE_MS or now_ms >= deadline_ms or
            grant.get('trading') is not False or grant.get('paid_api') is not False):
        raise ValueError('COHORT_SUPPLEMENTAL_GRANT')
    rows = snapshot.get('selected')
    limit = snapshot.get('limit')
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50 or
            not isinstance(rows, list) or len(rows) > limit):
        raise ValueError('COHORT_SELECTED_BOUND')
    seen = set()
    for rank, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or not address(row.get('mint')) or
                row['mint'] in seen or row.get('observed_rank') != rank or
                row.get('provider_rank') != rank or
                row.get('metric') != snapshot['metric'] or row.get('chain_verified') is not False or
                row.get('creator') is not None and not address(row['creator'])):
            raise ValueError('COHORT_SELECTED_ROW')
        created = _integer(row.get('created_ms'), 'CREATED_MS')
        if not as_of-WINDOW_MS <= created <= as_of:
            raise ValueError('COHORT_SELECTED_WINDOW')
        _cap(row.get('usd_market_cap'))
        seen.add(row['mint'])
    ordered = sorted(rows, key=lambda row: row['mint'])
    ordered.sort(key=lambda row: Decimal(row['usd_market_cap']), reverse=True)
    if [r['mint'] for r in ordered] != [r['mint'] for r in rows]:
        raise ValueError('COHORT_SELECTED_ORDER')
    return digest


def activate(graph, snapshot, grant, now_ms, deadline_ms=ORIGINAL_DEADLINE_MS):
    """Append memberships and initial stages without rewriting original roots.

    A supplemental grant pins ``snapshot_sha256``, ``cutoff_ms`` and
    ``window_start_ms`` and contains the exact AUTHORIZATION string, unchanged
    ``deadline_ms``, ``trading: false`` and ``paid_api: false``. Existing-root
    memberships reuse their current investigation without enqueueing duplicates.
    Savepoints preserve an enclosing caller transaction, if one exists.
    """
    digest = _validate_activation(snapshot, grant, now_ms, deadline_ms)
    body, grant_body = canonical(snapshot), canonical(grant)
    db = graph.db
    db.execute('SAVEPOINT cohort_activation')
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS cohorts(
          id TEXT PRIMARY KEY,body TEXT NOT NULL,grant_body TEXT NOT NULL,
          grant_sha256 TEXT NOT NULL,activated_ms INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS cohort_memberships(
          cohort_id TEXT NOT NULL,mint TEXT NOT NULL,observed_rank INTEGER NOT NULL,
          existing_root INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(cohort_id,mint))''')
        db.execute('''CREATE TABLE IF NOT EXISTS cohort_root_stages(
          cohort_id TEXT NOT NULL,mint TEXT NOT NULL,stage TEXT NOT NULL,
          state TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(cohort_id,mint,stage))''')
        old = db.execute('SELECT body,grant_body FROM cohorts WHERE id=?', (digest,)).fetchone()
        if old:
            if old[0] != body or old[1] != grant_body:
                raise ValueError('COHORT_ACTIVATION_IDENTITY_CHANGED')
            db.execute('RELEASE SAVEPOINT cohort_activation')
            return dict(cohort_id=digest, already_active=True, roots_added=0,
                        existing_roots_preserved=0, launch_jobs_added=0,
                        missing_creator=0, core_jobs_added=0, selected=len(snapshot['selected']))
        db.execute('INSERT INTO cohorts VALUES(?,?,?,?,?)',
                   (digest, body, grant_body, snapshot_sha256(grant), now_ms))
        added = existing = queued = missing = core_queued = 0
        for row in snapshot['selected']:
            mint = row['mint']
            present = db.execute('SELECT body FROM roots WHERE mint=?', (mint,)).fetchone()
            job = None
            core_jobs = {}
            if present:
                existing += 1
                launch_state = 'existing_root_preserved'
            else:
                root = dict(row, cohort_id=digest, cohort_as_of_ms=snapshot['as_of_ms'],
                            evidence='cohort_snapshot:'+digest,
                            ath_status='provider_claim_unverified',
                            ath_metric_used=False, cohort_kind='recent30d_current_marketcap',
                            valuation_metric='current_usd_market_cap_provider_claim')
                db.execute('INSERT INTO roots VALUES(?,?)', (mint, canonical(root)))
                added += 1
                graph.node(mint, 'token', 'recent_launch_provider_claim')
                if row['creator']:
                    graph.node(row['creator'], 'account', 'provider_creator_unverified')
                    graph.edge(mint, row['creator'], mint, 'provider_creator_claim', None,
                               row['created_ms'], source='recent30d_marketcap_snapshot',
                               cohort_id=digest, snapshot_sha256=digest,
                               chain_verified=False, creator_verified=False,
                               ranking_verified=False, universe_complete=False)
                    job = graph.enqueue(mint, row['creator'], None, row['created_ms'],
                                        0, 'recover_launch')
                    queued += int(job is not None)
                    for source in ('core_forward', 'core_early'):
                        core_jobs[source] = graph.enqueue(mint, row['creator'], None,
                                                          row['created_ms'], 0, source)
                        core_queued += int(core_jobs[source] is not None)
                    launch_state = 'pending_original_launch'
                else:
                    missing += 1
                    launch_state = 'pending_missing_creator_metadata'
            db.execute('INSERT INTO cohort_memberships VALUES(?,?,?,?,?)',
                       (digest, mint, row['observed_rank'], int(present is not None), canonical(row)))
            for stage in ('launch', 'early_buyers', 'forward_funds', 'reentry', 'current_wallet'):
                state = launch_state if stage == 'launch' else 'unverified'
                detail = dict(job_id=job if stage == 'launch' else None,
                              core_jobs=core_jobs,
                              time_anchor_is_provider_claim=not bool(present),
                              creator_status=row['creator_status'],
                              existing_root_preserved=present is not None,
                              complete=False, same_funds_proven=False)
                db.execute('INSERT INTO cohort_root_stages VALUES(?,?,?,?,?)',
                           (digest, mint, stage, state, canonical(detail)))
        db.execute('RELEASE SAVEPOINT cohort_activation')
        return dict(cohort_id=digest, already_active=False, roots_added=added,
                    existing_roots_preserved=existing, launch_jobs_added=queued,
                    core_jobs_added=core_queued, missing_creator=missing,
                    selected=len(snapshot['selected']))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT cohort_activation')
        db.execute('RELEASE SAVEPOINT cohort_activation')
        raise
