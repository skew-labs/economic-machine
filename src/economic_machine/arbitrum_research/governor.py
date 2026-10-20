"""Native admission for Muse's resource proposals; this module mutates no host.

Measurements must come from the operator's sampler, not from model output. The
caller may apply the result only to its isolated Arbitrum research unit. CPU and
RAM are recomputed from spare host capacity; the model does not own global caps.
"""
import hashlib
import json
import math
import re
import time

GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_MEMORY = 32 * GIB
OPTIONAL_METRICS = {'cache_current_bytes', 'memory_current_bytes'}
METRIC_KEYS = {
    'observed_ms', 'logical_cpus', 'cpu_busy_pct', 'io_pressure_pct',
    'mem_available_bytes', 'mem_total_bytes', 'free_disk_bytes',
    'protected_free_bytes', 'arb_current_bytes', 'monthly_remaining_krw',
    'source_backoff_seconds', 'backlog_blocks',
}
PROPOSAL_KEYS = {
    'download_batch_blocks', 'download_concurrency', 'schedule_delay_seconds',
    'cache_target_bytes', 'io_priority', 'model_research_workers', 'reason', 'source_ids',
}


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('ARB_RESOURCE_' + field.upper())
    return value


def validate_metrics(value, now_ms=None):
    """Strict public-only sampler schema, documented by METRIC_KEYS above."""
    if type(value) is not dict or not METRIC_KEYS <= value.keys() or value.keys() - METRIC_KEYS - OPTIONAL_METRICS:
        raise ValueError('ARB_RESOURCE_METRIC_FIELDS')
    out = dict(value)
    for field in METRIC_KEYS - {'cpu_busy_pct', 'io_pressure_pct'}:
        _integer(out[field], 0, 2**63 - 1, field)
    for field in ('cpu_busy_pct', 'io_pressure_pct'):
        if type(out[field]) not in (int, float) or not math.isfinite(out[field]) or not 0 <= out[field] <= 100:
            raise ValueError('ARB_RESOURCE_' + field.upper())
    _integer(out['logical_cpus'], 1, 4096, 'logical_cpus')
    _integer(out.get('cache_current_bytes', 0), 0, 2**63 - 1, 'cache_current_bytes')
    out['cache_current_bytes'] = out.get('cache_current_bytes', 0)
    if 'memory_current_bytes' in out:
        _integer(out['memory_current_bytes'], 0, out['mem_total_bytes'], 'memory_current_bytes')
    if out['mem_available_bytes'] > out['mem_total_bytes']:
        raise ValueError('ARB_RESOURCE_MEMORY_MEASUREMENT')
    # The source retention floor cannot be weakened by a model or bad sampler.
    if out['protected_free_bytes'] < 128 * GIB:
        raise ValueError('ARB_RESOURCE_PROTECTED_FREE')
    now_ms = time.time_ns() // 1000000 if now_ms is None else now_ms
    if not 0 <= now_ms - out['observed_ms'] <= 120000:
        raise ValueError('ARB_RESOURCE_STALE_MEASUREMENT')
    return out


