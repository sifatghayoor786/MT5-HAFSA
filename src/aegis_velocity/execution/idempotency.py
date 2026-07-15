"""Idempotency (§3.6): duplicates are impossible by construction.

key = sha1(account|broker_symbol|strategy_id|trigger_id|direction)[:12], UNIQUE
in SQLite; one in-flight intent per symbol; magic 77_SSS_VV; comment AEG|{key}.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from aegis_velocity.core.state import OrderState

IN_FLIGHT_STATES = frozenset(
    {
        OrderState.ARMED.value,
        OrderState.CHECKED.value,
        OrderState.SUBMITTED.value,
        OrderState.PLACED.value,
        OrderState.UNKNOWN_OUTCOME.value,
    }
)


def idem_key(
    account: int, broker_symbol: str, strategy_id: str, trigger_id: str, direction: str
) -> str:
    material = f"{account}|{broker_symbol}|{strategy_id}|{trigger_id}|{direction}"
    return hashlib.sha1(material.encode()).hexdigest()[:12]


def magic_for(strategy_id: str, version: int, prefix: int = 77) -> int:
    """77_SSS_VV: strategy ordinal + version."""
    ordinal = int(strategy_id.lstrip("F") or "0")
    return prefix * 100_000 + ordinal * 100 + version


def comment_for(key: str) -> str:
    return f"AEG|{key}"


def key_from_comment(comment: str) -> str | None:
    if comment.startswith("AEG|") and len(comment) >= 5:
        return comment[4:16]
    return None


@dataclass(frozen=True)
class IntentRow:
    key: str
    symbol: str
    strategy_id: str
    direction: str
    state: str
    ticket: int
    volume: float
    created_utc: str


class IntentStore:
    """SQLite-backed intent registry sharing the ledger's database file."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    state TEXT NOT NULL,
                    ticket INTEGER NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    oco_group TEXT NOT NULL DEFAULT '',
                    created_utc TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def try_claim(
        self, key: str, symbol: str, strategy_id: str, direction: str, oco_group: str = ""
    ) -> bool:
        """Atomically claim a key. False = duplicate OR symbol already has an
        in-flight intent (one in-flight per symbol, hard rule). Exception: the
        two legs of one OCO straddle share a non-empty oco_group and count as
        ONE logical intent."""
        with self._lock:
            placeholders = ",".join("?" for _ in IN_FLIGHT_STATES)
            cur = self._conn.execute(
                "SELECT oco_group FROM intents WHERE symbol = ? AND state IN"
                f" ({placeholders})",
                (symbol, *IN_FLIGHT_STATES),
            )
            in_flight_groups = [str(row[0]) for row in cur.fetchall()]
            blocking = [
                g for g in in_flight_groups if not (oco_group and g == oco_group)
            ]
            if blocking:
                return False
            try:
                self._conn.execute(
                    "INSERT INTO intents (key, symbol, strategy_id, direction, state,"
                    " oco_group, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        symbol,
                        strategy_id,
                        direction,
                        OrderState.ARMED.value,
                        oco_group,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def set_state(self, key: str, state: OrderState, ticket: int = 0, volume: float = 0.0
                  ) -> None:
        with self._lock:
            if ticket or volume:
                self._conn.execute(
                    "UPDATE intents SET state = ?, ticket = ?, volume = ? WHERE key = ?",
                    (state.value, ticket, volume, key),
                )
            else:
                self._conn.execute(
                    "UPDATE intents SET state = ? WHERE key = ?", (state.value, key)
                )
            self._conn.commit()

    def get(self, key: str) -> IntentRow | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, symbol, strategy_id, direction, state, ticket, volume,"
                " created_utc FROM intents WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
        return IntentRow(*row) if row else None

    def in_state(self, *states: OrderState) -> list[IntentRow]:
        with self._lock:
            placeholders = ",".join("?" for _ in states)
            cur = self._conn.execute(
                "SELECT key, symbol, strategy_id, direction, state, ticket, volume,"
                f" created_utc FROM intents WHERE state IN ({placeholders})",
                tuple(s.value for s in states),
            )
            return [IntentRow(*row) for row in cur.fetchall()]

    def all_keys(self) -> set[str]:
        with self._lock:
            cur = self._conn.execute("SELECT key FROM intents")
            return {row[0] for row in cur.fetchall()}
