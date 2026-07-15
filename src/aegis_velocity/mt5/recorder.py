"""Tick recorder: JSONL per symbol per day. Feeds the tick backtester and parity
golden files. Runs from Stage 3 onward wherever a tick source exists."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TextIO

from aegis_velocity.mt5.protocol import Tick


class TickRecorder:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._files: dict[str, TextIO] = {}
        self._counts: dict[str, int] = {}

    def on_tick(self, symbol: str, tick: Tick) -> None:
        day = tick.time.strftime("%Y%m%d")
        key = f"{symbol}:{day}"
        with self._lock:
            fh = self._files.get(key)
            if fh is None:
                path = self._root / symbol / f"{day}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(path, "a", encoding="utf-8")
                self._files[key] = fh
            fh.write(
                json.dumps(
                    {
                        "t": tick.time.isoformat(),
                        "bid": tick.bid,
                        "ask": tick.ask,
                        "last": tick.last,
                        "msc": tick.time_msc,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            self._counts[symbol] = self._counts.get(symbol, 0) + 1

    def flush(self) -> None:
        with self._lock:
            for fh in self._files.values():
                fh.flush()

    def close(self) -> None:
        with self._lock:
            for fh in self._files.values():
                fh.flush()
                fh.close()
            self._files.clear()

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def load_recorded_ticks(root: Path, symbol: str) -> list[Tick]:
    """Load all recorded ticks for a symbol, oldest day first."""
    from datetime import datetime

    out: list[Tick] = []
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return out
    for path in sorted(symbol_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            out.append(
                Tick(
                    time=datetime.fromisoformat(raw["t"]),
                    bid=float(raw["bid"]),
                    ask=float(raw["ask"]),
                    last=float(raw.get("last", 0.0)),
                    time_msc=int(raw.get("msc", 0)),
                )
            )
    return out
