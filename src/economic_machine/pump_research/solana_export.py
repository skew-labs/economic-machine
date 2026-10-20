"""Bounded retained-CAR -> complete transaction facts -> private address search.

No provider client or collection scheduler lives here. The lossless wire and
protobuf fields travel with every transaction, including votes and failures.
Decoded amounts and instruction outcomes are observations, not inferred owners
or a claim that the provider has all canonical history.
"""
import base64
import fcntl
import gzip
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from google.protobuf.json_format import MessageToDict

from .archive import nodes, link, dataframe
from .bulk_decode import decompress
from .store import canonical, encode, address
from .wire import raw_transaction, varint, committed_inner

SCHEMA = 'economic-machine.solana-export.v1'
MAX_CAR = 16 * 1024**2
MAX_LINE = 32 * 1024**2
MAX_EXPANDED = 128 * 1024**2
MAX_PACKED = 64 * 1024**2


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def un64(value, maximum=8*1024**2):
    if not isinstance(value, str) or len(value) > (maximum+2)//3*4:
        raise ValueError('EXPORT_BASE64_BOUND')
    raw = base64.b64decode(value, validate=True)
    if len(raw) > maximum:
        raise ValueError('EXPORT_BASE64_BOUND')
    return raw


def proto_dict(value):
    return MessageToDict(value, preserving_proto_field_name=True)


def transaction_facts(wire, meta_raw, meta_type, slot, order, tx_cid):
    signature, static, outer = raw_transaction(wire)  # Verify every signer.
    meta = meta_type.FromString(meta_raw)
    keys = [encode(x) for x in static] + [encode(bytes(x)) for x in
        [*meta.loaded_writable_addresses, *meta.loaded_readonly_addresses]]
    if len(keys) > 256 or any(not address(x) for x in keys):
        raise ValueError('EXPORT_ACCOUNT_KEYS')
    success = not meta.HasField('err')
    missing = []
    if len(meta.pre_balances) != len(keys): missing.append('native_pre_balance_cardinality')
    if len(meta.post_balances) != len(keys): missing.append('native_post_balance_cardinality')
    native = [dict(account=k, index=i,
        pre=str(meta.pre_balances[i]) if i < len(meta.pre_balances) else None,
        post=str(meta.post_balances[i]) if i < len(meta.post_balances) else None)
        for i, k in enumerate(keys)]
    tokens = []
    for phase, values in (('pre', meta.pre_token_balances), ('post', meta.post_token_balances)):
        seen = set()
        for item in values:
            i = int(item.account_index)
            if not 0 <= i < len(keys) or i in seen:
                raise ValueError('EXPORT_TOKEN_ACCOUNT_INDEX')
            seen.add(i)
            owner = item.owner or None
            if owner is not None and not address(owner):
                raise ValueError('EXPORT_TOKEN_OWNER')
            if not owner: missing.append('token_owner_not_recorded')
            tokens.append(dict(phase=phase, account=keys[i], index=i, owner=owner,
                mint=item.mint, amount=item.ui_token_amount.amount,
                decimals=int(item.ui_token_amount.decimals),
                raw=proto_dict(item), instruction_time_owner_proven=False))
    instructions = []
    def instruction(parent, number, program, accounts, data, stack_height, commitment):
        if not 0 <= program < len(keys) or any(not 0 <= x < len(keys) for x in accounts):
            raise ValueError('EXPORT_INSTRUCTION_INDEX')
        return dict(outer_index=parent, inner_index=number, program=keys[program],
            program_index=program, account_indices=accounts,
            accounts=[keys[x] for x in accounts], data_b64=b64(data),
            stack_height=stack_height, effect_status=commitment)
    for i, (program, accounts, data) in enumerate(outer):
        instructions.append(instruction(i, None, program, accounts, data, None,
                            'transaction_success' if success else 'transaction_failed'))
    if success:
        committed, reasons = committed_inner(meta, outer, keys)
    else:
        committed, reasons = set(), []
    if getattr(meta, 'inner_instructions_none', False): missing.append('inner_instructions_unavailable')
    if getattr(meta, 'log_messages_none', False): missing.append('log_messages_unavailable')
    seen_groups = set()
    for group in meta.inner_instructions:
        if group.index in seen_groups or not 0 <= group.index < len(outer):
            raise ValueError('EXPORT_INNER_GROUP')
        seen_groups.add(group.index)
        for j, item in enumerate(group.instructions):
            order_key = group.index*65536+j+1
            status = ('transaction_failed' if not success else
                      'committed_from_runtime_logs' if order_key in committed else
                      'unresolved_or_reverted')
            instructions.append(instruction(group.index, j, item.program_id_index,
                list(item.accounts), bytes(item.data), getattr(item, 'stack_height', None), status))
    n, offset = varint(wire)
    return dict(kind='transaction', slot=slot, order=order, signature=signature,
        signatures=[encode(wire[offset+i*64:offset+(i+1)*64]) for i in range(n)],
        transaction_cid=b64(tx_cid), success=success, fee_lamports=str(meta.fee),
        account_keys=keys, native_balances=native, token_balances=tokens,
        instructions=instructions, instruction_commit_gaps=reasons,
        metadata=proto_dict(meta), missing_fields=sorted(set(missing)),
        wire_b64=b64(wire), wire_sha256=sha(wire),
        metadata_protobuf_b64=b64(meta_raw), metadata_sha256=sha(meta_raw),
        protobuf_unknown_fields_preserved=True, signature_verified=True,
        transfer_coverage_complete=False, same_owner_proven=False)


