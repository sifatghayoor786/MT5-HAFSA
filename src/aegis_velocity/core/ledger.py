"""Tamper-evident, hash-chained ledger (§3.6).

Every row: hash = sha256(prev_hash + canonical_json(row)). SQLite in WAL mode with
synchronous commits for order facts, plus an append-only JSON-lines journal that is
flushed+fsynced per fact — a crash between fill and analytics flush loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GENESIS_HASH = "0" * 64


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev_hash: str, ts_utc: str, kind: str, correlation_id: str, payload: str) -> str:
    material = prev_hash + canonical_json(
        {"ts_utc": ts_utc, "kind": kind, "correlation_id": correlation_id, "payload": payload}
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class LedgerRow:
    seq: int
    ts_utc: str
    kind: str
    correlation_id: str
    payload: dict[str, object]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows: int
    detail: str


class Ledger:
    def __init__(self, db_path: Path, journal_path: Path | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._journal_path = journal_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL UNIQUE
            )
            """
        )
        self._conn.commit()
        self._tip = self._load_tip()

    def _load_tip(self) -> str:
        cur = self._conn.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        return str(row[0]) if row else GENESIS_HASH

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        correlation_id: str = "",
    ) -> LedgerRow:
        """Synchronous, durable append. Returns the committed row."""
        ts = datetime.now(UTC).isoformat()
        payload_json = canonical_json(dict(payload))
        with self._lock:
            prev = self._tip
            digest = _row_hash(prev, ts, kind, correlation_id, payload_json)
            cur = self._conn.execute(
                "INSERT INTO ledger (ts_utc, kind, correlation_id, payload, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, kind, correlation_id, payload_json, prev, digest),
            )
            self._conn.commit()
            self._tip = digest
            seq = int(cur.lastrowid or 0)
            if self._journal_path is not None:
                line = canonical_json(
                    {
                        "seq": seq,
                        "ts_utc": ts,
                        "kind": kind,
                        "correlation_id": correlation_id,
                        "payload": payload_json,
                        "prev_hash": prev,
                        "hash": digest,
                    }
                )
                with open(self._journal_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        return LedgerRow(seq, ts, kind, correlation_id, dict(payload), prev, digest)

    def verify(self) -> VerifyResult:
        """Walk the whole chain re-computing every hash."""
        prev = GENESIS_HASH
        count = 0
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq, ts_utc, kind, correlation_id, payload, prev_hash, hash"
                " FROM ledger ORDER BY seq"
            )
            for seq, ts, kind, corr, payload, prev_hash, digest in cur:
                if prev_hash != prev:
                    return VerifyResult(False, count, f"row {seq}: prev_hash mismatch")
                expected = _row_hash(prev, ts, kind, corr, payload)
                if expected != digest:
                    return VerifyResult(False, count, f"row {seq}: hash mismatch (tampered)")
                prev = digest
                count += 1
        return VerifyResult(True, count, "chain verified")

    def rows(self, kind: str | None = None, limit: int = 1000) -> Iterator[LedgerRow]:
        with self._lock:
            if kind is None:
                cur = self._conn.execute(
                    "SELECT seq, ts_utc, kind, correlation_id, payload, prev_hash, hash"
                    " FROM ledger ORDER BY seq DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT seq, ts_utc, kind, correlation_id, payload, prev_hash, hash"
                    " FROM ledger WHERE kind = ? ORDER BY seq DESC LIMIT ?",
                    (kind, limit),
                )
            fetched = cur.fetchall()
        for seq, ts, k, corr, payload, prev_hash, digest in fetched:
            yield LedgerRow(seq, ts, k, corr, json.loads(payload), prev_hash, digest)

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared connection for sibling tables (idempotency, counters)."""
        return self._conn

    @property
    def db_lock(self) -> threading.Lock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()
