"""Bounded, durable admission for model-selected public-history research.

Admission is an execution-engine choice. It never masquerades as a Muse
selection; original model IDs and any later coverage substitutions stay apart.
"""
import hashlib
import json

from .store import canonical

ROTATION_KEY = 'investigation_menu_rotation_v1'
SOURCE_ORDER = ('gsfa', 'portal', 'raw_history', 'custody', 'crosschain')
GROUP_ORDER = ('interactive', 'original', 'recent', 'gsfa', 'raw_history', 'portal', 'custody', 'crosschain')


def compact_cursor(raw, max_chars=600):
    """Keep cursor evidence in SQLite, send a bounded UTF-8 projection to Muse."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 64:
        raise ValueError('MENU_CURSOR_BOUND')
    text = raw if isinstance(raw, str) else canonical(raw)
    encoded = text.encode()
    if len(encoded) <= max_chars:
        return text
    digest = hashlib.sha256(encoded).hexdigest()
    summary = dict(truncated=True, sha256=digest, original_bytes=len(encoded),
                   original_characters=len(text), head='')
    if len(canonical(summary).encode()) > max_chars:
        return digest
    low, high = 0, min(len(text), max_chars)
    while low < high:
        mid = (low+high+1)//2
        summary['head'] = text[:mid]
        if len(canonical(summary).encode()) <= max_chars:
            low = mid
        else:
            high = mid-1
    summary['head'] = text[:low]
    return canonical(summary)


def _positive(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError('MENU_' + name)


def _original_roots(graph, roots):
    supplied = graph.state('original_research_roots_v1')
    if isinstance(supplied, dict):
        supplied = supplied.get('mints', supplied.get('roots'))
    if isinstance(supplied, list):
        return {row['mint'] if isinstance(row, dict) else row for row in supplied
                if isinstance(row, str) or isinstance(row, dict) and isinstance(row.get('mint'), str)}
    return {row['mint'] for row in roots if not json.loads(row['body']).get('cohort_id')}


def _candidates(graph, root, excluded, ready, turn, per_root):
    placeholders = ','.join('?' for _ in excluded)
    where = '''f.root=? AND f.state='pending'
      AND (f.source!='core_forward' OR ?)
      AND (f.source NOT IN ('core_early','core_participation') OR ?)
      AND (f.source!='indexed_forward' OR ?)'''
    params = [root, *map(int, ready)]
    if excluded:
        where += ' AND f.id NOT IN ('+placeholders+')'
        params.extend(excluded)
    order = "CASE f.source WHEN 'portal_resolve_launch' THEN 0 WHEN 'recover_launch' THEN 1 WHEN 'gsfa_history' THEN 2 WHEN 'portal_verify' THEN 3 ELSE 4 END,f.depth,f.rowid"
    ordinary = [dict(row) for row in graph.db.execute(
        'SELECT f.* FROM frontier f WHERE '+where+' ORDER BY '+order+' LIMIT ?',
        [*params, per_root]).fetchall()]
    if not ordinary:
        return [], turn
    special = {}
    predicates = dict(gsfa="f.source='gsfa_history'", portal="f.source IN ('portal_verify','portal_forward')", raw_history="f.source='archive_transfer'",
                      custody='EXISTS(SELECT 1 FROM custody_jobs c WHERE c.job=f.id)',
                      crosschain="f.source='wormhole_forward'")
    for group, predicate in predicates.items():
        evidence_order = ("CASE f.source WHEN 'portal_verify' THEN 0 ELSE 1 END," if group == 'portal' else '')
        row = graph.db.execute('SELECT f.* FROM frontier f WHERE '+where+' AND '+predicate+
                               ' ORDER BY '+evidence_order+'f.depth,f.rowid LIMIT 1', params).fetchone()
        if row:
            special[group] = dict(row)
    selected = [ordinary[0]]
    selected_ids = {ordinary[0]['id']}
    # One priority slot and one rotating evidence-source slot. When per_root=1
    # there is intentionally no extra append that could violate the bound.
    for offset in range(len(SOURCE_ORDER)):
        if len(selected) >= per_root:
            break
        index = (turn+offset) % len(SOURCE_ORDER)
        candidate = special.get(SOURCE_ORDER[index])
        if candidate and candidate['id'] not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate['id'])
            turn = index+1
            break
    for candidate in ordinary:
        if len(selected) >= per_root:
            break
        if candidate['id'] not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate['id'])
    return selected, turn


def build_menu(graph, max_roots=10, per_root=2, excluded_ids=()):
    """Rotate root lanes, reserving at most one root for interactive requests.

    Exclusion is applied before SQL limits so concurrently reserved IDs do not
    hide the next pending job. Full cursor bodies are retained in returned jobs;
    callers must use compact_cursor when constructing the model request.
    """
    _positive(max_roots, 'ROOT_BOUND', 10)
    _positive(per_root, 'PER_ROOT_BOUND', 2)
    excluded = tuple(dict.fromkeys(excluded_ids))
    if len(excluded) > 2000 or any(not isinstance(value, str) for value in excluded):
        raise ValueError('MENU_EXCLUDED_IDS')
    core = graph.base/'evidence/core-trace-v1'
    ready = ((core/'arkm_funding.sqlite3').exists(),
             (core/'pump_event_default.sqlite3').exists(),
             (graph.out/'address-index.sqlite3').exists())
    roots = [dict(row) for row in graph.db.execute('SELECT mint,body FROM roots ORDER BY rowid')]
    interactive = set()
    for row in roots:
        body = json.loads(row['body'])
        if body.get('root_kind') == 'wallet' and body.get('cohort_kind') == 'internal_wallet_request':
            interactive.add(row['mint'])
    original = _original_roots(graph, roots) - interactive
    lanes = dict(original=[row['mint'] for row in roots if row['mint'] in original],
                 recent=[row['mint'] for row in roots if row['mint'] not in original and row['mint'] not in interactive],
                 interactive=[row['mint'] for row in roots if row['mint'] in interactive])
    rotation = graph.state(ROTATION_KEY, {}) or {}
    positions = dict(rotation.get('positions', {}))
    turns = dict(rotation.get('source_turns', {}))
    available = {}
    for lane, mints in lanes.items():
        start = int(positions.get(lane, 0)) % max(1, len(mints))
        available[lane] = []
        for step in range(len(mints)):
            index = (start+step) % len(mints)
            root = mints[index]
            jobs, turn = _candidates(graph, root, excluded, ready,
                                     int(turns.get(root, 0)), per_root)
            if jobs:
                available[lane].append((root, jobs, index+1, turn))
                if len(available[lane]) >= (1 if lane == 'interactive' else max_roots):
                    break
    lane = rotation.get('next_lane', 'original')
    if lane not in ('original', 'recent'):
        lane = 'original'
    jobs, admitted, cohort_groups = [], [], dict(original=[], recent=[], interactive=[])
    if available['interactive']:
        root, candidates, position, turn = available['interactive'][0]
        jobs.extend(candidates)
        admitted.append(root)
        cohort_groups['interactive'].extend(job['id'] for job in candidates)
        positions['interactive'] = position
        turns[root] = turn
    while len(admitted) < max_roots and (available['original'] or available['recent']):
        other = 'recent' if lane == 'original' else 'original'
        if not available[lane]:
            lane, other = other, lane
        root, candidates, position, turn = available[lane].pop(0)
        jobs.extend(candidates)
        admitted.append(root)
        cohort_groups[lane].extend(job['id'] for job in candidates)
        positions[lane] = position
        turns[root] = turn
        lane = other
    if jobs:
        graph.db.execute('SAVEPOINT investigation_menu_rotation')
        try:
            graph.set(ROTATION_KEY, dict(positions=positions, source_turns=turns, next_lane=lane,
                                        menus=int(rotation.get('menus', 0))+1))
            graph.db.execute('RELEASE SAVEPOINT investigation_menu_rotation')
        except Exception:
            graph.db.execute('ROLLBACK TO SAVEPOINT investigation_menu_rotation')
            graph.db.execute('RELEASE SAVEPOINT investigation_menu_rotation')
            raise
    groups = dict(cohort_groups)
    groups.update(gsfa=[j['id'] for j in jobs if j['source'] == 'gsfa_history'],raw_history=[j['id'] for j in jobs if j['source'] == 'archive_transfer'],
                  portal=[j['id'] for j in jobs if j['source'] in ('portal_verify','portal_forward')],
                  crosschain=[j['id'] for j in jobs if j['source'] == 'wormhole_forward'],
                  custody=[j['id'] for j in jobs if graph.db.execute(
                      'SELECT 1 FROM custody_jobs WHERE job=? LIMIT 1', (j['id'],)).fetchone()])
    return dict(jobs=jobs, admitted_roots=admitted, cohort_groups=cohort_groups, groups=groups,
                maximum_roots=max_roots, maximum_jobs_per_root=per_root,
                original_roots_admitted=sum(root in original for root in admitted),
                recent_roots_admitted=sum(root not in original and root not in interactive for root in admitted),
                interactive_roots_admitted=sum(root in interactive for root in admitted),
                excluded_count=len(excluded), model_selection=False)


def supplement_selection(original_ids, mapping, groups):
    """Preserve Muse's record and transparently substitute bounded coverage jobs."""
    if (not isinstance(original_ids, list) or not 1 <= len(original_ids) <= 8 or
            any(not isinstance(ident, str) or ident not in mapping for ident in original_ids) or
            len(set(original_ids)) != len(original_ids)):
        raise ValueError('MENU_MODEL_SELECTION')
    original = list(original_ids)
    normalized = {}
    for name, values in groups.items():
        if not isinstance(name, str) or not isinstance(values, (list, tuple)):
            raise ValueError('MENU_COVERAGE_GROUP')
        if any(not isinstance(ident, str) or ident not in mapping for ident in values):
            raise ValueError('MENU_COVERAGE_UNKNOWN_ID')
        if values:
            normalized[name] = list(dict.fromkeys(values))
    names = [name for name in GROUP_ORDER if name in normalized]
    names.extend(sorted(set(normalized)-set(names)))
    scheduled = list(original)
    for name in names:
        candidates = normalized[name]
        if set(candidates).intersection(scheduled):
            continue
        uncovered = [values for values in normalized.values() if not set(values).intersection(scheduled)]
        # Prefer a candidate covering several still-missing evidence groups.
        candidate = max(candidates, key=lambda ident: sum(ident in values for values in uncovered))
        if len(scheduled) < 8:
            scheduled.append(candidate)
            continue
        covered = [values for values in normalized.values() if set(values).intersection(scheduled)]
        removable = next((ident for ident in reversed(scheduled)
                          if all(set(values).intersection(set(scheduled)-{ident}) or candidate in values
                                 for values in covered)), None)
        if removable is not None:
            scheduled.remove(removable)
            scheduled.append(candidate)
    reserved = {}
    for name in names:
        selected = [ident for ident in scheduled if ident in normalized[name]]
        if selected and not any(ident in original for ident in selected):
            reserved[name] = selected[0]
    return dict(original_job_ids=original, scheduled_job_ids=scheduled,
                engine_reserved=reserved,
                dropped_original_job_ids=[ident for ident in original if ident not in scheduled],
                uncovered_groups=[name for name in names if not set(normalized[name]).intersection(scheduled)])
