"""Metered provider timestamp hints for new research cohorts, never launch proof."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from .archive import NoRedirect
from .cohort import ORIGINAL_DEADLINE_MS
from .portal_history import HOST, SOURCE_BYTES, PRIOR_PROBE_BYTES, portal_network
from .store import address, atomic, canonical

CAP = 4 * 1024 * 1024
RESPONSE_LIMIT = 4096
SCHEMA = 'economic-machine.portal-launch-ledger.v1'
ANCHOR_SCHEMA = 'economic-machine.portal-launch-anchor.v1'
_SHA = re.compile(r'^[0-9a-f]{64}$')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, maximum):
    if path.is_symlink() or path.stat().st_size > maximum:
        raise ValueError('LAUNCH_CACHE_BOUND_OR_LINK')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        raw = handle.read(maximum+1)
    if len(raw) > maximum:
        raise ValueError('LAUNCH_CACHE_BOUND_OR_LINK')
    return raw


class LaunchResolver:
    def __init__(self, graph):
        self.g = graph
        self.path = graph.out/'portal-launch-ledger.json'
        self.folder = graph.out/'portal-launch-responses'
        self.folder.mkdir(exist_ok=True)

    def _guard(self, scope):
        scope.guard()
        if getattr(scope,'continuation',None):return
        if (scope.grant.get('deadline_ms') != ORIGINAL_DEADLINE_MS or
                time.time_ns()//1000000 >= ORIGINAL_DEADLINE_MS):
            raise ValueError('LAUNCH_ORIGINAL_DEADLINE')

    def _root(self, mint):
        row = self.g.db.execute('SELECT body FROM roots WHERE mint=?', (mint,)).fetchone()
        value = json.loads(row[0]) if row else {}
        return value if (address(mint) and value.get('mint') == mint and
                         value.get('cohort_kind') and value.get('root_kind') != 'wallet' and
                         value.get('cohort_kind') != 'internal_wallet_request' and
                         type(value.get('created_ms')) is int and
                         0 <= value['created_ms'] < ORIGINAL_DEADLINE_MS) else None

    def _head(self):
        head = self.g.state('portal_finalized_head')
        if (not isinstance(head, dict) or type(head.get('number')) is not int or
                not 0 <= head['number'] < 1_000_000_000 or
                not isinstance(head.get('hash'), str) or not head['hash']):
            raise ValueError('LAUNCH_PROVIDER_HEAD_REQUIRED')
        return head

    def _ledger(self):
        value = json.loads(_read(self.path, 1024*1024)) if self.path.exists() else dict(schema=SCHEMA, requests=[])
        if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
                not isinstance(value.get('requests'), list) or len(value['requests']) > 4096):
            raise ValueError('LAUNCH_LEDGER_SCHEMA')
        for row in value['requests']:
            if not isinstance(row, dict) or type(row.get('reserved_bytes')) is not int or not 0 < row['reserved_bytes'] <= CAP:
                raise ValueError('LAUNCH_LEDGER_RESERVATION')
        if sum(row['reserved_bytes'] for row in value['requests']) > CAP:
            raise ValueError('LAUNCH_LEDGER_CAP')
        return value

    def _raw_receipt(self, folder, row, maximum):
        name = row.get('raw_file')
        if (not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..') or
                not isinstance(row.get('sha256'), str) or not _SHA.fullmatch(row['sha256'])):
            raise ValueError('LAUNCH_METADATA_RECEIPT')
        raw = _read(folder/name, maximum)
        if _sha(raw) != row['sha256']:
            raise ValueError('LAUNCH_METADATA_HASH')
        return raw

    def _anchor(self, root, raw, receipt):
        body = json.loads(raw)
        slot = body.get('block_number') if isinstance(body, dict) else None
        head = self._head()
        if type(slot) is not int or not 0 <= slot < 1_000_000_000 or slot > head['number']:
            raise ValueError('LAUNCH_PROVIDER_SLOT')
        return dict(schema=ANCHOR_SCHEMA, mint=root['mint'], slot=slot,
                    created_ms=root['created_ms'], start=max(0, slot-8), end=min(head['number'], slot+16),
                    source='portal_timestamp_provider_claim', source_sha256=_sha(raw),
                    response=body, receipt=receipt, provider_head=head,
                    canonical_verified=False, launch_verified=False, actual_car_required=True)

    def _cached(self, root):
        out = self.g.base/'evidence/recent50-v1'
        cache = out/'launch-anchors.json'
        ledger = out/'portal-metadata-ledger.json'
        if not cache.exists() or not ledger.exists():
            return None
        cache_raw = _read(cache, 2*1024*1024)
        entries = json.loads(cache_raw)
        value = entries.get(root['mint']) if isinstance(entries, dict) else None
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get('created_ms') != root['created_ms'] or
                value.get('source') != 'portal_timestamp_provider_claim' or
                value.get('canonical_verified') is not False):
            raise ValueError('LAUNCH_CACHED_ANCHOR_IDENTITY')
        ledger_raw = _read(ledger, 1024*1024)
        data = json.loads(ledger_raw)
        if (not isinstance(data, dict) or data.get('schema') != 'economic-machine.portal-metadata-ledger.v1' or
                not isinstance(data.get('requests'), list) or
                any(not isinstance(row, dict) or type(row.get('reserved_bytes')) is not int or
                    not 0 < row['reserved_bytes'] <= CAP for row in data['requests']) or
                sum(row['reserved_bytes'] for row in data['requests']) > CAP):
            raise ValueError('LAUNCH_METADATA_LEDGER_SCHEMA')
        endpoint = '/timestamps/'+str(root['created_ms']//1000)+'/block'
        matching = [row for row in data['requests'] if row.get('endpoint') == endpoint and
                    row.get('request_body') is None and row.get('status') == 200]
        if not matching:
            return None
        accepted = None
        for row in matching:
            raw = self._raw_receipt(out, row, 65536)
            anchor = self._anchor(root, raw, dict(kind='retained_metadata_response',
                raw_file='evidence/recent50-v1/'+row['raw_file'], sha256=_sha(raw),
                metadata_ledger_sha256=_sha(ledger_raw), cached_anchors_sha256=_sha(cache_raw),
                endpoint=endpoint, request_id=row.get('id'), cached_anchor_claim=value))
            if anchor['slot'] != value.get('slot') or accepted and anchor['slot'] != accepted['slot']:
                raise ValueError('LAUNCH_CACHED_ANCHOR_CONFLICT')
            accepted = anchor
        return accepted

    def _install(self, root, anchor):
        head = self._head()
        if (not isinstance(anchor, dict) or anchor.get('schema') != ANCHOR_SCHEMA or
                anchor.get('mint') != root['mint'] or anchor.get('created_ms') != root['created_ms'] or
                type(anchor.get('slot')) is not int or not 0 <= anchor['slot'] <= head['number'] or
                anchor.get('start') != max(0, anchor['slot']-8) or
                type(anchor.get('end')) is not int or not anchor['slot'] <= anchor['end'] <= min(head['number'], anchor['slot']+16) or
                not isinstance(anchor.get('source_sha256'), str) or not _SHA.fullmatch(anchor['source_sha256']) or
                anchor.get('canonical_verified') is not False or anchor.get('launch_verified') is not False):
            raise ValueError('LAUNCH_ANCHOR_SCHEMA')
        key = 'launch_anchor:'+root['mint']
        existing = self.g.state(key)
        with self.g.db:
            if existing is not None:
                if any(existing.get(field) != anchor[field] for field in ('mint', 'slot', 'created_ms', 'source_sha256')):
                    raise ValueError('LAUNCH_ANCHOR_IMMUTABLE_CONFLICT')
            else:
                self.g.set(key, anchor)
            # Missing creator is a token lookup identity, not a creator claim.
            if not self.g.db.execute("SELECT 1 FROM frontier WHERE root=? AND source='recover_launch' LIMIT 1", (root['mint'],)).fetchone():
                lookup = root['creator'] if address(root.get('creator')) else root['mint']
                self.g.enqueue(root['mint'], lookup, None, root['created_ms'], 0, 'recover_launch')
                if not address(root.get('creator')):
                    self.g.set('launch_recovery_lookup:'+root['mint'], dict(wallet_is_mint_lookup=True, creator_known=False))
            self.g.db.execute("UPDATE frontier SET state='pending' WHERE root=? AND source='recover_launch' AND state='waiting_for_launch_anchor'", (root['mint'],))
            self.g.db.execute("UPDATE frontier SET state='launch_anchor_ready' WHERE root=? AND source='portal_resolve_launch' AND state IN ('pending','source_blocked')", (root['mint'],))

    def seed(self, scope):
        self._guard(scope)
        result = dict(queued=0, cached=0, existing=0, original_roots_untouched=True)
        for row in self.g.db.execute('SELECT mint FROM roots ORDER BY rowid').fetchall():
            self._guard(scope)
            root = self._root(row[0])
            if root is None:
                continue
            existing = self.g.state('launch_anchor:'+root['mint'])
            if existing:
                self._install(root, existing)
                result['existing'] += 1
                continue
            cached = self._cached(root)
            if cached:
                self._install(root, cached)
                result['cached'] += 1
                continue
            wallet = root['creator'] if address(root.get('creator')) else root['mint']
            with self.g.db:
                self.g.db.execute("UPDATE frontier SET state='waiting_for_launch_anchor' WHERE root=? AND source='recover_launch' AND state='pending'", (root['mint'],))
                before = self.g.db.total_changes
                self.g.enqueue(root['mint'], wallet, None, root['created_ms'], 0, 'portal_resolve_launch')
                result['queued'] += int(self.g.db.total_changes > before)
        return result

    def _fetch(self, root, scope):
        endpoint = '/timestamps/'+str(root['created_ms']//1000)+'/block'
        with portal_network(self.g.base, scope, wait=False):
            self._guard(scope)
            ledger = self._ledger()
            for row in ledger['requests']:
                if row.get('endpoint') == endpoint and row.get('status') == 200 and row.get('validated'):
                    raw = self._raw_receipt(self.folder, row, RESPONSE_LIMIT)
                    return self._anchor(root, raw, dict(row, cache_reused=True))
            reserved = sum(row['reserved_bytes'] for row in ledger['requests'])
            if reserved+RESPONSE_LIMIT+1 > CAP:
                raise ValueError('LAUNCH_LEDGER_CAP')
            portal_used = self.g.db.execute('SELECT coalesce(sum(bytes),0) FROM portal_source_io').fetchone()[0]
            if portal_used+PRIOR_PROBE_BYTES+CAP+reserved+RESPONSE_LIMIT+1 > SOURCE_BYTES:
                raise ValueError('PORTAL_EVALUATION_IO_LIMIT')
            row = dict(id=len(ledger['requests'])+1, endpoint=endpoint, reserved_bytes=RESPONSE_LIMIT+1,
                       at_ms=time.time_ns()//1000000, status='reserved', validated=False)
            ledger['requests'].append(row)
            atomic(self.path, ledger)
            try:
                scope.reserve(RESPONSE_LIMIT+1, 'portal-launch:'+endpoint)
                request = urllib.request.Request(HOST+endpoint, headers={
                    'Accept': 'application/json', 'Accept-Encoding': 'identity',
                    'User-Agent': 'EconomicMachineResearch/0.3-evaluation'})
                with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as reply:
                    raw = reply.read(RESPONSE_LIMIT+1)
                    row['status'] = reply.status
                digest = _sha(raw)
                name = str(row['id'])+'-'+digest+'.json'
                with (self.folder/name).open('xb') as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                row.update(sha256=digest, raw_file=name, response_bytes=len(raw))
                atomic(self.path, ledger)
                if len(raw) > RESPONSE_LIMIT or row['status'] != 200:
                    raise ValueError('LAUNCH_RESPONSE_BOUND_OR_STATUS')
                anchor = self._anchor(root, raw, dict(row))
                row['validated'] = True
                atomic(self.path, ledger)
                anchor['receipt'] = dict(row)
                return anchor
            except Exception as error:
                row.update(error=type(error).__name__, failed_reservation_retained=True)
                if isinstance(error, urllib.error.HTTPError):
                    row.update(status=error.code, retry_after=error.headers.get('Retry-After') if error.headers else None)
                atomic(self.path, ledger)
                raise

    def run(self, job, scope):
        self._guard(scope)
        row = self.g.db.execute('SELECT * FROM frontier WHERE id=?', (job['id'],)).fetchone()
        if row is None or row['source'] != 'portal_resolve_launch':
            raise ValueError('LAUNCH_RESOLVER_JOB')
        root = self._root(row['root'])
        if root is None or row['stamp'] != root['created_ms']:
            raise ValueError('LAUNCH_RESOLVER_ROOT_SCOPE')
        if row['state'] not in ('pending', 'source_blocked'):
            return dict(job_id=row['id'], state=row['state'], no_request=True)
        existing = self.g.state('launch_anchor:'+root['mint'])
        anchor = existing or self._cached(root) or self._fetch(root, scope)
        self._install(root, anchor)
        return dict(job_id=row['id'], state='launch_anchor_ready', slot=anchor['slot'],
                    source_sha256=anchor['source_sha256'], canonical_verified=False,
                    launch_verified=False, actual_car_required=True)
