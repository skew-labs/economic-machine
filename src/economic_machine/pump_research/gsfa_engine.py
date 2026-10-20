"""Small Graph scheduler adapter for verified, locally imported GSFA epochs.

Receipt readiness gates every seed/run. Existing grants, original frontier
anchors, custody annotations and raw-index cursors remain authoritative.
"""
import hashlib
import json
from pathlib import Path
import re
import stat
import time

from .address_index import AddressIndex
from .gsfa_history import GsfaHistory
from .gsfa_index import Budget, FILES, GsfaIndex, MIB
from .precision import order_bound
from .raw_history import ScopedArchive
from .store import address, canonical


def _read_json(path, maximum):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('GSFA_ENGINE_RECEIPT_FILE')
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError('GSFA_ENGINE_RECEIPT_BOUND')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _local_path(base, value):
    """Registry references are local evidence paths, never network locations."""
    if (not isinstance(value, str) or not value or len(value) > 512 or
            '\\' in value or '\x00' in value):
        raise ValueError('GSFA_ENGINE_REGISTRY_PATH')
    parts = value.split('/')
    if parts[0] != 'evidence' or any(part in ('', '.', '..') for part in parts):
        raise ValueError('GSFA_ENGINE_REGISTRY_PATH')
    path = Path(base)
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('GSFA_ENGINE_REGISTRY_LINK')
    if not path.resolve().is_relative_to(Path(base).resolve()):
        raise ValueError('GSFA_ENGINE_REGISTRY_PATH')
    return path