def metric_source_id(metrics):
    return 'metrics:' + hashlib.sha256(json.dumps(metrics, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_proposal(value):
    if type(value) is not dict or value.keys() != PROPOSAL_KEYS:
        raise ValueError('ARB_RESOURCE_PROPOSAL_FIELDS')
    ranges = {'download_batch_blocks': (1, 2048), 'download_concurrency': (1, 4),
              'schedule_delay_seconds': (0, 3600), 'cache_target_bytes': (0, 512 * GIB),
              'model_research_workers': (0, 30)}
    for field, (low, high) in ranges.items():
        _integer(value[field], low, high, field)
    if value['io_priority'] not in ('low', 'normal'):
        raise ValueError('ARB_RESOURCE_IO_PRIORITY')
    reason = value['reason']
    if type(reason) is not str or not 1 <= len(reason) <= 600 or re.search(r'https?://|file://|\$\(|`|\x00', reason):
        raise ValueError('ARB_RESOURCE_REASON')
    sources = value['source_ids']
    if type(sources) is not list or not 1 <= len(sources) <= 8 or any(type(s) is not str for s in sources) or len(set(sources)) != len(sources):
        raise ValueError('ARB_RESOURCE_SOURCE_IDS')
    if any(type(s) is not str or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', s) for s in sources):
        raise ValueError('ARB_RESOURCE_SOURCE_ID')
    return dict(value)


def govern(proposal, metrics, *, now_ms=None):
    """Clamp a validated model proposal against current capacity and budget.

    Returned limits grant admission, not spending or authority to delete data.
    A cache reduction never authorizes deleting retained evidence. The caller
    rechecks these measurements before applying properties and each chunk.
    """
    m = validate_metrics(metrics, now_ms)
    p = validate_proposal(proposal)
    if p['source_ids'] != [metric_source_id(m)]:
        raise ValueError('ARB_RESOURCE_SOURCE_MISMATCH')
    spare_disk = max(0, m['free_disk_bytes'] - m['protected_free_bytes'])
    reserve = max(8 * GIB, m['mem_total_bytes'] // 8)
    headroom = max(0, m['mem_available_bytes'] - reserve)
    # Use most spare capacity, not a fixed quarter. Retain the unit's current
    # allocation when pressure rises; stop admitting work instead of killing it.
    current = m.get('memory_current_bytes', 0)
    memory = min(MAX_MEMORY, max(current + 64 * MIB, current + headroom * 4 // 5))
    idle_cores = m['logical_cpus'] * (100 - m['cpu_busy_pct']) / 100
    # Half of measured idle CPU leaves capacity for production bursts.
    quota = max(5, min(800, int(idle_cores * 50)))
    cache = min(p['cache_target_bytes'], spare_disk // 5)
    reasons = []
    delay = max(1, p['schedule_delay_seconds'], m['source_backoff_seconds'])
    workers = p['model_research_workers']
    concurrency = min(p['download_concurrency'], max(1, quota // 100))
    blocks = p['download_batch_blocks']
    # Production uses weight 10 on this host. Research may match it while
    # healthy but must never gain greater relative disk priority.
    io_weight = 1 if p['io_priority'] == 'low' else 10
    state = 'ADMITTED'
    if cache < p['cache_target_bytes']:
        reasons.append('CACHE_CLAMPED_TO_SPARE_DISK')
    if m['io_pressure_pct'] >= 20:
        concurrency = 1
        blocks = min(blocks, 128)
        io_weight = 1
        delay = max(delay, 10)
        reasons.append('HOST_IO_PRESSURE')
    if m['cpu_busy_pct'] >= 85:
        workers = min(workers, 4)
        delay = max(delay, 10)
        reasons.append('HOST_CPU_PRESSURE')
    if m['monthly_remaining_krw'] < 10000:
        workers = min(workers, 1)
        concurrency = 1
        reasons.append('MONTHLY_BUDGET_LOW')
    if m['source_backoff_seconds']:
        reasons.append('SOURCE_BACKOFF')
        state = 'BACKOFF'
    if (m['monthly_remaining_krw'] <= 0 or spare_disk < 256 * MIB or headroom < 256 * MIB or cache < 256 * MIB
            or m['io_pressure_pct'] >= 60 or m['cpu_busy_pct'] >= 98):
        state = 'PAUSED_CAPACITY'
        concurrency = workers = 0
        delay = max(delay, 60)
        if m['monthly_remaining_krw'] <= 0: reasons.append('MONTHLY_BUDGET_EXHAUSTED')
        if spare_disk < 256 * MIB: reasons.append('PROTECTED_DISK_FLOOR')
        if headroom < 256 * MIB: reasons.append('LOW_AVAILABLE_RAM')
        if cache < 256 * MIB: reasons.append('INSUFFICIENT_WORKING_CACHE')
        if m['io_pressure_pct'] >= 60: reasons.append('HOST_IO_CRITICAL')
        if m['cpu_busy_pct'] >= 98: reasons.append('HOST_CPU_CRITICAL')
    if m['backlog_blocks'] == 0:
        concurrency = workers = 0
        state = 'IDLE_CAUGHT_UP'
        delay = max(delay, 15)
        reasons.append('NO_PENDING_BLOCKS')
    return dict(schema='economic-machine.arbitrum-resource-admission.v1', state=state,
                observed_ms=m['observed_ms'], source_ids=p['source_ids'],
                cpu_quota_percent=quota, memory_max_bytes=max(64 * MIB, memory),
                host_memory_reserve_bytes=reserve, memory_mode='aggressive_spare_capacity',
                cache_limit_bytes=cache,
                cache_growth_allowed_bytes=max(0, cache - m['cache_current_bytes']),
                download_batch_blocks=blocks, download_concurrency=concurrency,
                schedule_delay_seconds=delay, model_research_workers=workers,
                io_weight=io_weight, reasons=reasons,
                scope='isolated_arbitrum_research_only',
                production_changes=False, evidence_deletion=False, budget_increase=False)


def conservative_proposal(metrics):
    """Deterministic fallback when no accepted manager response is available."""
    m = validate_metrics(metrics)
    return dict(download_batch_blocks=128, download_concurrency=1,
                schedule_delay_seconds=5, cache_target_bytes=min(512 * GIB, max(0, (m['free_disk_bytes'] - m['protected_free_bytes']) // 5)),
                io_priority='low', model_research_workers=30,
                reason='Deterministic admission until a valid manager proposal is available.',
                source_ids=[metric_source_id(m)])
