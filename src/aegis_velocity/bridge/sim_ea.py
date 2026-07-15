"""SimEa: CI stand-in for AegisFastGuard.mq5 over the REAL TCP bridge.

Mirrors the EA's bridge contract: HELLO on connect, 1 s heartbeats, resync ack,
PROTECT mode when Python heartbeats stop, records commands, never originates.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from aegis_velocity.bridge.protocol import (
    DEFAULT_GRACE_S,
    HEARTBEAT_INTERVAL_S,
    MsgType,
    decode_line,
    encode,
)


class SimEa:
    def __init__(
        self,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> None:
        self._interval = heartbeat_interval_s
        self._grace = grace_s
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.heartbeats_enabled = True
        self.protect_mode = False
        self.resync_count = 0
        self.received_commands: list[dict[str, Any]] = []
        self.received_oco_pairs: list[dict[str, Any]] = []
        self._last_python_heartbeat = time.monotonic()

    def connect(self, port: int, host: str = "127.0.0.1") -> None:
        self._sock = socket.create_connection((host, port), timeout=2.0)
        self._sock.settimeout(0.2)
        self._last_python_heartbeat = time.monotonic()
        self._send(MsgType.HELLO, {"ea": "SimEa", "magic_prefix": 77})
        for target, name in (
            (self._read_loop, "sim-ea-read"),
            (self._heartbeat_loop, "sim-ea-heartbeat"),
            (self._watchdog_loop, "sim-ea-watchdog"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=1.0)

    def _send(self, msg_type: MsgType, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._sock is None:
                return
            self._seq += 1
            try:
                self._sock.sendall(encode(msg_type, payload, self._seq))
            except OSError:
                pass

    def send_state(self, positions: int, pendings: int) -> None:
        self._send(MsgType.STATE, {"positions": positions, "pendings": pendings})

    def send_fill(self, ticket: int, comment: str) -> None:
        self._send(MsgType.FILL, {"ticket": ticket, "comment": comment})

    def _read_loop(self) -> None:
        assert self._sock is not None
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                msg = decode_line(line)
                if msg is None:
                    continue
                self._last_python_heartbeat = time.monotonic()
                if msg["type"] == MsgType.RESYNC.value:
                    self.resync_count += 1
                    self._send(MsgType.ACK, {"of": "resync"})
                    self.send_state(positions=0, pendings=0)
                elif msg["type"] == MsgType.COMMAND.value:
                    self.received_commands.append(dict(msg.get("data", {})))
                    self._send(MsgType.ACK, {"of": msg.get("data", {}).get("command", "")})
                elif msg["type"] == MsgType.OCO_PAIR.value:
                    self.received_oco_pairs.append(dict(msg.get("data", {})))

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if self.heartbeats_enabled:
                self._send(MsgType.HEARTBEAT)
            self._stop.wait(self._interval)

    def _watchdog_loop(self) -> None:
        """PROTECT: SL/TP and time-stops keep being enforced; nothing else changes."""
        while not self._stop.is_set():
            silent = time.monotonic() - self._last_python_heartbeat
            self.protect_mode = silent > self._grace
            self._stop.wait(self._interval / 2)
