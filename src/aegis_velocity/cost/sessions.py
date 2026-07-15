"""Session/rollover/Friday gating in BROKER SERVER time (§5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aegis_velocity.core.config import SessionsConfig, SessionWindow


@dataclass(frozen=True)
class SessionVerdict:
    ok: bool
    reason: str  # "" when ok


def _in_window(minutes: int, window: SessionWindow) -> bool:
    start, end = window.start_minutes(), window.end_minutes()
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # crosses midnight


def check_session(cfg: SessionsConfig, server_now: datetime, scope: str = "default"
                  ) -> SessionVerdict:
    minutes = server_now.hour * 60 + server_now.minute

    if _in_window(minutes, cfg.rollover_blackout):
        return SessionVerdict(False, "ROLLOVER_BLACKOUT")

    if server_now.weekday() == 4:  # Friday
        h, m = cfg.friday_cutoff.split(":")
        if minutes >= int(h) * 60 + int(m):
            return SessionVerdict(False, "FRIDAY_CUTOFF")
    if server_now.weekday() >= 5:
        return SessionVerdict(False, "SESSION_BLOCKED")

    window_names = cfg.entry_windows.get(scope, cfg.entry_windows.get("default", []))
    for name in window_names:
        if _in_window(minutes, cfg.sessions[name]):
            return SessionVerdict(True, "")
    return SessionVerdict(False, "SESSION_BLOCKED")
