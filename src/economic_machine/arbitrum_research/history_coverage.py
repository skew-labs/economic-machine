"""Union of indexed block intervals, separate from complete wallet history.

These are catalogue/provider claims already admitted by their own verifiers.
This calculation does not re-verify bytes, internal traces or consensus.
"""


def intervals(rows, total):
    if type(total) is not int or total < 1:
        raise ValueError('COVERAGE_TOTAL')
    bounded = []
    for first, last in rows:
        if type(first) is not int or type(last) is not int or first < 0 or last < first:
            raise ValueError('COVERAGE_INTERVAL')
        if first < total:
            bounded.append((first, min(last, total - 1)))
    merged = []
    for first, last in sorted(bounded):
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(last, merged[-1][1])
        else:
            merged.append([first, last])
    return merged


def summary(provider, shards, total, now_ms, deadline_ms, previous=None):
    p = intervals(provider, total)
    s = intervals(shards, total)
    union = intervals(p + s, total)
    count = lambda rs: sum(b - a + 1 for a, b in rs)
    completed = count(union)
    seconds = max(0, (deadline_ms - now_ms) / 1000)
    remaining = total - completed
    rate = None
    if previous and previous.get('total_blocks') == total and now_ms > previous.get('at_ms', now_ms):
        delta = completed - previous['nonoverlapping_indexed_blocks']
        if delta >= 0:
            rate = delta / ((now_ms - previous['at_ms']) / 1000)
    return dict(at_ms=now_ms, total_blocks=total,
        nonoverlapping_indexed_blocks=completed,
        provider_indexed_blocks=count(p), snapshot_indexed_blocks=count(s),
        overlap_blocks=count(p) + count(s) - completed,
        indexed_block_percentage=100 * completed / total,
        remaining_blocks=remaining, merged_ranges=union,
        observed_interval_blocks_per_second=rate,
        required_blocks_per_second=remaining / seconds if seconds else None,
        required_with_20_percent_time_margin=remaining / (seconds * .8) if seconds else None,
        measured_rate_is_sustained_proof=False,
        coverage_basis='provider_cursors_and_registered_search_shards',
        internal_trace_complete=False, wallet_history_complete=False,
        independent_consensus_verified=False)
