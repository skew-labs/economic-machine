"""Collect a bounded free current-cap sample for the approved annual cohort.

No activation, model calls, credentials or transactions. Existing recent-50
response files are reused by reference and verified against their receipts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from economic_machine.pump_research.annual_cohort import (
    AUTHORIZATION, ORIGINAL_DEADLINE_MS, build_snapshot, snapshot_sha256,
)
from economic_machine.pump_research.archive import NoRedirect
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.portal_history import RETRY_STATUSES, _retry_delay
from economic_machine.pump_research.store import atomic, canonical

MAX_RESPONSE = 2 * 1024 ** 2
MAX_ROWS = 3000
PAGE_SIZE = 50
CHAIN = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'
PARENT_SHA = '7c2f063d9834a86ea1992237d4983c9c3c295730d5a32f0b96924a30ddc597df'


def page_url(offset):
    if type(offset) is not int or offset < 0 or offset >= MAX_ROWS or offset % PAGE_SIZE:
        raise ValueError('ANNUAL_COLLECT_OFFSET')
    return f'https://frontend-api-v3.pump.fun/coins?offset={offset}&limit=50&sort=market_cap&order=DESC&includeNsfw=true'


def load_page(path, receipt, offset, base):
    if not path.exists() or not receipt.exists():
        raise ValueError('ANNUAL_COLLECT_CACHE_PAIR_MISSING')
    if path.stat().st_size > MAX_RESPONSE or receipt.stat().st_size > 65536:
        raise ValueError('ANNUAL_COLLECT_CACHE_BOUND')
    info = json.loads(receipt.read_bytes()); raw = path.read_bytes()
    if (info.get('url') != page_url(offset) or info.get('status') != 200
            or type(info.get('at_ms')) is not int or info.get('bytes') != len(raw)
            or info.get('sha256') != hashlib.sha256(raw).hexdigest()):
        raise ValueError('ANNUAL_COLLECT_RECEIPT_MISMATCH')
    rows = json.loads(raw, parse_float=str)
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise ValueError('ANNUAL_COLLECT_ROWS')
    for row in rows:
        if not isinstance(row, dict) or row.get('chain_id') != CHAIN:
            raise ValueError('ANNUAL_COLLECT_CHAIN')
        # Public endpoint documents this field in milliseconds. The original
        # response hash is retained; the added unit is explicit parse metadata.
        row['timestamp_unit'] = 'ms'
    return dict(provider='pump.fun_public_frontend', fetched_ms=info['at_ms'],
        sha256=info['sha256'], rows=rows, url=info['url'],
        raw_path=str(path.relative_to(base)), receipt_path=str(receipt.relative_to(base)),
        provider_sort='market_cap_quote_units', requested_offset=offset,
        pagination_snapshot_isolation=False, response_hash_verified_by_collector=True)


def fetch_page(out, offset, scope):
    path = out / f'cap-page-{offset:04}.json'; receipt = path.with_suffix('.receipt.json')
    if path.exists() or receipt.exists():
        if path.exists() and receipt.exists():
            return path, receipt
        raise ValueError('ANNUAL_COLLECT_INCOMPLETE_PAIR_PRESERVED')
    throttle = out / 'provider-backoff.json'
    while True:
        state = json.loads(throttle.read_bytes()) if throttle.exists() else {}
        while time.time() < state.get('not_before', 0):
            scope.guard()
            time.sleep(max(0, min(1, state['not_before'] - time.time())))
        scope.reserve(MAX_RESPONSE + 1, 'annual1000:current_market_cap_page:' + str(offset))
        at = time.time_ns() // 1_000_000
        attempt = out / f'cap-page-{offset:04}.attempt-{at}.json'
        note = dict(offset=offset, url=page_url(offset), at_ms=at,
            reserved_bytes=MAX_RESPONSE + 1, state='reserved', paid_api=False)
        atomic(attempt, note)
        request = urllib.request.Request(page_url(offset), headers={
            'User-Agent': 'EconomicMachine-Research/1.0', 'Accept-Encoding': 'identity'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
                raw = response.read(MAX_RESPONSE + 1); status = response.status
            with path.open('xb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            info = dict(url=page_url(offset), at_ms=at, status=status, bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), reserved_bytes=MAX_RESPONSE + 1,
                attempt_receipt=attempt.name)
            atomic(receipt, info)
            atomic(attempt, dict(note, state='received', sha256=info['sha256'], response_bytes=len(raw), status=status))
            atomic(throttle, dict(not_before=time.time() + 1, attempts=0, status=status))
            if status != 200 or len(raw) > MAX_RESPONSE:
                raise ValueError('ANNUAL_COLLECT_RESPONSE_BOUND_OR_STATUS')
            return path, receipt
        except urllib.error.HTTPError as error:
            count = state.get('attempts', 0) + 1
            atomic(attempt, dict(note, state='http_error', status=error.code,
                retry_after=error.headers.get('Retry-After') if error.headers else None,
                failed_reservation_retained=True))
            if error.code in RETRY_STATUSES:
                atomic(throttle, dict(not_before=time.time() + _retry_delay(error, count),
                    attempts=count, status=error.code, failed_reservation_retained=True))
                if count < 3:
                    continue
            raise


def freeze_grant(out, snapshot, scope):
    grant = dict(authorization=AUTHORIZATION, cutoff_ms=snapshot['as_of_ms'],
        window_start_ms=snapshot['window_start_ms'], snapshot_sha256=snapshot_sha256(snapshot),
        deadline_ms=scope.grant['deadline_ms'], trading=False, paid_api=False, new_server=False,
        parent_grant_sha256=scope.hash, total_storage_bytes=scope.storage_limit,
        maximum_cpu_percent=100, model='meta/muse-spark-1.3-contributor',
        total_model_call_limit=None, total_model_cost_limit=None,
        authority='Owner requested prior365day launches current-marketcap1000 plus retained original10/recent50; public research only; original resource/time limits preserved.')
    path = out / 'grant.json'
    if path.exists():
        if canonical(json.loads(path.read_bytes())) != canonical(grant):
            raise ValueError('ANNUAL_COLLECT_GRANT_CHANGED')
    else:
        atomic(path, grant)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pin = out / 'grant.sha256'
    if pin.exists():
        if pin.read_text().strip() != digest:
            raise ValueError('ANNUAL_COLLECT_GRANT_PIN_CHANGED')
    else:
        with pin.open('x') as handle:
            handle.write(digest + '\n'); handle.flush(); os.fsync(handle.fileno())
    return grant


def collect(base):
    base = Path(base); out = base / 'evidence/annual-year-v1'; out.mkdir(exist_ok=True)
    scope = Scope(base, base / 'evidence/core-trace-v1/grant.json')
    try:
        if (scope.hash != PARENT_SHA or scope.grant['deadline_ms'] != ORIGINAL_DEADLINE_MS
                or scope.storage_limit != 96 * 1024 ** 3):
            raise ValueError('ANNUAL_COLLECT_PARENT_SCOPE')
        cutoff = out / 'cutoff.json'
        if not cutoff.exists():
            atomic(cutoff, dict(as_of_ms=time.time_ns() // 1_000_000,
                window_days=365, parent_grant_sha256=scope.hash, original_deadline_ms=ORIGINAL_DEADLINE_MS))
        frozen_cutoff = json.loads(cutoff.read_bytes()); asof = frozen_cutoff['as_of_ms']
        if (type(asof) is not int or frozen_cutoff.get('window_days') != 365
                or frozen_cutoff.get('parent_grant_sha256') != scope.hash
                or frozen_cutoff.get('original_deadline_ms') != ORIGINAL_DEADLINE_MS
                or asof >= ORIGINAL_DEADLINE_MS or asof > time.time_ns() // 1_000_000):
            raise ValueError('ANNUAL_COLLECT_CUTOFF')
        frozen = out / 'snapshot.json'
        if frozen.exists():
            snapshot = json.loads(frozen.read_bytes())
            if snapshot.get('as_of_ms') != asof or snapshot.get('window_days') != 365:
                raise ValueError('ANNUAL_COLLECT_SNAPSHOT_CUTOFF')
            freeze_grant(out, snapshot, scope)
            return dict(state='SNAPSHOT_PRESERVED', sha256=snapshot_sha256(snapshot), selected=len(snapshot['selected']))
        pages = []; first_sufficient_offset = None; stop_reason = 'MAXIMUM_3000_ROWS_OBSERVED'
        for offset in range(0, MAX_ROWS, PAGE_SIZE):
            scope.guard()
            if offset < 500:
                path = base / 'evidence/recent50-v1' / f'cap-page-{offset:04}.json'
                receipt = path.with_suffix('.receipt.json')
            else:
                path, receipt = fetch_page(out, offset, scope)
            page = load_page(path, receipt, offset, base); pages.append(page)
            snapshot = build_snapshot(pages, asof, limit=1000)
            if snapshot['eligible_distinct_mints'] >= 1000:
                if first_sufficient_offset is None:
                    first_sufficient_offset = offset
            else:
                first_sufficient_offset = None
            atomic(out / 'collection-status.json', dict(state='COLLECTING', at_ms=time.time_ns() // 1_000_000,
                cutoff_ms=asof, last_offset=offset, rows_observed=snapshot['rows_observed'],
                eligible=snapshot['eligible_distinct_mints'], selected=len(snapshot['selected']),
                first_sufficient_offset=first_sufficient_offset, maximum_rows=MAX_ROWS,
                ranking_verified=False, universe_complete=False, paid_api=False))
            if len(page['rows']) < PAGE_SIZE:
                stop_reason = 'PROVIDER_PAGE_EXHAUSTED_NOT_UNIVERSE_PROOF'; break
            if first_sufficient_offset is not None and offset >= first_sufficient_offset + 2 * PAGE_SIZE:
                stop_reason = 'OBSERVED_1000_PLUS_TWO_PAGES'; break
        snapshot['collection_stop_reason'] = stop_reason
        snapshot['maximum_rows_requested'] = MAX_ROWS
        snapshot['existing_response_files_reused'] = [p['raw_path'] for p in pages if p['requested_offset'] < 500]
        atomic(frozen, snapshot)
        freeze_grant(out, snapshot, scope)
        result = dict(state='SNAPSHOT_READY', selected=len(snapshot['selected']), rows=snapshot['rows_observed'],
            eligible=snapshot['eligible_distinct_mints'], ranking_verified=False, universe_complete=False,
            stop_reason=stop_reason, sha256=snapshot_sha256(snapshot), paid_api=False)
        atomic(out / 'collection-status.json', dict(result, at_ms=time.time_ns() // 1_000_000, cutoff_ms=asof))
        return result
    finally:
        scope.close()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--base', required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.base)), flush=True)


if __name__ == '__main__':
    main()