def block_records(raw, slot, meta_type, rewards_type, guard=lambda: None):
    """One bounded CAR block in memory; transactions are yielded individually."""
    if not 0 < len(raw) <= MAX_CAR:
        raise ValueError('EXPORT_CAR_BOUND')
    guard(); ns = nodes(raw)
    found = [(cid, n) for cid, n in ns.items() if n[0] == 2]
    if len(found) != 1 or found[0][1][1] != slot:
        raise ValueError('EXPORT_BLOCK_IDENTITY')
    cid, block = found[0]
    if len(block) != 6 or len(block[4]) != 3:
        raise ValueError('EXPORT_UNSUPPORTED_BLOCK_LAYOUT')
    txs = []; seen = set()
    for e in block[3]:
        entry = ns[link(e)]
        if entry[0] != 1: raise ValueError('EXPORT_ENTRY_TYPE')
        for t in entry[3]:
            tc = link(t); node = ns[tc]
            if tc in seen or node[0] != 0 or node[3] != slot:
                raise ValueError('EXPORT_TRANSACTION_MEMBERSHIP')
            seen.add(tc); txs.append((tc, node))
    # A referenced block must account for every transaction node in its slice.
    if len(txs) != sum(n[0] == 0 for n in ns.values()):
        raise ValueError('EXPORT_UNREFERENCED_TRANSACTION')
    yield dict(kind='block', schema=SCHEMA, slot=slot, source_car_sha256=sha(raw),
        source_car_bytes=len(raw), block_cid=b64(cid), parent_slot=block[4][0],
        block_time=block[4][1], block_height=block[4][2], transactions=len(txs),
        car_content_hashes_verified=True, independent_consensus_verified=False,
        full_wallet_history=False, vote_filter_applied=False,
        source_block_metadata_preserved=True)
    orders = set(); signatures = set(); fields = set(); totals = dict(transactions=0, failed=0,
        native_balance_rows=0, token_balance_rows=0, inner_instructions=0)
    for fallback, (tc, node) in enumerate(txs):
        guard(); order = node[4] if len(node) > 4 and node[4] is not None else fallback
        if type(order) is not int or order < 0 or order in orders:
            raise ValueError('EXPORT_TRANSACTION_ORDER')
        orders.add(order)
        wire = dataframe(node[1], ns); packed = dataframe(node[2], ns)
        if not packed: raise ValueError('EXPORT_MISSING_TRANSACTION_METADATA')
        record = transaction_facts(wire, decompress(packed), meta_type, slot, order, tc)
        if record['signature'] in signatures: raise ValueError('EXPORT_DUPLICATE_SIGNATURE')
        signatures.add(record['signature']); fields.update(record['missing_fields'])
        totals['transactions'] += 1; totals['failed'] += not record['success']
        totals['native_balance_rows'] += len(record['native_balances'])
        totals['token_balance_rows'] += len(record['token_balances'])
        totals['inner_instructions'] += sum(x['inner_index'] is not None for x in record['instructions'])
        yield record
    guard()
    if block[5] is None:
        reward = dict(kind='block_rewards', slot=slot, availability='source_link_absent',
                      rewards=[], missing_fields=['block_rewards_unavailable'])
    else:
        node = ns[link(block[5])]
        if node[0] != 5 or node[1] != slot or len(node) != 3:
            raise ValueError('EXPORT_REWARD_MEMBERSHIP')
        packed = dataframe(node[2], ns)
        if not packed:
            reward = dict(kind='block_rewards', slot=slot, availability='source_frame_empty',
                          rewards=[], missing_fields=['block_rewards_unavailable'])
        else:
            proto = decompress(packed); parsed = rewards_type.FromString(proto)
            reward = dict(kind='block_rewards', slot=slot, availability='source_recorded',
                rewards=proto_dict(parsed), protobuf_b64=b64(proto), protobuf_sha256=sha(proto),
                missing_fields=[])
    fields.update(reward['missing_fields']); yield reward
    yield dict(kind='end', slot=slot, totals=totals, missing_fields=sorted(fields),
        source_transaction_membership_complete=True, source_record_export_complete=True,
        source_fields_without_gaps=not fields, full_wallet_history=False,
        independent_consensus_verified=False)


