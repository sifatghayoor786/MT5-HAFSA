"""Persisted equity anchors and desk counters — all halts are restart-safe (§6)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


class PersistedState:
    """Small atomic JSON key-value store (write-temp + rename)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    self._data = loaded
            except json.JSONDecodeError:
                # corrupt state is treated as absent; halts re-derive conservatively
                self._data = {}

    def get(self, key: str, default: object = None) -> object:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def update(self, values: dict[str, object]) -> None:
        with self._lock:
            self._data.update(values)
            self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True, default=str))
        tmp.replace(self._path)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class DrawdownStatus:
    daily_loss_frac: float
    weekly_loss_frac: float
    peak_drawdown_frac: float


class EquityAnchors:
    """Day/week equity anchors at server-midnight / week-start, plus all-time peak."""

    def __init__(self, store: PersistedState) -> None:
        self._store = store

    def roll(self, server_now: datetime, equity: float) -> None:
        """Advance anchors when a new server day/week begins; track peak."""
        today = server_now.date().isoformat()
        wstart = week_start(server_now.date()).isoformat()
        if self._store.get("day_date") != today:
            self._store.update({"day_date": today, "day_anchor": equity})
        if self._store.get("week_start") != wstart:
            self._store.update({"week_start": wstart, "week_anchor": equity})
        peak = self._store.get("equity_peak")
        if not isinstance(peak, int | float) or equity > float(peak):
            self._store.set("equity_peak", equity)

    def _anchor(self, key: str, fallback: float) -> float:
        value = self._store.get(key)
        return float(value) if isinstance(value, int | float) and float(value) > 0 else fallback

    def status(self, equity: float) -> DrawdownStatus:
        day_anchor = self._anchor("day_anchor", equity)
        week_anchor = self._anchor("week_anchor", equity)
        peak = self._anchor("equity_peak", equity)
        return DrawdownStatus(
            daily_loss_frac=max(0.0, (day_anchor - equity) / day_anchor),
            weekly_loss_frac=max(0.0, (week_anchor - equity) / week_anchor),
            peak_drawdown_frac=max(0.0, (peak - equity) / peak),
        )

    @property
    def initialized(self) -> bool:
        return self._store.get("day_date") is not None


class PersistedCounters:
    """Restart-safe rolling counters (hourly/daily trades, consecutive losses)."""

    def __init__(self, store: PersistedState) -> None:
        self._store = store
        self._lock = threading.Lock()

    def _bucket(self, name: str) -> list[float]:
        raw = self._store.get(name, [])
        return [float(x) for x in raw] if isinstance(raw, list) else []

    def record_event(self, name: str, ts: datetime) -> None:
        with self._lock:
            events = self._bucket(name)
            events.append(ts.timestamp())
            self._store.set(name, events[-2000:])

    def count_within(self, name: str, ts: datetime, window_s: float) -> int:
        cutoff = ts.timestamp() - window_s
        return sum(1 for t in self._bucket(name) if t >= cutoff)

    def get_int(self, name: str, default: int = 0) -> int:
        value = self._store.get(name, default)
        return int(value) if isinstance(value, int | float) else default

    def set_int(self, name: str, value: int) -> None:
        self._store.set(name, value)

    def get_str(self, name: str) -> str:
        value = self._store.get(name, "")
        return str(value) if value is not None else ""

    def set_str(self, name: str, value: str) -> None:
        self._store.set(name, value)

    def get_float(self, name: str, default: float = 0.0) -> float:
        value = self._store.get(name, default)
        return float(value) if isinstance(value, int | float) else default

    def set_float(self, name: str, value: float) -> None:
        self._store.set(name, value)


def server_midnight_utc(server_now: datetime) -> datetime:
    return server_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
