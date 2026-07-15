"""Per-symbol per-hour spread distributions for the liquidity window (§5).

Entries are allowed only when the current spread is at or below the configured
percentile of the same-hour distribution over the trailing window. No history
⇒ fail closed with an explicit reason — never guessed.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path

MIN_SAMPLES = 30


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile; values need not be sorted."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


class SpreadHistory:
    """Rolling (timestamp, spread_points) samples keyed by symbol x server-hour."""

    def __init__(self, persist_path: Path | None = None, history_days: int = 20) -> None:
        self._persist_path = persist_path
        self._history_days = history_days
        self._lock = threading.Lock()
        # symbol -> hour -> list[(epoch_s, spread_points)]
        self._data: dict[str, dict[int, list[tuple[float, float]]]] = {}
        if persist_path is not None and persist_path.is_file():
            try:
                raw = json.loads(persist_path.read_text())
                for symbol, hours in raw.items():
                    self._data[symbol] = {
                        int(h): [(float(t), float(s)) for t, s in samples]
                        for h, samples in hours.items()
                    }
            except (json.JSONDecodeError, ValueError, AttributeError):
                self._data = {}

    def record(self, symbol: str, server_time: datetime, spread_points: float) -> None:
        cutoff = (server_time - timedelta(days=self._history_days)).timestamp()
        with self._lock:
            bucket = self._data.setdefault(symbol, {}).setdefault(server_time.hour, [])
            bucket.append((server_time.timestamp(), spread_points))
            if len(bucket) > 50_000 or (bucket and bucket[0][0] < cutoff):
                bucket[:] = [x for x in bucket if x[0] >= cutoff]

    def spread_percentile(self, symbol: str, server_time: datetime, pct: float) -> float | None:
        """Percentile of the same-hour distribution; None when sample too thin."""
        cutoff = (server_time - timedelta(days=self._history_days)).timestamp()
        with self._lock:
            bucket = self._data.get(symbol, {}).get(server_time.hour, [])
            values = [s for t, s in bucket if t >= cutoff]
        if len(values) < MIN_SAMPLES:
            return None
        return percentile(values, pct)

    def save(self) -> None:
        if self._persist_path is None:
            return
        with self._lock:
            payload = {
                symbol: {str(h): samples for h, samples in hours.items()}
                for symbol, hours in self._data.items()
            }
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._persist_path)
