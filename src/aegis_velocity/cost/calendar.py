"""News calendar (§5). Stale or missing calendar ⇒ news_status=UNKNOWN ⇒ entries
blocked in scalp mode — stricter than a swing desk, never merely penalised."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from aegis_velocity.core.config import NewsBlackoutCfg


class NewsStatus(StrEnum):
    OK = "OK"
    BLACKOUT = "BLACKOUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NewsEvent:
    utc_time: datetime
    currency: str
    impact: str  # high | medium | low
    title: str


@dataclass(frozen=True)
class NewsVerdict:
    status: NewsStatus
    detail: str


_KNOWN_CODES = frozenset(
    {"EUR", "GBP", "USD", "JPY", "CHF", "CAD", "AUD", "NZD", "XAU", "XAG"}
)


def symbol_currencies(symbol: str) -> tuple[str, ...]:
    """EURUSD -> (EUR, USD); frxGBPJPY -> (GBP, JPY). Unrecognised forms fail wide."""
    cleaned = re.sub(r"[^A-Z]", "", symbol.upper())
    for i in range(max(0, len(cleaned) - 5)):
        a, b = cleaned[i : i + 3], cleaned[i + 3 : i + 6]
        if a in _KNOWN_CODES and b in _KNOWN_CODES:
            return (a, b)
    if len(cleaned) >= 6:
        return (cleaned[:3], cleaned[3:6])
    return (cleaned,) if cleaned else ()


def import_calendar_csv(csv_path: Path, store_path: Path) -> int:
    """Validate + normalise a calendar CSV into the desk's JSON store."""
    events: list[dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            when = datetime.fromisoformat(row["utc_time"].replace("Z", "+00:00"))
            impact = row["impact"].strip().lower()
            if impact not in ("high", "medium", "low"):
                raise ValueError(f"invalid impact {row['impact']!r} in calendar")
            events.append(
                {
                    "utc_time": when.astimezone(UTC).isoformat(),
                    "currency": row["currency"].strip().upper(),
                    "impact": impact,
                    "title": row["title"].strip(),
                }
            )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"imported_utc": datetime.now(UTC).isoformat(), "events": events}
    tmp = store_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(store_path)
    return len(events)


class NewsCalendar:
    def __init__(self, store_path: Path, cfg: NewsBlackoutCfg) -> None:
        self._cfg = cfg
        self._events: list[NewsEvent] = []
        self._imported_utc: datetime | None = None
        if store_path.is_file():
            try:
                raw = json.loads(store_path.read_text())
                self._imported_utc = datetime.fromisoformat(raw["imported_utc"])
                self._events = [
                    NewsEvent(
                        utc_time=datetime.fromisoformat(e["utc_time"]),
                        currency=str(e["currency"]),
                        impact=str(e["impact"]),
                        title=str(e["title"]),
                    )
                    for e in raw["events"]
                ]
            except (json.JSONDecodeError, KeyError, ValueError):
                self._events = []
                self._imported_utc = None

    def status(self, symbol: str, now_utc: datetime) -> NewsVerdict:
        if self._imported_utc is None:
            return NewsVerdict(NewsStatus.UNKNOWN, "no calendar imported")
        age = now_utc - self._imported_utc
        if age > timedelta(hours=self._cfg.stale_calendar_hours):
            return NewsVerdict(
                NewsStatus.UNKNOWN,
                f"calendar stale: imported {age.total_seconds() / 3600:.0f}h ago "
                f"(max {self._cfg.stale_calendar_hours}h)",
            )
        currencies = set(symbol_currencies(symbol))
        for event in self._events:
            if event.currency not in currencies:
                continue
            if event.impact == "high":
                margin = timedelta(minutes=self._cfg.high_impact_minutes)
            elif event.impact == "medium":
                margin = timedelta(minutes=self._cfg.medium_impact_minutes)
            else:
                continue
            if event.utc_time - margin <= now_utc <= event.utc_time + margin:
                return NewsVerdict(
                    NewsStatus.BLACKOUT,
                    f"{event.impact} impact {event.currency} '{event.title}' "
                    f"at {event.utc_time.isoformat()}",
                )
        return NewsVerdict(NewsStatus.OK, "clear")
