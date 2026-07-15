"""Arming token lifecycle (§13). HUMAN-ONLY issuance happens in the CLI;
this module owns validation and auto-invalidation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ARMING_PHRASE = "I_ACCEPT_LIVE_TRADING_RISK"


@dataclass(frozen=True)
class ArmingToken:
    account: int
    server: str
    mode: str
    issued_utc: str
    config_hash: str


@dataclass(frozen=True)
class ArmingStatus:
    armed: bool
    reason: str
    token: ArmingToken | None = None


def write_token(path: Path, account: int, server: str, mode: str, config_hash: str) -> ArmingToken:
    token = ArmingToken(
        account=account,
        server=server,
        mode=mode,
        issued_utc=datetime.now(UTC).isoformat(),
        config_hash=config_hash,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(token.__dict__, indent=1))
    tmp.replace(path)
    return token


def read_token(path: Path) -> ArmingToken | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
        return ArmingToken(
            account=int(raw["account"]),
            server=str(raw["server"]),
            mode=str(raw["mode"]),
            issued_utc=str(raw["issued_utc"]),
            config_hash=str(raw["config_hash"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def disarm(path: Path, reason: str) -> None:
    """Invalidate the token; an audit stub replaces it so re-arm is explicit."""
    if path.is_file():
        stub = {
            "disarmed": True,
            "reason": reason,
            "disarmed_utc": datetime.now(UTC).isoformat(),
        }
        path.write_text(json.dumps(stub, indent=1))


def validate_token(
    path: Path,
    account: int,
    server: str,
    config_hash: str,
    emergency_stopped: bool = False,
    hard_drawdown_halted: bool = False,
    bridge_lost_beyond_grace: bool = False,
) -> ArmingStatus:
    """Fail-closed validation; ANY mismatch invalidates the token on disk."""
    token = read_token(path)
    if token is None:
        return ArmingStatus(False, "no valid arming token")
    checks: list[tuple[bool, str]] = [
        (token.account != account, "account mismatch"),
        (token.server != server, "server mismatch"),
        (token.config_hash != config_hash, "config changed since arming"),
        (emergency_stopped, "emergency stop"),
        (hard_drawdown_halted, "hard drawdown halt"),
        (bridge_lost_beyond_grace, "EA bridge lost beyond grace"),
    ]
    for failed, reason in checks:
        if failed:
            disarm(path, reason)
            return ArmingStatus(False, f"token invalidated: {reason}")
    return ArmingStatus(True, "armed", token)
