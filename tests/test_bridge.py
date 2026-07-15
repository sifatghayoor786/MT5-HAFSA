"""Bridge: heartbeat loss => EA PROTECT + Python SAFE; resync; commands; robustness."""

import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aegis_velocity.bridge.protocol import MsgType, decode_line, encode
from aegis_velocity.bridge.server import BridgeState, EaBridge
from aegis_velocity.bridge.sim_ea import SimEa

FAST = 0.05  # accelerated heartbeat interval for tests
GRACE = 0.25


@pytest.fixture()
def bridge() -> Iterator[EaBridge]:
    b = EaBridge(port=0, heartbeat_interval_s=FAST, grace_s=GRACE)
    b.start()
    yield b
    b.stop()


def _ea(bridge: EaBridge) -> SimEa:
    ea = SimEa(heartbeat_interval_s=FAST, grace_s=GRACE)
    ea.connect(bridge._port)
    return ea


def _wait_for(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


def test_connect_resync_then_entries_allowed(bridge: EaBridge) -> None:
    assert not bridge.entries_allowed  # SAFE until EA is up
    ea = _ea(bridge)
    try:
        assert _wait_for(lambda: bridge.state is BridgeState.CONNECTED)
        assert bridge.entries_allowed
        assert ea.resync_count >= 1  # state resync is part of recovery, not optional
        assert _wait_for(lambda: bridge.last_ea_state.get("positions") == 0)
    finally:
        ea.close()


def test_ea_silence_puts_python_in_safe_mode(bridge: EaBridge) -> None:
    ea = _ea(bridge)
    try:
        assert _wait_for(lambda: bridge.entries_allowed)
        states: list[BridgeState] = []
        bridge.on_state_change(states.append)
        ea.heartbeats_enabled = False  # EA goes silent
        assert _wait_for(lambda: bridge.state is BridgeState.LOST)
        assert not bridge.entries_allowed  # SAFE: no new entries
        assert BridgeState.LOST in states
    finally:
        ea.close()


def test_recovery_requires_heartbeat_and_resync(bridge: EaBridge) -> None:
    ea = _ea(bridge)
    try:
        assert _wait_for(lambda: bridge.entries_allowed)
        ea.heartbeats_enabled = False
        assert _wait_for(lambda: bridge.state is BridgeState.LOST)
        before = ea.resync_count
        ea.heartbeats_enabled = True  # heartbeats resume
        # server watchdog re-enters SYNCING only via HELLO/reconnect; simulate EA re-hello
        ea._send(MsgType.HELLO, {"ea": "SimEa"})
        assert _wait_for(lambda: bridge.state is BridgeState.CONNECTED)
        assert ea.resync_count > before  # recovery included a state resync
    finally:
        ea.close()


def test_python_silence_puts_ea_in_protect(bridge: EaBridge) -> None:
    ea = _ea(bridge)
    try:
        assert _wait_for(lambda: bridge.entries_allowed)
        assert not ea.protect_mode
        bridge.heartbeats_paused = True  # Python control plane stalls
        assert _wait_for(lambda: ea.protect_mode)
        bridge.heartbeats_paused = False
        assert _wait_for(lambda: not ea.protect_mode)
    finally:
        ea.close()


def test_commands_and_oco_pairs_delivered(bridge: EaBridge) -> None:
    ea = _ea(bridge)
    try:
        assert _wait_for(lambda: bridge.entries_allowed)
        assert bridge.send_command("cancel_pending", ticket=4242)
        assert bridge.send_oco_pair(101, 102, group="straddle-1")
        assert _wait_for(lambda: len(ea.received_commands) == 1)
        assert ea.received_commands[0] == {"command": "cancel_pending", "ticket": 4242}
        assert _wait_for(lambda: len(ea.received_oco_pairs) == 1)
        assert ea.received_oco_pairs[0]["ticket_a"] == 101
    finally:
        ea.close()


def test_malformed_lines_never_crash_the_bridge(bridge: EaBridge) -> None:
    raw = socket.create_connection(("127.0.0.1", bridge._port), timeout=1.0)
    try:
        raw.sendall(b"this is not json\n")
        raw.sendall(b'{"v":99,"type":"heartbeat"}\n')  # wrong schema version
        raw.sendall(encode(MsgType.HELLO, {"ea": "raw"}))
        time.sleep(0.2)
        assert bridge.state in (BridgeState.SYNCING, BridgeState.CONNECTED, BridgeState.LOST)
    finally:
        raw.close()


def test_protocol_encode_decode_roundtrip() -> None:
    line = encode(MsgType.COMMAND, {"command": "flatten_all"}, seq=7)
    msg = decode_line(line.strip())
    assert msg is not None
    assert msg["type"] == "command"
    assert msg["seq"] == 7
    assert msg["data"]["command"] == "flatten_all"
    assert decode_line(b"garbage") is None
    assert decode_line(b'{"no":"type"}') is None


def test_ea_source_never_originates_trades() -> None:
    """Static invariant on the MQL5 source: the only trade calls are close/modify/delete.

    This cannot execute MQL5; it enforces that no order-opening API appears in the
    EA. Compile + behavioural verification on the Windows host is a USER-ACTION.
    """
    src = (Path(__file__).resolve().parents[1] / "mql5" / "AegisFastGuard.mq5").read_text()
    forbidden = [
        ".Buy(", ".Sell(", ".BuyStop(", ".SellStop(", ".BuyLimit(", ".SellLimit(",
        "OrderSend(", "OrderSendAsync(", "TRADE_ACTION_DEAL", "TRADE_ACTION_PENDING",
        ".PositionOpen(", ".OrderOpen(",
    ]
    for token in forbidden:
        assert token not in src, f"EA source contains trade-originating call: {token}"
    for token in (".PositionClose(", ".PositionModify(", ".OrderDelete("):
        assert token in src  # management duties are present
