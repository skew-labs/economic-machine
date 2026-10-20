import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.pump_research.gsfa_engine import GsfaEngine
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph


SLOT = 685 * 432000 + 120


def key(n):
    return encode(bytes([n]) * 32)


class FakeIndex:
    def __init__(self, directory, epoch, budget):
        self.identity = 'a' * 64
        self.root_cid = '01711220' + '00' * 32
        self.epoch = epoch
        self.budget = budget
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
    def close(self):
        pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'evidence').mkdir()
        self.g = Graph(self.base)
        self.addCleanup(self.g.close)
        self.engine = GsfaEngine(self.g)
        self.folder = self.base / 'evidence/address-epoch-v1'
        self.folder.mkdir()
        self.scope = SimpleNamespace(guard=lambda extra=0: None,
                                     grant={'deadline_ms': int(time.time() * 1000) + 120000})
        with self.g.db:
            self.g.db.execute('INSERT INTO roots VALUES(?,?)', (key(1), '{}'))
            self.parent = self.g.enqueue(key(1), key(2), SLOT, 1729000000000, 0,
                                         'local_forward', after_order=(7, 0))
        self.patcher = patch('economic_machine.pump_research.gsfa_engine.GsfaIndex', FakeIndex)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def receipt(self, epoch=685):
        folder = self.folder if epoch == 685 else self.base / ('evidence/address-epochs-v2/epochs/'+str(epoch))
        folder.mkdir(parents=True, exist_ok=True)
        directory = folder / ('gsfa-index-'+str(epoch))
        directory.mkdir(exist_ok=True)
        rows = []
        for name in ('manifest', 'linked-log', 'pubkey-to-offset-and-size.index'):
            raw = name.encode()
            (directory / name).write_bytes(raw)
            rows.append(dict(name=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        body = dict(schema='economic-machine.gsfa-import.v1', epoch=epoch, directory=directory.name,
                    source_sha256='b' * 64, index_identity='a' * 64, root_cid=FakeIndex(None, epoch, None).root_cid,
                    index_query_verified=True, extraction=dict(files=rows, member_bytes=sum(r['bytes'] for r in rows),
                        compressed_sha256='b' * 64, format_inspected=True, extracted=True,
                        downloaded_chunk_hashes_verified=True))
        (folder / 'download-manifest.json').write_text(canonical(dict(sha256='b' * 64)))
        (folder / 'import-receipt.json').write_text(canonical(body))
        return body

    def write_receipt(self, body):
        (self.folder / 'import-receipt.json').write_text(canonical(body))

    def registry(self, *epochs):
        folder = self.base/'evidence/address-epochs-v2'
        folder.mkdir(exist_ok=True)
        grant = folder/'grant.json'
        if not grant.exists():
            grant.write_text(canonical(dict(schema='synthetic-scope-fixture', epochs=[1008,1009])))
        entries = []
        for epoch in epochs:
            receipt = folder/('epochs/'+str(epoch)+'/import-receipt.json')
            if not receipt.exists():
                self.receipt(epoch)
            entries.append(dict(epoch=epoch, receipt_path=receipt.relative_to(self.base).as_posix(),
                receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
                grant_path=grant.relative_to(self.base).as_posix(),
                grant_sha256=hashlib.sha256(grant.read_bytes()).hexdigest()))
        value = dict(schema='economic-machine.gsfa-import-registry.v1', entries=entries)
        (folder/'registry.json').write_text(canonical(value))
        return value

    def write_registry(self, value):
        self.engine.registry.write_text(canonical(value))

    def job(self):
        return dict(self.g.db.execute("SELECT * FROM frontier WHERE source='gsfa_history' ORDER BY rowid LIMIT 1").fetchone())

    def binding(self, job):
        return json.loads(self.g.db.execute('SELECT body FROM gsfa_engine_jobs WHERE job=?', (job['id'],)).fetchone()[0])

    def result(self, state='verified_success', slot=SLOT + 1):
        return dict(id=1, result=dict(state=state, slot=slot, tx_order=8, block_time=1729000001,
                    outgoing=[dict(source=key(2), destination=key(3), ix=0, amount='10', mint='SOL')]))

    def test_missing_import_does_not_activate_or_seed(self):
        out = self.engine.seed(self.scope)
        self.assertEqual(out['seeded'], 0)
        self.assertEqual(self.engine.snapshot()['state'], 'waiting_for_verified_import')
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_jobs').fetchone()[0], 0)

    def test_import_can_arrive_after_engine_creation(self):
        self.assertEqual(self.engine.seed(self.scope)['seeded'], 0)
        self.receipt()
        self.assertEqual(self.engine.seed(self.scope)['seeded'], 1)
        self.assertEqual(self.engine.seed(self.scope)['seeded'], 0)

    def test_download_only_or_unverified_import_cannot_activate(self):
        body = self.receipt()
        body['index_query_verified'] = False
        self.write_receipt(body)
        with self.assertRaisesRegex(ValueError, 'IMPORT_NOT_READY'):
            self.engine.seed(self.scope)

    def test_compressed_source_identity_is_checked(self):
        self.receipt()
        (self.folder / 'download-manifest.json').write_text(canonical(dict(sha256='c' * 64)))
        with self.assertRaisesRegex(ValueError, 'IMPORT_SOURCE_MISMATCH'):
            self.engine.seed(self.scope)

    def test_directory_escape_and_member_symlink_are_rejected(self):
        body = self.receipt()
        body['directory'] = '../outside'
        self.write_receipt(body)
        with self.assertRaisesRegex(ValueError, 'IMPORT_NOT_READY'):
            self.engine.seed(self.scope)
        body['directory'] = 'gsfa-index-685'
        self.write_receipt(body)
        member = self.folder / body['directory'] / 'manifest'
        member.rename(member.with_name('saved-manifest'))
        member.symlink_to('saved-manifest')
        with self.assertRaisesRegex(ValueError, 'MEMBER_LINK'):
            self.engine.seed(self.scope)

    def test_member_size_and_pin_detect_local_mutation(self):
        self.receipt()
        self.engine.seed(self.scope)
        member = self.folder / 'gsfa-index-685/manifest'
        member.write_bytes(b'changed!')  # same length, different file stat identity
        with self.assertRaisesRegex(ValueError, 'PINNED_IMPORT_CHANGED'):
            self.engine.seed(self.scope)

    def test_receipt_pin_is_not_silently_replaced(self):
        body = self.receipt()
        self.engine.seed(self.scope)
        body['note'] = 'different receipt'
        self.write_receipt(body)
        with self.assertRaisesRegex(ValueError, 'PINNED_IMPORT_CHANGED'):
            self.engine.seed(self.scope)

    def test_participants_wallet_requests_but_not_token_mints_seed(self):
        self.receipt()
        with self.g.db:
            self.g.enqueue(key(1), key(1), SLOT, None, 0, 'local_forward')
            self.g.db.execute('INSERT INTO roots VALUES(?,?)', (key(4), canonical(dict(root_kind='wallet'))))
            self.g.enqueue(key(4), key(4), 0, None, 0, 'indexed_forward')
            self.g.enqueue(key(1), key(5), 702 * 432000, None, 0, 'local_forward')
        self.assertEqual(self.engine.seed(self.scope)['seeded'], 2)
        wallets = {r[0] for r in self.g.db.execute("SELECT wallet FROM frontier WHERE source='gsfa_history'")}
        self.assertEqual(wallets, {key(2), key(4)})

    def test_custody_context_is_inherited_at_seed(self):
        self.receipt()
        with self.g.db:
            self.g.db.execute('INSERT INTO custody_jobs VALUES(?,?)', (self.parent, 'private-ledger-gap'))
        self.engine.seed(self.scope)
        self.assertEqual(self.g.db.execute('SELECT case_id FROM custody_jobs WHERE job=?', (self.job()['id'],)).fetchone()[0], 'private-ledger-gap')

    def test_original_parent_anchor_change_is_rejected(self):
        self.receipt()
        self.engine.seed(self.scope)
        job = self.job()
        body = self.binding(job)
        with self.g.db:
            self.g.db.execute('UPDATE frontier SET slot=slot+1 WHERE id=?', (self.parent,))
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_PARENT_CHANGED'):
            self.engine._parent(job, body)

    def test_verified_result_wakes_original_reader_without_cursor_reset_and_seeds_recipient(self):
        self.receipt()
        self.engine.seed(self.scope)
        job = self.job()
        body = self.binding(job)
        with self.g.db:
            child = self.g.enqueue(key(1), key(2), SLOT, 1729000000000, 0, 'indexed_forward', after_order=(7, 0))
            self.g.db.execute("UPDATE frontier SET state='indexed_source_exhausted',cursor=? WHERE id=?", (canonical(dict(transfer=999, incoming_transfer=88)), child))
            self.g.db.execute('INSERT INTO custody_jobs VALUES(?,?)', (job['id'], 'gap'))
        self.engine._apply(job, body, self.engine._receipt(), [self.result()])
        reader = self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (child,)).fetchone()
        self.assertEqual(reader['state'], 'pending')
        self.assertEqual(json.loads(reader['cursor']), dict(transfer=999, incoming_transfer=88))
        recipient = self.g.db.execute("SELECT id FROM frontier WHERE source='gsfa_history' AND wallet=?", (key(3),)).fetchone()
        self.assertIsNotNone(recipient)
        self.assertEqual(self.g.db.execute('SELECT case_id FROM custody_jobs WHERE job=?', (recipient[0],)).fetchone()[0], 'gap')

    def test_failed_or_before_anchor_result_does_not_derive_recipient(self):
        self.receipt()
        self.engine.seed(self.scope)
        job = self.job()
        body = self.binding(job)
        self.engine._apply(job, body, self.engine._receipt(), [self.result('verified_failed_transaction')])
        before = self.result(slot=SLOT)
        before['id'] = 2
        before['result']['tx_order'] = 6
        self.engine._apply(job, body, self.engine._receipt(), [before])
        self.assertIsNone(self.g.db.execute("SELECT 1 FROM frontier WHERE source='gsfa_history' AND wallet=?", (key(3),)).fetchone())

    def test_existing_scope_failure_prevents_seeding(self):
        self.receipt()
        def fail(*args):
            raise ValueError('CORE_DEADLINE')
        self.scope.guard = fail
        with self.assertRaisesRegex(ValueError, 'CORE_DEADLINE'):
            self.engine.seed(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_jobs').fetchone()[0], 0)

    def test_new_epochs_append_without_resetting_legacy_pin_jobs_or_cursors(self):
        self.receipt()
        self.engine.seed(self.scope)
        old = self.job()
        pin = self.g.state('gsfa_import_pin:685')
        with self.g.db:
            self.g.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',
                (canonical(dict(after_result_id=47)), 'gsfa_index_drained', old['id']))
        self.registry(1008, 1009)
        first = self.engine.seed(self.scope)
        second = self.engine.seed(self.scope)
        self.assertEqual((first['epoch'],second['epoch']), (1008,1009))
        self.assertEqual((first['seeded'],second['seeded']), (1,1))
        preserved = self.g.db.execute('SELECT cursor,state FROM frontier WHERE id=?', (old['id'],)).fetchone()
        self.assertEqual(json.loads(preserved['cursor']), dict(after_result_id=47))
        self.assertEqual(preserved['state'], 'gsfa_index_drained')
        self.assertEqual(self.g.state('gsfa_import_pin:685'), pin)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_jobs').fetchone()[0], 3)
        self.assertEqual(self.engine.seed(self.scope)['epoch'], 685)

    def test_later_epoch_uses_its_own_slot_bounds_and_index_epoch(self):
        self.registry(1008)
        with self.g.db:
            self.g.enqueue(key(1), key(5), 1008*432000+99, None, 0, 'local_forward')
            self.g.enqueue(key(1), key(6), 1009*432000, None, 0, 'local_forward')
        out = self.engine.seed(self.scope)
        self.assertEqual(out['epoch'], 1008)
        self.assertEqual(out['seeded'], 2)
        self.assertEqual({r[0] for r in self.g.db.execute("SELECT wallet FROM frontier WHERE source='gsfa_history'")},
                         {key(2),key(5)})
        bodies = [json.loads(r[0]) for r in self.g.db.execute('SELECT body FROM gsfa_engine_jobs')]
        self.assertEqual({r['last_slot'] for r in bodies}, {1009*432000-1})
        self.assertEqual({r['first_slot'] for r in bodies}, {1008*432000,1008*432000+99})
        with self.engine._open_index(self.engine._receipt(epoch=1008), self.scope) as index:
            self.assertEqual(index.epoch, 1008)

    def test_epoch_rotation_keeps_global_seed_bound(self):
        self.receipt()
        self.registry(1008,1009)
        with self.g.db:
            for number in range(10,90):
                self.g.enqueue(key(1), key(number), SLOT, None, 0, 'local_forward')
        results = [self.engine.seed(self.scope) for _ in range(3)]
        self.assertEqual([r['epoch'] for r in results], [685,1008,1009])
        self.assertEqual([r['seeded'] for r in results], [64,64,64])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_jobs').fetchone()[0], 192)

    def test_missing_or_replaced_registered_receipt_is_rejected(self):
        value = self.registry(1008)
        receipt = self.base/value['entries'][0]['receipt_path']
        original = receipt.read_bytes()
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, 'REGISTERED_RECEIPT_MISSING'):
            self.engine.seed(self.scope)
        receipt.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError, 'REGISTERED_SOURCE_CHANGED'):
            self.engine.seed(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_imports').fetchone()[0], 0)

    def test_grant_provenance_hash_is_checked_without_replacing_old_grant(self):
        self.registry(1008)
        grant = self.base/'evidence/address-epochs-v2/grant.json'
        grant.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'REGISTERED_SOURCE_CHANGED'):
            self.engine.seed(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM gsfa_engine_jobs').fetchone()[0], 0)

    def test_registry_cannot_replace_remove_or_duplicate_accepted_epochs(self):
        value = self.registry(1008)
        self.engine.seed(self.scope)
        self.write_registry(dict(value, entries=[]))
        with self.assertRaisesRegex(ValueError, 'REGISTERED_IMPORT_CHANGED'):
            self.engine.seed(self.scope)
        self.write_registry(dict(value, entries=value['entries']*2))
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OR_LEGACY'):
            self.engine.seed(self.scope)
        self.write_registry(dict(value, entries=[dict(value['entries'][0],receipt_sha256='c'*64)]))
        with self.assertRaisesRegex(ValueError, 'REGISTERED_IMPORT_CHANGED'):
            self.engine.seed(self.scope)
        self.engine.registry.unlink()
        with self.assertRaisesRegex(ValueError, 'REGISTRY_REMOVED'):
            self.engine.seed(self.scope)

    def test_registry_cannot_override_legacy_epoch(self):
        value = self.registry(1008)
        value['entries'][0]['epoch'] = 685
        self.write_registry(value)
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OR_LEGACY'):
            self.engine.seed(self.scope)

    def test_registry_rejects_path_escape_and_symlinked_parent(self):
        value = self.registry(1008)
        for escaped in ('../import-receipt.json','/tmp/import-receipt.json','evidence/../import-receipt.json',
                        'https://example.invalid/import-receipt.json'):
            self.write_registry(dict(value, entries=[dict(value['entries'][0],receipt_path=escaped)]))
            with self.assertRaisesRegex(ValueError, 'REGISTRY_PATH'):
                self.engine.seed(self.scope)
        self.write_registry(value)
        folder = self.base/'evidence/address-epochs-v2/epochs/1008'
        saved = folder.with_name('saved-1008')
        folder.rename(saved)
        folder.symlink_to(saved.name)
        with self.assertRaisesRegex(ValueError, 'REGISTRY_LINK'):
            self.engine.seed(self.scope)

    def test_registered_receipt_still_requires_verified_import_and_matching_epoch(self):
        value = self.registry(1008)
        path = self.base/value['entries'][0]['receipt_path']
        receipt = json.loads(path.read_text())
        for changed in (dict(receipt,index_query_verified=False), dict(receipt,epoch=1009)):
            path.write_text(canonical(changed))
            value['entries'][0]['receipt_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.write_registry(value)
            with self.assertRaisesRegex(ValueError, 'IMPORT_NOT_READY'):
                self.engine.seed(self.scope)

    def test_future_recipient_seeds_that_epoch_without_resetting_original_anchor(self):
        self.registry(1008)
        self.engine.seed(self.scope)
        job = self.job()
        body = self.binding(job)
        self.engine._apply(job, body, self.engine._receipt(epoch=1008), [self.result(slot=1008*432000+1)])
        row = self.g.db.execute("SELECT b.epoch,b.body FROM gsfa_engine_jobs b JOIN frontier f ON f.id=b.job WHERE f.wallet=?", (key(3),)).fetchone()
        self.assertEqual(row['epoch'], 1008)
        self.assertEqual(json.loads(row['body'])['anchor']['slot'], 1008*432000+1)
        self.assertEqual(self.g.anchor(self.g.db.execute('SELECT * FROM frontier WHERE id=?', (self.parent,)).fetchone())['slot'], SLOT)

    def test_run_resolves_import_from_bound_epoch_not_latest_selected_epoch(self):
        self.receipt()
        self.registry(1008)
        self.engine.seed(self.scope)
        legacy = self.job()
        self.engine.seed(self.scope)
        self.assertEqual(self.g.state('gsfa_engine_ready')['epoch'], 1008)
        # Stop immediately after the bound receipt check, before any CAR/model IO.
        with patch('economic_machine.pump_research.cli.proto', side_effect=RuntimeError('stop-before-io')):
            with patch.object(self.engine, '_receipt', wraps=self.engine._receipt) as read:
                with self.assertRaisesRegex(RuntimeError, 'stop-before-io'):
                    self.engine.run(legacy, self.scope)
                self.assertEqual(read.call_args.kwargs['epoch'], 685)


if __name__ == '__main__':
    unittest.main()