def file_hash(path, maximum=MAX_PACKED):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('EXPORT_FILE_BOUND')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for raw in iter(lambda: stream.read(1024**2), b''): digest.update(raw)
    return digest.hexdigest()


def read_pack(path, expected_source=None):
    """Strict bounded replay; no partially decoded artifact can become searchable."""
    if Path(path).stat().st_size > MAX_PACKED: raise ValueError('EXPORT_PACK_BOUND')
    size = 0; ordinal = 0; txs = 0; saw_rewards = False; ended = False
    with gzip.open(path, 'rb') as stream:
        while True:
            line = stream.readline(MAX_LINE+1)
            if not line: break
            size += len(line)
            if len(line) > MAX_LINE or size > MAX_EXPANDED or not line.endswith(b'\n'):
                raise ValueError('EXPORT_EXPANSION_BOUND')
            record = json.loads(line)
            if canonical(record).encode()+b'\n' != line or ended:
                raise ValueError('EXPORT_NONCANONICAL_OR_TRAILING')
            kind = record.get('kind')
            if ordinal == 0:
                if kind != 'block' or record.get('schema') != SCHEMA:
                    raise ValueError('EXPORT_HEADER')
                header = record
                if expected_source and record['source_car_sha256'] != expected_source:
                    raise ValueError('EXPORT_SOURCE_CHANGED')
            elif record.get('slot') != header['slot']:
                raise ValueError('EXPORT_SLOT_MISMATCH')
            elif kind == 'transaction' and not saw_rewards:
                wire = un64(record['wire_b64']); meta = un64(record['metadata_protobuf_b64'])
                if sha(wire) != record['wire_sha256'] or sha(meta) != record['metadata_sha256']:
                    raise ValueError('EXPORT_TRANSACTION_HASH')
                txs += 1
            elif kind == 'block_rewards' and not saw_rewards:
                saw_rewards = True
                if 'protobuf_b64' in record and sha(un64(record['protobuf_b64'])) != record['protobuf_sha256']:
                    raise ValueError('EXPORT_REWARD_HASH')
            elif kind == 'end' and saw_rewards:
                ended = True
                if txs != header['transactions'] or txs != record['totals']['transactions']:
                    raise ValueError('EXPORT_TRANSACTION_COUNT')
            else: raise ValueError('EXPORT_RECORD_ORDER')
            ordinal += 1; yield record
    if not ended: raise ValueError('EXPORT_INCOMPLETE_PACK')


def verify_pack(path, raw, slot, meta_type, rewards_type, guard=lambda: None):
    """Re-derive every exported field from retained source before admission."""
    absent = object()
    actual = read_pack(path, sha(raw))
    expected = block_records(raw, slot, meta_type, rewards_type, guard)
    for a, b in itertools.zip_longest(actual, expected, fillvalue=absent):
        guard()
        if a is absent or b is absent or a != b:
            raise ValueError('EXPORT_SOURCE_FACT_MISMATCH')


def archive_pack(base, path, digest, guard=lambda:None):
    """Existing shared budget/native Blob uploader, then a complete restore hash."""
    from .research_cloud import Cloud
    guard(); cloud=Cloud(base)
    try:
        proof=cloud.upload(path, digest); guard()
        fresh=not (cloud.cache/digest).exists()
        restored=hashlib.sha256(); count=0
        with cloud.cached(proof['sha256'],proof['bytes']) as stream:
            while raw:=stream.read(1024**2):
                guard(); count+=len(raw);restored.update(raw)
        if count!=proof['bytes'] or restored.hexdigest()!=digest:
            raise ValueError('EXPORT_CLOUD_RESTORE_MISMATCH')
        return dict(**proof,full_download_verified=True,restore_sha256=restored.hexdigest(),
            fresh_restore_from_blob=fresh,shared_budget_sha256=cloud.spend.hash,
            original_car_preserved=True)
    finally:cloud.close()


