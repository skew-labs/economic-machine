"""Size the frozen provider inventory without inventing chain completeness.

Old Faithful's report labels GiB as GB and TiB as TB. Its generator uses
max(1, round(content_length / 2**30)); the interval below is conditional on
that published generator, never an exact transfer or transaction denominator.
"""
import hashlib
import re

GIB = 1024**3
TIB = 1024**4
GENERATOR_COMMIT = '9ca9ec15983c61efb83b1a839d2fe2384875e408'
GENERATOR_SHA256 = 'd8fa31211a5a2aaa9ceac776e4864583205e2c235983f7ef9361498e360d8875'


def rounded_gib_bounds(label):
    m = re.fullmatch(r'([0-9,]+) GB', label)
    if not m:
        return None
    value = int(m[1].replace(',', ''))
    if value < 1:
        raise ValueError('WORKLOAD_SIZE_VALUE')
    # Inclusive and deliberately conservative about Python tie rounding.
    return dict(reported_gib=value,
                lower_bytes=1 if value == 1 else value*GIB-GIB//2,
                upper_bytes=value*GIB+GIB//2)


def parse_report(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 1024**2:
        raise ValueError('WORKLOAD_REPORT_BOUND')
    rows = []
    seen = set()
    for line in raw.decode('utf-8').splitlines():
        if not re.match(r'^\|\s*\d+\s*\|', line):
            continue
        cols = [c.strip() for c in line.strip('|').split('|')]
        if len(cols) not in (10, 11):
            raise ValueError('WORKLOAD_REPORT_COLUMNS')
        epoch = int(cols[0])
        if epoch in seen or not 0 <= epoch <= 100000:
            raise ValueError('WORKLOAD_REPORT_EPOCH')
        seen.add(epoch)
        expected = f'https://files.old-faithful.net/{epoch}/epoch-{epoch}.car'
        links = re.findall(r'\]\(([^)]+)\)', cols[1])
        if links and links != [expected]:
            raise ValueError('WORKLOAD_REPORT_SOURCE')
        bounds = rounded_gib_bounds(cols[4])
        if links and bounds is None:
            raise ValueError('WORKLOAD_REPORT_SIZE_MISSING')
        if not links and bounds is not None:
            raise ValueError('WORKLOAD_REPORT_ORPHAN_SIZE')
        rows.append(dict(epoch=epoch, car_url=expected if links else None,
                         provider_state='available' if links else
                         ('ongoing' if cols[1] == 'ongoing' else 'missing'),
                         car_size=bounds, generic_indexes_label=cols[8],
                         tx_metadata_validation_label=cols[5],
                         row_sha256=hashlib.sha256(line.encode()).hexdigest()))
    if not rows:
        raise ValueError('WORKLOAD_REPORT_EMPTY')
    return sorted(rows, key=lambda r: r['epoch'])


def estimate(rows, *, known_exact=(), available_seconds, cloud_cap_bytes,
             storage_krw_per_gib=60, read_krw_per_gib=200):
    if not rows or not available_seconds > 0 or cloud_cap_bytes <= 0:
        raise ValueError('WORKLOAD_ESTIMATE_BOUND')
    exact = {}
    by_epoch = {r['epoch']: r for r in rows}
    for item in known_exact:
        epoch, size = item['epoch'], item['bytes']
        if epoch in exact or type(epoch) is not int or type(size) is not int or size <= 0:
            raise ValueError('WORKLOAD_EXACT_IDENTITY')
        if epoch not in by_epoch or by_epoch[epoch]['car_url'] != item['url']:
            raise ValueError('WORKLOAD_EXACT_SOURCE')
        interval = by_epoch[epoch]['car_size']
        if interval is None or not interval['lower_bytes'] <= size <= interval['upper_bytes']:
            raise ValueError('WORKLOAD_REPORT_HEAD_DISAGREEMENT')
        exact[epoch] = size
    available = [r for r in rows if r['car_size'] is not None]
    lower = sum(exact.get(r['epoch'], r['car_size']['lower_bytes']) for r in available)
    upper = sum(exact.get(r['epoch'], r['car_size']['upper_bytes']) for r in available)
    return dict(
        scope='frozen_provider_inventory', epoch_count=len(rows),
        first_epoch=min(by_epoch), last_epoch=max(by_epoch),
        provider_available_epochs=len(available),
        missing_epochs=[r['epoch'] for r in rows if r['provider_state'] == 'missing'],
        ongoing_epochs=[r['epoch'] for r in rows if r['provider_state'] == 'ongoing'],
        reported_car_gib=sum(r['car_size']['reported_gib'] for r in available),
        car_bytes_lower=lower, car_bytes_upper=upper,
        exact_provider_size_epochs=sorted(exact), exact_provider_size_bytes=sum(exact.values()),
        exact_all_car_bytes_known=len(exact) == len(rows),
        size_interval_basis='conditional_on_pinned_report_generator_rounding_and_provider_claims',
        available_seconds=available_seconds,
        full_car_minimum_bytes_per_second=lower/available_seconds,
        full_car_bytes_per_second_with_20pct_time_headroom=lower/(available_seconds*.8),
        full_car_storage_vs_current_cloud_cap=lower/cloud_cap_bytes,
        full_car_storage_monthly_reserve_lower_krw=lower/GIB*storage_krw_per_gib,
        full_car_one_blob_read_reserve_lower_krw=lower/GIB*read_krw_per_gib,
        full_car_gross_bytes_not_remaining=True,
        derived_graph_bytes=None, required_selective_fetch_bytes=None,
        remaining_transactions=None, required_transactions_per_second=None,
        whole_wallet_history_complete=False, independent_consensus_verified=False,
        unknowns=['ongoing_and_missing_provider_epochs', 'exact_per_epoch_car_sizes',
                  'transaction_and_non_vote_denominator', 'early_metadata_gaps',
                  'derived_graph_size_and_projection_ratio', 'all_required_cross_program_semantics'],
    )


def interval_rate(first, last, *, bytes_key='received_bytes', time_key='at_ms'):
    """Same-object monotonically increasing acquisition only, including waits."""
    if first.get('epoch') != last.get('epoch') or first.get('total_bytes') != last.get('total_bytes'):
        raise ValueError('WORKLOAD_RATE_DIFFERENT_OBJECT')
    elapsed = (last[time_key]-first[time_key])/1000
    amount = last[bytes_key]-first[bytes_key]
    if elapsed <= 0 or amount < 0:
        raise ValueError('WORKLOAD_RATE_ORDER')
    return dict(elapsed_seconds=elapsed, received_bytes_delta=amount,
                bytes_per_second=amount/elapsed,
                scope='same_gsfa_object_acquisition_including_provider_waits',
                whole_car_or_search_throughput=False)
