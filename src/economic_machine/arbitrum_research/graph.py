"""Chain-scoped, provenance-bearing EVM observations, never ownership assertions.

The index is a derived view. Immutable source pages remain authoritative and must
be retained separately. Successful receipt logs and successful value traces are
not independent proof of canonicality or complete trace coverage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

CHAIN_ID = 42161
NITRO_FIRST_BLOCK = 22_207_818
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
_ZERO = "0x" + "0" * 40
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fixed(value: Any, size: int, name: str) -> str:
    if not isinstance(value, str) or len(value) != size * 2 + 2 or not value.startswith("0x") or not _HEX.fullmatch(value[2:]):
        raise ValueError(f"invalid {name}")
    return value.lower()


def _address(value: Any) -> str:
    return _fixed(value, 20, "address")


def _uint(value: Any, name: str, bits: int = 256) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and (value.isdigit() or value.startswith("0x")):
        result = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise ValueError(f"invalid {name}")
    if not 0 <= result < (1 << bits):
        raise ValueError(f"invalid {name}")
    return result


def _status(value: Any) -> int | None:
    if not isinstance(value, (int, str, bool)):
        return None
    if value in (0, "0", "0x0", False):
        return 0
    if value in (1, "1", "0x1", True):
        return 1
    return None


def _data(value: Any) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2 or (value[2:] and not _HEX.fullmatch(value[2:])):
        raise ValueError("invalid event data")
    return bytes.fromhex(value[2:])


def _topic_address(value: Any) -> str:
    topic = _fixed(value, 32, "address topic")
    if topic[2:26] != "0" * 24:
        raise ValueError("non-address event topic")
    return "0x" + topic[26:]


def _path(trace: dict) -> tuple[int, ...]:
    path = trace.get("traceAddress")
    if not isinstance(path, list):
        raise ValueError("traceAddress missing")
    return tuple(_uint(x, "trace path", 31) for x in path)


def _failed(trace: dict) -> bool:
    return bool(trace.get("error") or trace.get("revertReason"))


def _batch(data: bytes) -> list[tuple[int, int]]:
    if len(data) < 128 or len(data) % 32:
        raise ValueError("invalid ERC1155 batch size")
    words = [int.from_bytes(data[i:i + 32], "big") for i in range(0, len(data), 32)]
    offsets = words[:2]
    arrays = []
    intervals = []
    for offset in offsets:
        if offset % 32 or offset < 64 or offset // 32 >= len(words):
            raise ValueError("invalid ERC1155 array offset")
        first = offset // 32
        count = words[first]
        end = first + 1 + count
        if end > len(words):
            raise ValueError("invalid ERC1155 array length")
        arrays.append(words[first + 1:end])
        intervals.append((first, end))
    if max(intervals[0][0], intervals[1][0]) < min(intervals[0][1], intervals[1][1]):
        raise ValueError("overlapping ERC1155 arrays")
    if len(arrays[0]) != len(arrays[1]):
        raise ValueError("unequal ERC1155 arrays")
    if sum(end - start for start, end in intervals) + 2 != len(words):
        raise ValueError("trailing or missing ERC1155 data")
    return list(zip(*arrays))


class Graph:
    """An atomic contiguous shard; use the previous shard's tip to start another."""

    def __init__(self, path: str | Path, *, initial_start: int = 0, previous_block_hash: str | None = None, chain_id: int = CHAIN_ID, transaction_occurrences: bool = False):
        if type(chain_id) is not int or chain_id not in (42161, 4663):
            raise ValueError('unsupported graph chain')
        if type(transaction_occurrences) is not bool:raise ValueError('invalid transaction occurrence mode')
        self.transaction_occurrences = transaction_occurrences
        self.chain_id = chain_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initial_start = _uint(initial_start, "initial_start", 63)
        if initial_start and previous_block_hash is None:
            raise ValueError("non-genesis shard requires previous_block_hash")
        if previous_block_hash is not None:
            previous_block_hash = _fixed(previous_block_hash, 32, "previous block hash")
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        identity = {"chain_id": self.chain_id, "initial_start": initial_start,
                    "previous_block_hash": previous_block_hash, "schema": 2 if transaction_occurrences else 1}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata' AND type='table'").fetchone():
            old_identity=self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if old_identity and json.loads(old_identity[0])!=identity:
                self.db.close();raise ValueError("graph identity mismatch")
        hash_column="hash TEXT NOT NULL" if transaction_occurrences else "hash TEXT PRIMARY KEY"
        transaction_key="PRIMARY KEY(block,tx_index)" if transaction_occurrences else "UNIQUE(block,tx_index)"
        self.db.executescript(f"""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages(source_sha256 TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,
          start INTEGER NOT NULL,end INTEGER NOT NULL,cloud_ref TEXT NOT NULL,UNIQUE(start,end));
        CREATE TABLE IF NOT EXISTS blocks(number INTEGER PRIMARY KEY,hash TEXT UNIQUE NOT NULL,
          parent_hash TEXT NOT NULL,source_sha256 TEXT NOT NULL REFERENCES pages(source_sha256),
          timestamp TEXT,trace_coverage TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS transactions({hash_column},block INTEGER NOT NULL REFERENCES blocks(number),
          tx_index INTEGER NOT NULL,sender TEXT NOT NULL,recipient TEXT,status INTEGER,value TEXT NOT NULL,
          {transaction_key});
        CREATE TABLE IF NOT EXISTS addresses(address TEXT PRIMARY KEY,chain_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY,chain_id INTEGER NOT NULL,block INTEGER NOT NULL,
          tx_hash TEXT NOT NULL,tx_index INTEGER NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,
          asset TEXT NOT NULL,token_id TEXT,amount TEXT NOT NULL,kind TEXT NOT NULL,
          log_index INTEGER,trace_address TEXT,item_index INTEGER,evidence_level TEXT NOT NULL,
          source_sha256 TEXT NOT NULL REFERENCES pages(source_sha256));
        CREATE INDEX IF NOT EXISTS edges_sender ON edges(sender,block);
        CREATE INDEX IF NOT EXISTS edges_recipient ON edges(recipient,block);
        CREATE INDEX IF NOT EXISTS edges_asset ON edges(asset,block);
        CREATE TABLE IF NOT EXISTS interactions(id TEXT PRIMARY KEY,block INTEGER NOT NULL,tx_hash TEXT NOT NULL,
          sender TEXT,recipient TEXT,kind TEXT NOT NULL,status INTEGER,source_sha256 TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS interactions_sender ON interactions(sender,block);
        CREATE INDEX IF NOT EXISTS interactions_recipient ON interactions(recipient,block);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,block INTEGER NOT NULL,tx_hash TEXT,
          kind TEXT NOT NULL,position TEXT NOT NULL,contract TEXT,topic0 TEXT,source_sha256 TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS records_tx_kind ON records(tx_hash,kind);
        CREATE TABLE IF NOT EXISTS anomalies(id TEXT PRIMARY KEY,block INTEGER NOT NULL,tx_hash TEXT,
          code TEXT NOT NULL,detail TEXT NOT NULL,source_sha256 TEXT NOT NULL);
        """)
        if transaction_occurrences:self.db.execute("CREATE INDEX IF NOT EXISTS transactions_hash_occurrence ON transactions(hash,block,tx_index)")
        identity = {"chain_id": self.chain_id, "initial_start": initial_start,
                    "previous_block_hash": previous_block_hash, "schema": 2 if transaction_occurrences else 1}
        existing = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
        if existing and json.loads(existing[0]) != identity:
            self.db.close()
            raise ValueError("graph identity mismatch")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (_json(identity),))
        self.identity = identity
        self._context: dict[str, Any] = {}

    def _addresses(self, *values: str | None):
        self.db.executemany("INSERT OR IGNORE INTO addresses VALUES(?,?)", ((a, self.chain_id) for a in values if a))

    def _anomaly(self, suffix: str, code: str, detail: str, tx_hash: str | None = None):
        c = self._context
        self.db.execute("INSERT INTO anomalies VALUES(?,?,?,?,?,?)", (
            f"{c['block_hash']}:{suffix}:{code}", c["block"], tx_hash, code, detail, c["sha"]))

    def _interaction(self, suffix: str, tx: dict, sender: str | None, recipient: str | None, kind: str, status: int | None):
        c = self._context
        self._addresses(sender, recipient)
        self.db.execute("INSERT INTO interactions VALUES(?,?,?,?,?,?,?,?)", (
            f"{c['block_hash']}:{suffix}", c["block"], tx["hash"], sender, recipient, kind, status, c["sha"]))

    def _edge(self, suffix: str, tx: dict, sender: str, recipient: str, amount: int, *,
              asset: str = "native", token_id: int | None = None, kind: str,
              log_index: int | None = None, trace_path: tuple[int, ...] | None = None,
              item_index: int | None = None, evidence: str):
        c = self._context
        self._addresses(sender, recipient)
        self.db.execute("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            f"{c['block_hash']}:{suffix}", self.chain_id, c["block"], tx["hash"], tx["index"], sender, recipient,
            asset, str(token_id) if token_id is not None else None, str(amount), kind,
            log_index, _json(trace_path) if trace_path is not None else None, item_index,
            evidence, c["sha"]))

    def _tx_for_record(self, record: dict, txs: dict[int, dict]) -> dict | None:
        index = record.get("transactionIndex")
        if index is None:
            return None
        index = _uint(index, "record transaction index", 31)
        tx = txs.get(index)
        if tx is None:
            raise ValueError("record references unknown transaction")
        supplied = record.get("transactionHash")
        if supplied is not None and _fixed(supplied, 32, "record transaction hash") != tx["hash"]:
            raise ValueError("record transaction hash mismatch")
        return tx

    def _log(self, record: dict, txs: dict[int, dict], seen: set[int]):
        tx = self._tx_for_record(record, txs)
        if tx is None:
            raise ValueError("log transaction missing")
        index = _uint(record.get("logIndex"), "log index", 31)
        if index in seen:
            raise ValueError("duplicate log index")
        seen.add(index)
        prefix = f"log:{index}"
        address = _address(record.get("address"))
        topics = record.get("topics", [])
        if not isinstance(topics, list):
            raise ValueError("invalid log topics")
        topics = [_fixed(t, 32, "event topic") for t in topics]
        c = self._context
        self.db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (
            f"{c['block_hash']}:{prefix}", c["block"], tx["hash"], "log", str(index), address,
            topics[0] if topics else None, c["sha"]))
        self._interaction(prefix, tx, None, address, "log_emitter", tx["status"])
        if tx["status"] != 1 or tx["conflict"]:
            if tx["status"] == 0:
                self._anomaly(prefix, "FAILED_TX_LOG", "A failed receipt contains a log; no transfer indexed", tx["hash"])
            return
        if record.get("removed") is True:
            self._anomaly(prefix, "REMOVED_LOG", "Removed source log excluded", tx["hash"])
            return
        if not topics or topics[0] not in (TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH):
            return
        try:
            data = _data(record.get("data"))
            if topics[0] == TRANSFER:
                if len(topics) == 3 and len(data) == 32:
                    sender, recipient = map(_topic_address, topics[1:3])
                    self._edge(prefix, tx, sender, recipient, int.from_bytes(data, "big"),
                               asset=address, kind="erc20_transfer", log_index=index, evidence="successful_receipt_event_shape")
                elif len(topics) == 4 and not data:
                    sender, recipient = map(_topic_address, topics[1:3])
                    self._edge(prefix, tx, sender, recipient, 1, asset=address,
                               token_id=int(topics[3], 16), kind="erc721_transfer", log_index=index,
                               evidence="successful_receipt_event_shape")
                else:
                    raise ValueError("ambiguous Transfer event shape")
            else:
                if len(topics) != 4:
                    raise ValueError("invalid ERC1155 topic count")
                _topic_address(topics[1])  # Operator is not necessarily owner.
                sender, recipient = map(_topic_address, topics[2:4])
                if topics[0] == TRANSFER_SINGLE:
                    if len(data) != 64:
                        raise ValueError("invalid ERC1155 single size")
                    pairs = [(int.from_bytes(data[:32], "big"), int.from_bytes(data[32:], "big"))]
                else:
                    pairs = _batch(data)
                for i, (token, amount) in enumerate(pairs):
                    self._edge(f"{prefix}:{i}", tx, sender, recipient, amount, asset=address,
                               token_id=token, kind="erc1155_transfer", log_index=index, item_index=i,
                               evidence="successful_receipt_event_shape")
        except (ValueError, TypeError) as exc:
            self._anomaly(prefix, "UNDECODED_TRANSFER_EVENT", str(exc), tx["hash"])

    def _block(self, block: dict, sha: str, expected: int, parent: str | None) -> str:
        header = block.get("header", block)
        number = _uint(header.get("number", header.get("height")), "block number", 63)
        if number != expected:
            raise ValueError("noncontiguous block number")
        block_hash = _fixed(header.get("hash"), 32, "block hash")
        parent_hash = _fixed(header.get("parentHash"), 32, "parent hash")
        if parent is not None and parent_hash != parent:
            raise ValueError("block parent hash mismatch")
        self._context = {"block": number, "block_hash": block_hash, "sha": sha}
        coverage = "classic_trace_coverage_unverified" if self.chain_id == 42161 and number < NITRO_FIRST_BLOCK else "trace_coverage_unverified"
        self.db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?)", (
            number, block_hash, parent_hash, sha, str(header.get("timestamp")) if header.get("timestamp") is not None else None, coverage))
        txs: dict[int, dict] = {}
        for raw in block.get("transactions", []):
            index = _uint(raw.get("transactionIndex"), "transaction index", 31)
            if index in txs:
                raise ValueError("duplicate transaction index")
            tx = {"hash": _fixed(raw.get("hash"), 32, "transaction hash"), "index": index,
                  "sender": _address(raw.get("from")),
                  "recipient": _address(raw["to"]) if raw.get("to") is not None else None,
                  "value": _uint(raw.get("value"), "transaction value"),
                  "status": _status(raw.get("status")), "conflict": bool(raw.get("sender_conflict", False)),
                  "created": _address(raw["contractAddress"]) if raw.get("contractAddress") is not None else None}
            txs[index] = tx
            if tx['conflict']:
                self._anomaly(f"tx:{index}", "SOURCE_SENDER_CONFLICT", "Source senders disagree; economic edges excluded", tx['hash'])
            if self.transaction_occurrences:
                prior=self.db.execute("SELECT block,tx_index,status FROM transactions WHERE hash=? ORDER BY block,tx_index LIMIT 1",(tx["hash"],)).fetchone()
                if prior is not None:
                    self._anomaly(f"tx:{index}","REPEATED_TRANSACTION_HASH",_json(dict(prior_block=prior[0],prior_index=prior[1],prior_status=prior[2],current_status=tx["status"],occurrences_preserved=True)),tx["hash"])
            self.db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?)", (
                tx["hash"], number, index, tx["sender"], tx["recipient"], tx["status"], str(tx["value"])))
            self._interaction(f"tx:{index}", tx, tx["sender"], tx["recipient"], "transaction", tx["status"])
            if tx["created"] is not None:
                self._interaction(f"tx_create:{index}", tx, tx["sender"], tx["created"], "receipt_created_contract", tx["status"])
            if tx["status"] is None:
                self._anomaly(f"tx:{index}", "RECEIPT_STATUS_UNKNOWN", "Financial edges excluded until receipt status is known", tx["hash"])
        if set(txs) != set(range(len(txs))):
            raise ValueError("transaction indexes are incomplete")
        traces = []
        paths: dict[int, set[tuple[int, ...]]] = {}
        failed: dict[int, set[tuple[int, ...]]] = {}
        for trace_number, trace in enumerate(block.get("traces", [])):
            tx = self._tx_for_record(trace, txs)
            if tx is None:
                # Reward traces have no transaction. Retain the raw source, never invent a payer.
                if trace.get("type") != "reward":
                    raise ValueError("non-reward trace transaction missing")
                suffix = f"reward:{trace_number}"
                self.db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (
                    f"{block_hash}:{suffix}", number, None, "trace", suffix, None, None, sha))
                self._anomaly(suffix, "UNINDEXED_REWARD_TRACE", "Non-transaction reward remains in source page")
                continue
            path = _path(trace)
            index = tx["index"]
            if path in paths.setdefault(index, set()):
                raise ValueError("duplicate trace path")
            paths[index].add(path)
            if _failed(trace):
                failed.setdefault(index, set()).add(path)
                if not path and tx["status"] == 1:
                    tx["conflict"] = True
                    self._anomaly(f"tx:{index}", "RECEIPT_TRACE_CONFLICT", "Successful receipt conflicts with failed root trace", tx["hash"])
            traces.append((trace, tx, path))
        for tx in txs.values():
            if tx["status"] == 1 and not tx["conflict"] and tx["recipient"] is not None and tx["value"]:
                self._edge(f"tx:{tx['index']}", tx, tx["sender"], tx["recipient"], tx["value"],
                           kind="native_transaction", evidence="successful_receipt_transaction_value")
            if tx["status"] == 1 and not tx["conflict"] and tx["recipient"] is None and tx["created"] is not None and tx["value"]:
                self._edge(f"tx_create:{tx['index']}", tx, tx["sender"], tx["created"], tx["value"],
                           kind="native_contract_creation", evidence="successful_receipt_created_contract")
        for trace, tx, path in traces:
            self._trace(trace, tx, path, paths[tx["index"]], failed.get(tx["index"], set()))
        seen_logs: set[int] = set()
        for log in block.get("logs", []):
            self._log(log, txs, seen_logs)
        return block_hash

    def _trace(self, trace: dict, tx: dict, path: tuple[int, ...], paths: set[tuple[int, ...]], failed: set[tuple[int, ...]]):
        action, result = trace.get("action") or {}, trace.get("result") or {}
        kind = trace.get("type")
        prefix = f"trace:{tx['index']}:{_json(path)}"
        c = self._context
        self.db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (
            f"{c['block_hash']}:{prefix}", c["block"], tx["hash"], "trace", _json(path), None, None, c["sha"]))
        reverted = any(path[:len(p)] == p for p in failed)
        missing_parent = any(path[:i] not in paths for i in range(len(path)))
        if missing_parent:
            self._anomaly(prefix, "TRACE_ANCESTOR_MISSING", "Value trace cannot establish enclosing call success", tx["hash"])
        status = 0 if reverted or tx["status"] == 0 else tx["status"]
        try:
            sender = recipient = None
            raw_amount = None
            if kind == "call":
                sender, recipient = _address(action.get("from")), _address(action.get("to"))
                raw_amount = action.get("value")
            elif kind == "create":
                sender = _address(action.get("from"))
                recipient = _address(result["address"]) if result.get("address") is not None else None
                raw_amount = action.get("value")
            elif kind in ("suicide", "selfdestruct"):
                sender, recipient = _address(action.get("address")), _address(action.get("refundAddress"))
                raw_amount = action.get("balance")
            else:
                self._anomaly(prefix, "UNCLASSIFIED_TRACE", str(kind), tx["hash"])
                return
            self._interaction(prefix, tx, sender, recipient, f"trace_{kind}", status)
            if kind == "call" and str(action.get("callType", "")).lower() in ("delegatecall", "callcode", "staticcall"):
                return
            amount = _uint(raw_amount, "trace value")
            if status != 1 or tx["conflict"] or missing_parent or recipient is None or not amount:
                return
            if kind == "call":
                call_type = str(action.get("callType", "")).lower()
                if call_type in ("delegatecall", "callcode", "staticcall"):
                    return
                if call_type != "call":
                    self._anomaly(prefix, "SYSTEM_TRANSFER_UNCLASSIFIED", f"callType={call_type}; value retained only in raw source", tx["hash"])
                    return
                if not path:
                    if sender != tx["sender"] or recipient != tx["recipient"] or amount != tx["value"]:
                        self._anomaly(prefix, "ROOT_TRANSFER_MISMATCH", "Root call disagrees with transaction value; not double-counted", tx["hash"])
                    return
            elif kind == "create" and not path and (tx["recipient"] is not None or sender != tx["sender"] or amount != tx["value"]):
                self._anomaly(prefix, "ROOT_CREATE_MISMATCH", "Root create disagrees with transaction; no value edge", tx["hash"])
                return
            if kind == "create" and not path and tx["created"] is not None:
                if recipient != tx["created"]:
                    self._anomaly(prefix, "ROOT_CREATE_RECEIPT_MISMATCH", "Root create disagrees with receipt contract; not double-counted", tx["hash"])
                return
            self._edge(prefix, tx, sender, recipient, amount, kind=f"native_{kind}", trace_path=path,
                       evidence="successful_receipt_and_trace_ancestry")
        except (ValueError, TypeError) as exc:
            self._anomaly(prefix, "MALFORMED_TRACE", str(exc), tx["hash"])

    def ingest(self, blocks: list[dict], source_sha256: str, start: int, end: int, cloud_ref: Any) -> dict:
        if not isinstance(source_sha256, str) or len(source_sha256) != 64 or not _HEX.fullmatch(source_sha256):
            raise ValueError("invalid source SHA256")
        source_sha256 = source_sha256.lower()
        start, end = _uint(start, "start", 63), _uint(end, "end", 63)
        if not isinstance(blocks, list) or start > end or len(blocks) != end - start + 1:
            raise ValueError("incomplete source page")
        if cloud_ref is None:
            raise ValueError("source cloud reference required")
        payload_hash = hashlib.sha256(_json(blocks).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT * FROM pages WHERE source_sha256=?", (source_sha256,)).fetchone()
            if old:
                if (old["start"], old["end"], old["payload_sha256"], old["cloud_ref"]) != (start, end, payload_hash, _json(cloud_ref)):
                    raise ValueError("source page provenance conflict")
                self.db.execute("COMMIT")
                return {"idempotent": True, **self.snapshot()}
            last = self.db.execute("SELECT number,hash FROM blocks ORDER BY number DESC LIMIT 1").fetchone()
            expected = last["number"] + 1 if last else self.identity["initial_start"]
            parent = last["hash"] if last else self.identity["previous_block_hash"]
            if start != expected:
                raise ValueError("source page gap or overlap")
            self.db.execute("INSERT INTO pages VALUES(?,?,?,?,?)", (source_sha256, payload_hash, start, end, _json(cloud_ref)))
            for number, block in enumerate(blocks, start):
                parent = self._block(block, source_sha256, number, parent)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return {"idempotent": False, **self.snapshot()}

    def snapshot(self) -> dict:
        counts = {table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("pages", "blocks", "transactions", "addresses", "edges", "interactions", "records", "anomalies")}
        first = self.db.execute("SELECT MIN(number) FROM blocks").fetchone()[0]
        last = self.db.execute("SELECT number,hash FROM blocks ORDER BY number DESC LIMIT 1").fetchone()
        kinds = {r[0]: r[1] for r in self.db.execute("SELECT kind,COUNT(*) FROM records GROUP BY kind")}
        codes = {r[0]: r[1] for r in self.db.execute("SELECT code,COUNT(*) FROM anomalies GROUP BY code")}
        return {"chain_id": self.chain_id, "initial_start": self.identity["initial_start"],
                "covered_first_block": first, "covered_last_block": last["number"] if last else None,
                "tip_hash": last["hash"] if last else self.identity["previous_block_hash"],
                "counts": counts, "log_count": kinds.get("log", 0), "trace_count": kinds.get("trace", 0),
                "receipt_status_unknown_count": codes.get("RECEIPT_STATUS_UNKNOWN", 0),
                "transactions_without_trace_rows": self.db.execute("SELECT COUNT(*) FROM transactions t WHERE NOT EXISTS (SELECT 1 FROM records r WHERE r.tx_hash=t.hash AND r.block=t.block AND r.kind='trace')").fetchone()[0],
                "anomaly_counts": codes,
                "whole_history_complete": False, "finality_verified": False,
                "trace_coverage": "unverified", "contract_event_semantics": "standard_event_shape_only",
                "same_owner_inferred": False, "cex_private_ledger_available": False}

    def wallet(self, address: str, limit: int = 100) -> dict:
        address = _address(address)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        edges = [dict(r) for r in self.db.execute(
            "SELECT * FROM edges WHERE sender=? OR recipient=? ORDER BY block DESC,id LIMIT ?",
            (address, address, limit))]
        interactions = [dict(r) for r in self.db.execute(
            "SELECT * FROM interactions WHERE sender=? OR recipient=? ORDER BY block DESC,id LIMIT ?",
            (address, address, limit))]
        return {"chain_id": self.chain_id, "address": address, "edges": edges, "interactions": interactions,
                "scope": "this_shard_only", "coverage": self.snapshot(), "owner_inferred": False,
                "balance_reconstructed": False, "limited": len(edges) == limit or len(interactions) == limit}

    def checkpoint(self):
        result = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]:
            raise RuntimeError("graph checkpoint busy")
        return self.snapshot()

    def close(self):
        self.db.close()