class GsfaEngine:
    def __init__(self, graph):
        self.g = graph
        self.folder = graph.base / 'evidence/address-epoch-v1'
        self.registry = graph.base / 'evidence/address-epochs-v2/registry.json'
        graph.db.executescript('''
          CREATE TABLE IF NOT EXISTS gsfa_engine_jobs(job TEXT PRIMARY KEY,parent TEXT NOT NULL,epoch INTEGER NOT NULL,
            receipt_sha256 TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(parent,receipt_sha256));
          CREATE TABLE IF NOT EXISTS gsfa_engine_results(job TEXT,result INTEGER,body TEXT,
            PRIMARY KEY(job,result)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS gsfa_engine_imports(epoch INTEGER PRIMARY KEY,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS gsfa_engine_registry_receipts(sha256 TEXT PRIMARY KEY,body TEXT NOT NULL);
        ''')

    def _entries(self, pin=False):
        entries = {685: dict(epoch=685, legacy=True)}
        if not self.registry.exists():
            if self.g.db.execute('SELECT 1 FROM gsfa_engine_imports LIMIT 1').fetchone():
                raise ValueError('GSFA_ENGINE_REGISTRY_REMOVED')
            return entries
        _local_path(self.g.base, self.registry.relative_to(self.g.base).as_posix())
        registry, digest = _read_json(self.registry, 2 * MIB)
        if (not isinstance(registry, dict) or registry.get('schema') != 'economic-machine.gsfa-import-registry.v1' or
                not isinstance(registry.get('entries'), list) or len(registry['entries']) > 2048):
            raise ValueError('GSFA_ENGINE_REGISTRY_SHAPE')
        for entry in registry['entries']:
            if (not isinstance(entry, dict) or set(entry) !=
                    {'epoch', 'receipt_path', 'receipt_sha256', 'grant_path', 'grant_sha256'} or
                    type(entry['epoch']) is not int or not 0 <= entry['epoch'] <= 100000 or
                    not _hash(entry['receipt_sha256']) or not _hash(entry['grant_sha256'])):
                raise ValueError('GSFA_ENGINE_REGISTRY_ENTRY')
            epoch = entry['epoch']
            if epoch in entries:
                raise ValueError('GSFA_ENGINE_REGISTRY_DUPLICATE_OR_LEGACY_EPOCH')
            for key in ('receipt_path', 'grant_path'):
                _local_path(self.g.base, entry[key])
            entries[epoch] = dict(entry, legacy=False)
        for row in self.g.db.execute('SELECT epoch,body FROM gsfa_engine_imports'):
            if entries.get(row['epoch']) != json.loads(row['body']):
                raise ValueError('GSFA_ENGINE_REGISTERED_IMPORT_CHANGED')
        if pin:
            with self.g.db:
                self.g.db.execute('INSERT OR IGNORE INTO gsfa_engine_registry_receipts VALUES(?,?)',
                                  (digest, canonical(registry)))
        return entries

    def _receipt(self, pin=False, epoch=685, entry=None):
        if entry is None:
            entry = self._entries(pin=pin).get(epoch)
        if entry is None:
            raise ValueError('GSFA_ENGINE_EPOCH_NOT_REGISTERED')
        epoch = entry['epoch']
        legacy = entry['legacy']
        path = self.folder / 'import-receipt.json' if legacy else _local_path(self.g.base, entry['receipt_path'])
        folder = path.parent
        if not path.exists():
            if legacy:
                return None
            raise ValueError('GSFA_ENGINE_REGISTERED_RECEIPT_MISSING')
        receipt, digest = _read_json(path, 2 * MIB)
        if not legacy:
            _, grant_digest = _read_json(_local_path(self.g.base, entry['grant_path']), 256 * 1024)
            if digest != entry['receipt_sha256'] or grant_digest != entry['grant_sha256']:
                raise ValueError('GSFA_ENGINE_REGISTERED_SOURCE_CHANGED')
            # This pins provenance only. The active Scope remains responsible
            # for validating authority, deadline, IO and aggregate resources.
        if (not isinstance(receipt, dict) or receipt.get('schema') != 'economic-machine.gsfa-import.v1' or
                receipt.get('epoch') != epoch or receipt.get('index_query_verified') is not True or
                not _hash(receipt.get('index_identity')) or not _hash(receipt.get('source_sha256')) or
                receipt.get('directory') != 'gsfa-index-'+str(epoch)):
            raise ValueError('GSFA_ENGINE_IMPORT_NOT_READY')
        directory = folder / receipt['directory']
        if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != folder.resolve():
            raise ValueError('GSFA_ENGINE_DIRECTORY')
        manifest, _ = _read_json(folder / 'download-manifest.json', 8 * MIB)
        extraction = receipt.get('extraction', {})
        if (not isinstance(manifest, dict) or not isinstance(extraction, dict) or
                manifest.get('sha256') != receipt['source_sha256'] or
                extraction.get('compressed_sha256') != receipt['source_sha256'] or
                extraction.get('format_inspected') is not True or extraction.get('extracted') is not True or
                extraction.get('downloaded_chunk_hashes_verified') is not True):
            raise ValueError('GSFA_ENGINE_IMPORT_SOURCE_MISMATCH')
        files = extraction.get('files')
        if (not isinstance(files, list) or len(files) != 3 or
                any(not isinstance(r, dict) for r in files) or {r.get('name') for r in files} != FILES):
            raise ValueError('GSFA_ENGINE_IMPORT_MEMBERS')
        member_state = {}
        cloud_reference = None
        for row in files:
            if type(row.get('bytes')) is not int or row['bytes'] < 0 or not _hash(row.get('sha256')):
                raise ValueError('GSFA_ENGINE_MEMBER_IDENTITY')
            path = directory / row['name']
            if path.is_symlink():
                raise ValueError('GSFA_ENGINE_MEMBER_LINK')
            try:info=path.stat()
            except FileNotFoundError:
                if row['name']!='linked-log':raise
                from .research_cloud import load_reference
                cloud_reference=directory/'linked-log-cloud.json'
                cloud=load_reference(cloud_reference,row)
                # Preserve the originally pinned local identity across a
                # separately verified storage migration; never reset its pin.
                member_state[row['name']]=dict(cloud['original_local_identity'],sha256=row['sha256'])
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size != row['bytes']:
                raise ValueError('GSFA_ENGINE_MEMBER_SIZE')
            # Full content hashes were verified by the importer. Pin its receipt
            # plus immutable local file identity; do not reread 30 GB per job.
            member_state[row['name']] = dict(bytes=info.st_size, sha256=row['sha256'],
                device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
        if extraction.get('member_bytes') != sum(r['bytes'] for r in files):
            raise ValueError('GSFA_ENGINE_MEMBER_TOTAL')
        identity = dict(receipt_sha256=digest, source_sha256=receipt['source_sha256'],
                        index_identity=receipt['index_identity'], root_cid=receipt.get('root_cid'),
                        directory=receipt['directory'], members=member_state)
        key = 'gsfa_import_pin:'+str(epoch)
        prior = self.g.state(key)
        if prior is not None and prior != identity:
            raise ValueError('GSFA_ENGINE_PINNED_IMPORT_CHANGED')
        if pin:
            with self.g.db:
                if prior is None:
                    self.g.set(key, identity)
                if not legacy:
                    # Only a validated receipt becomes an immutable registry
                    # entry. Appending another epoch cannot replace this one.
                    self.g.db.execute('INSERT OR IGNORE INTO gsfa_engine_imports VALUES(?,?)',
                                      (epoch, canonical(entry)))
        return dict(receipt=receipt, sha256=digest, directory=directory,cloud_reference=cloud_reference)

    def _open_index(self, ready, scope):
        continuation=getattr(scope,'continuation',None)
        if continuation:continuation.assert_epoch(ready['receipt']['epoch'])
        def cold_reader():
            from .research_cloud import CloudFile
            expected=next(r for r in ready['receipt']['extraction']['files'] if r['name']=='linked-log')
            return CloudFile(self.g.base,ready['directory']/'linked-log-cloud.json',expected)
        args=(ready['directory'],ready['receipt']['epoch'],Budget(getattr(scope,'effective_deadline_ms',scope.grant['deadline_ms']),64*MIB,scope.guard))
        if ready.get('cloud_reference') is not None:index=GsfaIndex(*args,linked_reader=cold_reader())
        else:
            try:index=GsfaIndex(*args)
            except FileNotFoundError:
                # Retirement can occur after receipt validation. Retry only
                # the specifically migrated linked log; other missing files fail.
                if (ready['directory']/'linked-log').exists() or not (ready['directory']/'linked-log-cloud.json').exists():raise
                index=GsfaIndex(*args,linked_reader=cold_reader())
        if index.identity != ready['receipt']['index_identity'] or index.root_cid != ready['receipt']['root_cid']:
            index.close()
            raise ValueError('GSFA_ENGINE_INDEX_IDENTITY')
        return index

    def _bind(self, parent, ready):
        epoch = ready['receipt']['epoch']
        if (not address(parent['wallet']) or type(parent['slot']) is not int or
                not 0 <= parent['slot'] <= (epoch + 1) * 432000 - 1):
            return None
        root = self.g.db.execute('SELECT body FROM roots WHERE mint=?', (parent['root'],)).fetchone()
        wallet_root = bool(root and json.loads(root[0]).get('root_kind') == 'wallet')
        if parent['wallet'] == parent['root'] and not wallet_root:
            return None  # Token mints are not default wallet-history roots.
        anchor = self.g.anchor(parent)
        if anchor and anchor['slot'] != parent['slot']:
            raise ValueError('GSFA_ENGINE_PARENT_ANCHOR')
        job = hashlib.sha256(canonical(['gsfa_history', parent['id'], ready['sha256']]).encode()).hexdigest()[:24]
        original = {k: parent[k] for k in ('id', 'root', 'wallet', 'slot', 'stamp', 'depth', 'source')}
        body = dict(parent=original, anchor=anchor, epoch=epoch,
                    first_slot=max(epoch * 432000, parent['slot']), last_slot=(epoch + 1) * 432000 - 1,
                    import_receipt_sha256=ready['sha256'], index_identity=ready['receipt']['index_identity'],
                    whole_wallet_history=False, same_funds_continuity_proven=False)
        self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                          (job, parent['root'], parent['wallet'], parent['slot'], parent['stamp'], parent['depth'],
                           'pending', canonical(dict(after_result_id=0)), 'gsfa_history'))
        self.g.db.execute('INSERT OR IGNORE INTO gsfa_engine_jobs VALUES(?,?,?,?,?)',
                          (job, parent['id'], epoch, ready['sha256'], canonical(body)))
        self.g.custody.inherit(parent['id'], job)
        return job

    def seed(self, scope):
        scope.guard()
        entries = self._entries(pin=True)
        epochs = sorted(entries)
        last = self.g.state('gsfa_engine_seed_after_epoch')
        last = last if type(last) is int else -1
        order = [epoch for epoch in epochs if epoch > last] + [epoch for epoch in epochs if epoch <= last]
        ready = None
        for epoch in order:
            ready = self._receipt(pin=True, entry=entries[epoch])
            if ready is not None:
                break
        if ready is None:
            return dict(seeded=0, waiting_for='verified_gsfa_import_receipt', source_complete=False)
        epoch = ready['receipt']['epoch']
        with self._open_index(ready, scope):
            pass
        # Original launch/early-buyer jobs, later recipients and admitted wallet
        # requests all have stable local/indexed parent frontier records.
        rows = self.g.db.execute('''SELECT f.* FROM frontier f WHERE f.source IN ('local_forward','indexed_forward')
          AND f.slot IS NOT NULL AND f.slot BETWEEN 0 AND ?
          AND f.state NOT IN ('invalid_address','anchor_required')
          AND NOT EXISTS(SELECT 1 FROM gsfa_engine_jobs j WHERE j.parent=f.id AND j.receipt_sha256=?)
          AND (f.wallet!=f.root OR EXISTS(SELECT 1 FROM roots r WHERE r.mint=f.root AND json_extract(r.body,'$.root_kind')='wallet'))
          ORDER BY f.depth,f.rowid LIMIT 64''', ((epoch + 1) * 432000 - 1, ready['sha256'])).fetchall()
        count = 0
        with self.g.db:
            for row in rows:
                scope.guard()
                if self._bind(row, ready):
                    count += 1
            self.g.set('gsfa_engine_ready', dict(epoch=epoch, receipt_sha256=ready['sha256'],
                       index_identity=ready['receipt']['index_identity'], at_ms=time.time_ns() // 1000000))
            self.g.set('gsfa_engine_seed_after_epoch', epoch)
        return dict(seeded=count, epoch=epoch, registered_epochs=len(entries),
                    per_call_job_limit=64, epoch_rotation=True,
                    index_query_verified=True, address_membership_verified=False,
                    whole_wallet_history=False, independent_consensus_verified=False)

    def _parent(self, job, body):
        parent = self.g.db.execute('SELECT * FROM frontier WHERE id=?', (body['parent']['id'],)).fetchone()
        if parent is None or any(parent[k] != v for k, v in body['parent'].items()):
            raise ValueError('GSFA_ENGINE_ORIGINAL_PARENT_CHANGED')
        if any(job[k] != parent[k] for k in ('root', 'wallet', 'slot', 'stamp', 'depth')):
            raise ValueError('GSFA_ENGINE_JOB_ANCHOR_CHANGED')
        if body['anchor'] is not None and self.g.anchor(parent) != body['anchor']:
            raise ValueError('GSFA_ENGINE_ORIGINAL_ORDER_CHANGED')
        return parent

    def _apply(self, job, body, ready, rows):
        parent = self._parent(job, body)
        anchor = body['anchor']
        floor, after_ms, _ = order_bound(anchor, parent['slot'], parent['stamp'])
        with self.g.db:
            for row in rows:
                result = row['result']
                self.g.db.execute('INSERT OR IGNORE INTO gsfa_engine_results VALUES(?,?,?)',
                                  (job['id'], row['id'], canonical(result)))
                if result['state'] != 'verified_success':
                    continue
                child = self.g.enqueue(job['root'], job['wallet'], parent['slot'], parent['stamp'], parent['depth'],
                    'indexed_forward', after_order=(anchor['tx_order'], anchor['ix']) if anchor else None)
                self.g.custody.inherit(job['id'], child)
                self.g.db.execute("UPDATE frontier SET state='pending' WHERE id=? AND state='indexed_source_exhausted'", (child,))
                # Seed newly observed outgoing destinations in this available
                # epoch without substituting a mutable cursor for their anchor.
                for transfer in result['outgoing']:
                    if (result['slot'], result['tx_order'], transfer['ix']) <= floor or int(transfer['amount']) <= 0:
                        continue
                    stamp = result['block_time'] * 1000 if result.get('block_time') is not None else None
                    if after_ms is not None and (stamp is None or stamp <= after_ms):
                        continue
                    recipient = transfer['destination']
                    if not address(recipient) or recipient == job['wallet']:
                        continue
                    next_parent = self.g.enqueue(job['root'], recipient, result['slot'], stamp,
                        parent['depth'] + 1, 'local_forward', after_order=(result['tx_order'], transfer['ix']))
                    self.g.custody.inherit(job['id'], next_parent)
                    self._bind(self.g.db.execute('SELECT * FROM frontier WHERE id=?', (next_parent,)).fetchone(), ready)

    def run(self, job, scope):
        scope.guard()
        binding = self.g.db.execute('SELECT * FROM gsfa_engine_jobs WHERE job=?', (job['id'],)).fetchone()
        if binding is None:
            raise ValueError('GSFA_ENGINE_BINDING_IDENTITY')
        ready = self._receipt(pin=True, epoch=binding['epoch'])
        if ready is None:
            return dict(job_id=job['id'], waiting_for='verified_gsfa_import_receipt', source_complete=False)
        if binding['receipt_sha256'] != ready['sha256']:
            raise ValueError('GSFA_ENGINE_BINDING_IDENTITY')
        body = json.loads(binding['body'])
        self._parent(job, body)
        from .cli import proto
        _, meta_type = proto('/srv/economic-machine/collector')
        address_index = AddressIndex(self.g.out / 'address-index.sqlite3')
        history = None
        try:
            with self._open_index(ready, scope) as index:
                reader = ScopedArchive(self.g, scope, None, meta_type, address_index, wait=False)
                history = GsfaHistory(self.g.out / 'gsfa-history.sqlite3', index, reader, scope.guard)
                hjob = history.register(job['wallet'], dict(parent=body['parent'], order=body['anchor']),
                                       first_slot=body['first_slot'], last_slot=body['last_slot'])
                status = history.step(hjob, page_size=16, max_candidates=1)
                cursor = json.loads(job['cursor']) or dict(after_result_id=0)
                results = history.results(hjob, after_id=cursor['after_result_id'], limit=100)
                self._apply(job, body, ready, results['results'])
                gaps = any(n for state, n in status['states'].items() if state.startswith(('unresolved_', 'rejected_')))
                state = ('gsfa_drained_with_gaps' if gaps else 'gsfa_index_drained') if status['state'] == 'index_chain_drained' else 'pending'
                if len(results['results']) == 100:
                    state = 'pending'
                with self.g.db:
                    self.g.db.execute('UPDATE frontier SET state=?,cursor=? WHERE id=?',
                                      (state, canonical(dict(after_result_id=results['next_after_id'])), job['id']))
                return dict(job_id=job['id'], state=state, history=status, raw_results_applied=len(results['results']),
                            complete_address_history=False, independent_consensus_verified=False)
        finally:
            if history is not None:
                history.close()
            address_index.close()

    def snapshot(self):
        try:
            entries = self._entries()
            # The detailed receipt checks occur at seed/run. Snapshot bounds its
            # file work and reports registered versus previously validated epochs
            # separately; registry presence is not a completed history claim.
            selected = self.g.state('gsfa_engine_ready')
            epoch = selected.get('epoch', 685) if isinstance(selected, dict) else 685
            ready = self._receipt(epoch=epoch)
            readiness = 'import_ready' if ready else 'waiting_for_verified_import'
            error = None
        except (ValueError, OSError, KeyError, TypeError) as exc:
            entries, ready, readiness, error = {}, None, 'import_blocked', type(exc).__name__
        return dict(schema='economic-machine.gsfa-engine.v1', at_ms=time.time_ns() // 1000000,
                    state=readiness, import_error_type=error,
                    epoch=ready['receipt']['epoch'] if ready else None,
                    registered_epochs=sorted(entries),
                    previously_validated_epochs=sorted(
                        ([685] if self.g.state('gsfa_import_pin:685') else [])+
                        [row[0] for row in self.g.db.execute('SELECT epoch FROM gsfa_engine_imports ORDER BY epoch')]),
                    index_query_verified=bool(ready),
                    jobs=dict(self.g.db.execute("SELECT state,count(*) FROM frontier WHERE source='gsfa_history' GROUP BY state")),
                    raw_result_states=dict(self.g.db.execute("SELECT json_extract(body,'$.state'),count(*) FROM gsfa_engine_results GROUP BY json_extract(body,'$.state')")),
                    complete_address_history=False, full_epoch_coverage_proven=False,
                    indirect_token_owner_history_complete=False, independent_consensus_verified=False)
