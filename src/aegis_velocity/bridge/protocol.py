"""EA<->Python bridge wire protocol: schema-versioned JSON lines over localhost TCP.

Both sides heartbeat every second. Loss beyond grace: EA -> PROTECT (keeps
enforcing SL/TP/time-stops, cancels stale pendings, changes nothing else);
Python -> SAFE (no new entries). Recovery = heartbeat restore + state resync.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1
HEARTBEAT_INTERVAL_S = 1.0
DEFAULT_GRACE_S = 3.0


class MsgType(StrEnum):
    HELLO = "hello"
    HEARTBEAT = "heartbeat"
    STATE = "state"  # EA -> Python: managed positions/pendings snapshot
    POLICY = "policy"  # Python -> EA: per-position management policy
    OCO_PAIR = "oco_pair"  # Python -> EA: sibling tickets for cancel-on-fill
    COMMAND = "command"  # Python -> EA: cancel_pending | flatten_all | expire
    FILL = "fill"  # EA -> Python: OnTradeTransaction fill notice
    ACK = "ack"
    RESYNC_REQUEST = "resync_request"
    RESYNC = "resync"
    ERROR = "error"


def encode(msg_type: MsgType, payload: dict[str, Any] | None = None, seq: int = 0) -> bytes:
    body: dict[str, Any] = {"v": SCHEMA_VERSION, "type": msg_type.value, "seq": seq}
    if payload:
        body["data"] = payload
    return (json.dumps(body, separators=(",", ":")) + "\n").encode()


def decode_line(line: bytes | str) -> dict[str, Any] | None:
    """Best-effort decode; malformed input returns None and must never crash a peer."""
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or "type" not in raw or raw.get("v") != SCHEMA_VERSION:
        return None
    return raw
