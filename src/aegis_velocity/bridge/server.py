"""Python side of the EA bridge: localhost TCP server, single EA client.

Owns the SAFE-mode signal: `entries_allowed` is True only while the EA is
connected, heartbeating, and resynced. Everything is thread-based and
deterministic enough for CI against `SimEa`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from aegis_velocity.bridge.protocol import (
    DEFAULT_GRACE_S,
    HEARTBEAT_INTERVAL_S,
    MsgType,
    decode_line,
    encode,
)

log = logging.getLogger(__name__)


class BridgeState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    SYNCING = "SYNCING"  # connected, awaiting resync ack
    CONNECTED = "CONNECTED"
    LOST = "LOST"  # heartbeats missed beyond grace: Python is in SAFE mode


StateHandler = Callable[[BridgeState], None]
MessageHandler = Callable[[dict[str, Any]], None]


class EaBridge:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> None:
        self._host = host
        self._port = port
        self._interval = heartbeat_interval_s
        self._grace = grace_s
        self._server: socket.socket | None = None
        self._client: socket.socket | None = None
        self._client_lock = threading.Lock()
        self._stop = threading.Event()
        self._state = BridgeState.DISCONNECTED
        self._state_lock = threading.Lock()
        self._last_ea_heartbeat = 0.0
        self._seq = 0
        self._state_handlers: list[StateHandler] = []
        self._message_handlers: list[MessageHandler] = []
        self._last_ea_state: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []
        self.heartbeats_paused = False  # test hook: simulates Python-side stall

    # ------------------------------------------------------------- lifecycle

    def start(self) -> int:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self._host, self._port))
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._port = self._server.getsockname()[1]
        for target, name in (
            (self._accept_loop, "bridge-accept"),
            (self._heartbeat_loop, "bridge-heartbeat"),
            (self._watchdog_loop, "bridge-watchdog"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self._port

    def stop(self) -> None:
        self._stop.set()
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except OSError:
                    pass
                self._client = None
        if self._server is not None:
            self._server.close()
        for t in self._threads:
            t.join(timeout=1.0)

    # ------------------------------------------------------------- observers

    def on_state_change(self, handler: StateHandler) -> None:
        self._state_handlers.append(handler)

    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    @property
    def state(self) -> BridgeState:
        with self._state_lock:
            return self._state

    @property
    def entries_allowed(self) -> bool:
        """SAFE-mode signal: new entries only while the EA link is healthy."""
        return self.state is BridgeState.CONNECTED

    @property
    def last_ea_state(self) -> dict[str, Any]:
        return dict(self._last_ea_state)

    def _set_state(self, new: BridgeState) -> None:
        with self._state_lock:
            if self._state is new:
                return
            self._state = new
        log.info("bridge state -> %s", new.value)
        for handler in list(self._state_handlers):
            try:
                handler(new)
            except Exception:
                log.exception("bridge state handler failed")

    # ------------------------------------------------------------- send path

    def send(self, msg_type: MsgType, payload: dict[str, Any] | None = None) -> bool:
        with self._client_lock:
            client = self._client
            if client is None:
                return False
            self._seq += 1
            try:
                client.sendall(encode(msg_type, payload, self._seq))
                return True
            except OSError:
                return False

    def send_command(self, command: str, **kwargs: Any) -> bool:
        return self.send(MsgType.COMMAND, {"command": command, **kwargs})

    def send_oco_pair(self, ticket_a: int, ticket_b: int, group: str) -> bool:
        return self.send(
            MsgType.OCO_PAIR, {"ticket_a": ticket_a, "ticket_b": ticket_b, "group": group}
        )

    # ---------------------------------------------------------------- loops

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            log.info("EA connected from %s", addr)
            conn.settimeout(0.2)
            with self._client_lock:
                if self._client is not None:
                    try:
                        self._client.close()
                    except OSError:
                        pass
                self._client = conn
            self._last_ea_heartbeat = time.monotonic()
            self._set_state(BridgeState.SYNCING)
            self.send(MsgType.RESYNC, {"reason": "connect"})
            self._read_client(conn)

    def _read_client(self, conn: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle_line(line)
        with self._client_lock:
            if self._client is conn:
                self._client = None
        if not self._stop.is_set():
            self._set_state(BridgeState.LOST)

    def _handle_line(self, line: bytes) -> None:
        msg = decode_line(line)
        if msg is None:
            log.warning("malformed bridge message ignored (%d bytes)", len(line))
            return
        msg_type = msg.get("type")
        self._last_ea_heartbeat = time.monotonic()  # any valid traffic proves liveness
        if msg_type == MsgType.HEARTBEAT.value:
            pass
        elif msg_type == MsgType.HELLO.value:
            self._set_state(BridgeState.SYNCING)
            self.send(MsgType.RESYNC, {"reason": "hello"})
        elif msg_type == MsgType.ACK.value and msg.get("data", {}).get("of") == "resync":
            self._set_state(BridgeState.CONNECTED)
        elif msg_type == MsgType.STATE.value:
            self._last_ea_state = dict(msg.get("data", {}))
        for handler in list(self._message_handlers):
            try:
                handler(msg)
            except Exception:
                log.exception("bridge message handler failed")

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if not self.heartbeats_paused:
                self.send(MsgType.HEARTBEAT)
            self._stop.wait(self._interval)

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            state = self.state
            if state in (BridgeState.CONNECTED, BridgeState.SYNCING):
                silent_for = time.monotonic() - self._last_ea_heartbeat
                if silent_for > self._grace:
                    self._set_state(BridgeState.LOST)
            self._stop.wait(self._interval / 2)
