"""Broker clock protocol (§3.6).

Empirical broker-UTC offset (median of tick.time - local_utc), persisted hourly;
DST-jump alert; closed-bar rule; tick-staleness rule; local-drift halt trigger.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TICK_MAX_AGE_S = 0.5
BAR_CLOSE_GRACE_S = 2.0
LOCAL_DRIFT_HALT_S = 30.0
DST_JUMP_ALERT_S = 1800.0
_MAX_SAMPLES = 512


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ClockStatus:
    offset_s: float
    samples: int
    dst_alert: bool
    drift_halt: bool


@dataclass
class BrokerClock:
    """Tracks the broker-server clock relative to local UTC."""

    persist_path: Path | None = None
    _samples: list[float] = field(default_factory=list)
    _persisted_offset: float | None = None
    _dst_alert: bool = False
    _drift_halt: bool = False

    def __post_init__(self) -> None:
        if self.persist_path is not None and self.persist_path.is_file():
            try:
                data = json.loads(self.persist_path.read_text())
                self._persisted_offset = float(data["offset_s"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._persisted_offset = None

    def observe_tick(self, tick_time_utc: datetime, local_utc: datetime | None = None) -> None:
        local = local_utc if local_utc is not None else utc_now()
        offset = (tick_time_utc - local).total_seconds()
        self._samples.append(offset)
        if len(self._samples) > _MAX_SAMPLES:
            del self._samples[: len(self._samples) - _MAX_SAMPLES]
        if self._persisted_offset is not None:
            delta = abs(self.offset_s - self._persisted_offset)
            if delta > DST_JUMP_ALERT_S:
                self._dst_alert = True
            if delta > LOCAL_DRIFT_HALT_S and len(self._samples) >= 20:
                # a sustained jump beyond 30 s vs the persisted baseline means either
                # DST re-anchor (alert) or a broken local clock: fail closed either way
                self._drift_halt = delta <= DST_JUMP_ALERT_S

    @property
    def offset_s(self) -> float:
        if self._samples:
            return statistics.median(self._samples)
        if self._persisted_offset is not None:
            return self._persisted_offset
        return 0.0

    @property
    def calibrated(self) -> bool:
        return bool(self._samples) or self._persisted_offset is not None

    def persist(self) -> None:
        if self.persist_path is None:
            return
        payload = {"offset_s": self.offset_s, "saved_utc": utc_now().isoformat()}
        tmp = self.persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.persist_path)
        self._persisted_offset = self.offset_s

    def server_now(self, local_utc: datetime | None = None) -> datetime:
        local = local_utc if local_utc is not None else utc_now()
        return datetime.fromtimestamp(local.timestamp() + self.offset_s, tz=UTC)

    def status(self) -> ClockStatus:
        return ClockStatus(
            offset_s=self.offset_s,
            samples=len(self._samples),
            dst_alert=self._dst_alert,
            drift_halt=self._drift_halt,
        )

    def tick_is_fresh(
        self,
        tick_time_utc: datetime,
        local_utc: datetime | None = None,
        max_age_s: float = TICK_MAX_AGE_S,
    ) -> bool:
        """Entries require tick age <= 500 ms measured on the SERVER clock."""
        age = (self.server_now(local_utc) - tick_time_utc).total_seconds()
        return -max_age_s <= age <= max_age_s

    def bar_is_closed(
        self,
        bar_open_server: datetime,
        timeframe_s: int,
        newer_bar_exists: bool,
        local_utc: datetime | None = None,
    ) -> bool:
        """Context TFs use closed bars only: server_now >= open+tf+2s AND a newer bar exists."""
        if not newer_bar_exists:
            return False
        close_time = bar_open_server.timestamp() + timeframe_s + BAR_CLOSE_GRACE_S
        return self.server_now(local_utc).timestamp() >= close_time