class Facts:
    """Separate private index. Every block commits once after artifact readback."""
    def __init__(self, out, policy, guard=lambda: None):
        self.out = Path(out); self.out.mkdir(parents=True, exist_ok=True)
        self.guard = guard; self.policy = policy
        self.lock = (self.out/'writer.lock').open('a+b')
        try: fcntl.flock(self.lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            self.lock.close(); raise ValueError('EXPORT_WRITER_BUSY')
        self.db = sqlite3.connect(self.out/'facts.sqlite3', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS blocks(slot INTEGER PRIMARY KEY,source_hash TEXT NOT NULL,
            artifact TEXT NOT NULL,artifact_hash TEXT NOT NULL,transactions INTEGER,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS transactions(slot INTEGER,ord INTEGER,signature TEXT,body TEXT,
            record_hash TEXT,PRIMARY KEY(slot,ord),UNIQUE(slot,signature)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS appearances(address TEXT,role TEXT,slot INTEGER,ord INTEGER,
            PRIMARY KEY(address,role,slot,ord)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS rewards(slot INTEGER PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS reward_appearances(address TEXT,slot INTEGER,ord INTEGER,body TEXT,
            record_hash TEXT,PRIMARY KEY(address,slot,ord)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS failures(id TEXT PRIMARY KEY,slot INTEGER,stage TEXT,code TEXT,artifact TEXT);
        ''')
        body = canonical(policy)
        try:
            with self.db:
                old = self.db.execute('SELECT body FROM identity').fetchall()
                if old and [r[0] for r in old] != [body]: raise ValueError('EXPORT_POLICY_CHANGED')
                self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)', (body,))
        except BaseException:
            self.close(); raise

    def close(self):
        self.db.close(); self.lock.close()

    def export(self, raw, slot, meta_type, rewards_type, fault=None, archive=None):
        self.guard(); source = sha(raw)
        old = self.db.execute('SELECT * FROM blocks WHERE slot=?', (slot,)).fetchone()
        if old:
            if old['source_hash'] != source: raise ValueError('EXPORT_SLOT_CONFLICT')
            if file_hash(self.out/old['artifact']) != old['artifact_hash']:
                raise ValueError('EXPORT_COMMITTED_ARTIFACT_CHANGED')
            return dict(slot=slot, resumed=True, transactions=old['transactions'], artifact_hash=old['artifact_hash'])
        name = f'{slot}-{source}.jsonl.gz'; path = self.out/name
        partial = self.out/(name+'.partial-'+uuid.uuid4().hex); stage = 'decode'
        try:
            if not path.exists():
                size = 0
                with partial.open('xb') as target:
                    with gzip.GzipFile(filename='', mode='wb', compresslevel=3, fileobj=target, mtime=0) as packed:
                        for n, record in enumerate(block_records(raw, slot, meta_type, rewards_type, self.guard)):
                            self.guard(); line = canonical(record).encode()+b'\n'; size += len(line)
                            if len(line) > MAX_LINE or size > MAX_EXPANDED:
                                raise ValueError('EXPORT_EXPANSION_BOUND')
                            packed.write(line)
                            if target.tell() > MAX_PACKED: raise ValueError('EXPORT_PACK_BOUND')
                            if fault: fault('record', n)
                    target.flush(); os.fsync(target.fileno())
                os.replace(partial, path)
                fd = os.open(self.out, os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
            stage = 'readback'; digest = file_hash(path)
            # Full gzip/CRC and logical framing verification before indexing.
            verify_pack(path,raw,slot,meta_type,rewards_type,self.guard)
            if fault: fault('verified', 0)
            stage = 'archive'; cloud_proof=archive(path,digest) if archive else None
            if archive and (not isinstance(cloud_proof,dict) or cloud_proof.get('sha256')!=digest
                    or cloud_proof.get('bytes')!=path.stat().st_size
                    or cloud_proof.get('full_download_verified') is not True):
                raise ValueError('EXPORT_CLOUD_RECEIPT')
            self.guard()
            stage = 'index'; total = 0; header = end = None
            with self.db:
                for record in read_pack(path, source):
                    self.guard(); kind = record['kind']
                    if kind == 'block': header = record
                    elif kind == 'transaction':
                        stored = {k:v for k,v in record.items() if k not in ('wire_b64','metadata_protobuf_b64')}
                        self.db.execute('INSERT INTO transactions VALUES(?,?,?,?,?)',
                            (slot, record['order'], record['signature'], canonical(stored), sha(canonical(record).encode())))
                        appearances = {(k,'account_key') for k in record['account_keys']}
                        appearances.update((x['owner'],'token_owner_boundary') for x in record['token_balances'] if x['owner'])
                        self.db.executemany('INSERT INTO appearances VALUES(?,?,?,?)',
                            ((a,r,slot,record['order']) for a,r in appearances))
                        total += 1
                        if fault: fault('index', total)
                    elif kind == 'block_rewards':
                        self.db.execute('INSERT INTO rewards VALUES(?,?)', (slot,canonical(record)))
                        if record['availability']=='source_recorded':
                            for i, item in enumerate(record['rewards'].get('rewards', [])):
                                if not address(item.get('pubkey','')):
                                    raise ValueError('EXPORT_REWARD_ADDRESS')
                                self.db.execute('INSERT INTO reward_appearances VALUES(?,?,?,?,?)',
                                    (item['pubkey'],slot,i,canonical(item),sha(canonical(record).encode())))
                    elif kind == 'end': end = record
                if not header or not end or total != header['transactions']:
                    raise ValueError('EXPORT_INDEX_CARDINALITY')
                if file_hash(path) != digest: raise ValueError('EXPORT_ARTIFACT_CHANGED_DURING_INDEX')
                self.db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?)',
                    (slot,source,name,digest,total,canonical(dict(header=header,end=end,cloud=cloud_proof))))
            return dict(slot=slot,resumed=False,transactions=total,artifact_hash=digest,
                        artifact_bytes=path.stat().st_size,source_bytes=len(raw),totals=end['totals'],cloud=cloud_proof)
        except BaseException as error:
            self.db.rollback()
            with self.db:
                self.db.execute('INSERT INTO failures VALUES(?,?,?,?,?)',
                    (uuid.uuid4().hex,slot,stage,type(error).__name__+':'+str(error)[:120],
                     partial.name if partial.exists() else name if path.exists() else None))
            raise


def search(out, wallet, limit=20):
    if not address(wallet) or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('EXPORT_SEARCH_BOUND')
    out = Path(out); db = sqlite3.connect((out/'facts.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory = sqlite3.Row; db.execute('PRAGMA query_only=ON')
    deadline = time.monotonic()+2; db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
    try:
        rows = db.execute('''SELECT t.*,b.artifact,b.artifact_hash,b.source_hash
            FROM (SELECT DISTINCT slot,ord FROM appearances WHERE address=? ORDER BY slot DESC,ord DESC LIMIT ?) a
            JOIN transactions t ON (t.slot,t.ord)=(a.slot,a.ord) JOIN blocks b ON b.slot=t.slot
            ORDER BY t.slot DESC,t.ord DESC''',(wallet,limit+1)).fetchall()
        rewards = db.execute('''SELECT r.*,b.artifact,b.artifact_hash,b.source_hash
            FROM reward_appearances r JOIN blocks b ON b.slot=r.slot
            WHERE r.address=? ORDER BY r.slot DESC,r.ord DESC LIMIT ?''',(wallet,limit+1)).fetchall()
        selected = sorted([('transaction',r) for r in rows]+[('reward',r) for r in rewards],
                          key=lambda x:(x[1]['slot'],x[0],x[1]['ord']),reverse=True)[:limit]
        for artifact, digest in {(r['artifact'],r['artifact_hash']) for _,r in selected}:
            if Path(artifact).name != artifact or file_hash(out/artifact) != digest:
                raise ValueError('EXPORT_SEARCH_ARTIFACT_CHANGED')
        hits = [dict(kind=kind,slot=r['slot'],**{kind:json.loads(r['body'])},record_sha256=r['record_hash'],
            export_sha256=r['artifact_hash'],source_car_sha256=r['source_hash']) for kind,r in selected]
        return dict(schema=SCHEMA,hits=hits,truncated=len(rows)+len(rewards)>limit,
            indexed_blocks=db.execute('SELECT count(*) FROM blocks').fetchone()[0],
            full_wallet_history=False,current_balance_verified=False,same_owner_proven=False,
            independent_consensus_verified=False)
    finally: db.close()
