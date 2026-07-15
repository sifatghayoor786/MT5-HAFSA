"""MT5 gateway (§3.3): ONE thread owns every client call.

The MetaTrader5 package is not safely concurrent, so all calls flow through a
priority queue: P0 emergency/close/SL-safety, P1 order check/send/modify/cancel,
P2 positions/account, P3 ticks/rates/discovery. A tick pump enqueues P3 tick
reads at 20-50 ms per symbol and fans out to subscribers.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import statistics
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aegis_velocity.mt5.protocol import Mt5Client, Tick

log = logging.getLogger(__name__)

P0_EMERGENCY = 0
P1_ORDER = 1
P2_ACCOUNT = 2
P3_DATA = 3


class GatewayTimeout(Exception):
    pass


class GatewayClosed(Exception):
    pass


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    fn: Callable[[], object] = field(compare=False)
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    result: object = field(compare=False, default=None)
    error: BaseException | None = field(compare=False, default=None)
    queued_at: float = field(compare=False, default=0.0)
    started_at: float = field(compare=False, default=0.0)
    finished_at: float = field(compare=False, default=0.0)


@dataclass(frozen=True)
class GatewayMetrics:
    queue_depth: int
    calls_total: int
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p99_ms: float


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[idx]


TickHandler = Callable[[str, Tick], None]


class Mt5Gateway:
    def __init__(
        self,
        client: Mt5Client,
        tick_interval_s: float = 0.03,
        call_timeout_s: float = 5.0,
    ) -> None:
        self._client = client
        self._tick_interval_s = max(0.02, min(0.05, tick_interval_s))
        self._call_timeout_s = call_timeout_s
        self._heap: list[_Job] = []
        self._heap_lock = threading.Condition()
        self._seq = itertools.count()
        self._worker: threading.Thread | None = None
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self._pump_symbols: list[str] = []
        self._tick_handlers: list[TickHandler] = []
        self._latencies: deque[float] = deque(maxlen=4096)
        self._calls_total = 0
        self._exec_order: deque[tuple[int, int]] = deque(maxlen=1024)  # (priority, seq)
        self._last_ticks: dict[str, Tick] = {}
        self._backoff_until = 0.0
        self._tick_counts: dict[str, int] = defaultdict(int)

    # ----------------------------------------------------------- lifecycle

    def start(self, pump_symbols: list[str] | None = None) -> None:
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="mt5-gateway", daemon=True)
        self._worker.start()
        if pump_symbols:
            self._pump_symbols = list(pump_symbols)
            self._pump = threading.Thread(target=self._run_pump, name="tick-pump", daemon=True)
            self._pump.start()

    def stop(self) -> None:
        self._stop.set()
        with self._heap_lock:
            self._heap_lock.notify_all()
        for t in (self._pump, self._worker):
            if t is not None:
                t.join(timeout=2.0)

    # ----------------------------------------------------------- call path

    def call(
        self, priority: int, method: str, /, *args: Any, timeout_s: float | None = None, **kw: Any
    ) -> Any:
        """Execute `client.<method>(*args, **kw)` on the gateway thread."""
        if self._stop.is_set():
            raise GatewayClosed("gateway is stopped")
        bound = getattr(self._client, method)
        job = _Job(priority=priority, seq=next(self._seq), fn=lambda: bound(*args, **kw))
        job.queued_at = time.monotonic()
        with self._heap_lock:
            heapq.heappush(self._heap, job)
            self._heap_lock.notify()
        if not job.done.wait(timeout_s if timeout_s is not None else self._call_timeout_s):
            raise GatewayTimeout(f"{method} timed out (priority P{priority})")
        if job.error is not None:
            raise job.error
        return job.result

    def apply_backoff(self, seconds: float) -> None:
        """TOO_MANY_REQUESTS: delay all non-P0 work."""
        self._backoff_until = time.monotonic() + seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._heap_lock:
                while not self._heap and not self._stop.is_set():
                    self._heap_lock.wait(timeout=0.1)
                if self._stop.is_set():
                    break
                job = heapq.heappop(self._heap)
            now = time.monotonic()
            if job.priority > P0_EMERGENCY and now < self._backoff_until:
                delay = self._backoff_until - now
                time.sleep(min(delay, 0.05))
                with self._heap_lock:
                    heapq.heappush(self._heap, job)
                continue
            job.started_at = time.monotonic()
            try:
                job.result = job.fn()
            except BaseException as exc:  # propagate to caller, keep worker alive
                job.error = exc
            job.finished_at = time.monotonic()
            self._calls_total += 1
            self._latencies.append((job.finished_at - job.queued_at) * 1000.0)
            self._exec_order.append((job.priority, job.seq))
            job.done.set()
        # drain: fail everything still queued
        with self._heap_lock:
            for job in self._heap:
                job.error = GatewayClosed("gateway stopped")
                job.done.set()
            self._heap.clear()

    # ----------------------------------------------------------- tick pump

    def subscribe_ticks(self, handler: TickHandler) -> None:
        self._tick_handlers.append(handler)

    def _run_pump(self) -> None:
        while not self._stop.is_set():
            cycle_start = time.monotonic()
            for symbol in self._pump_symbols:
                if self._stop.is_set():
                    return
                try:
                    tick = self.call(P3_DATA, "symbol_info_tick", symbol, timeout_s=1.0)
                except (GatewayTimeout, GatewayClosed):
                    continue
                if isinstance(tick, Tick):
                    prev = self._last_ticks.get(symbol)
                    if prev is None or tick != prev:
                        self._last_ticks[symbol] = tick
                        self._tick_counts[symbol] += 1
                        for handler in list(self._tick_handlers):
                            try:
                                handler(symbol, tick)
                            except Exception:
                                log.exception("tick handler failed for %s", symbol)
            elapsed = time.monotonic() - cycle_start
            sleep_for = self._tick_interval_s - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def last_tick(self, symbol: str) -> Tick | None:
        return self._last_ticks.get(symbol)

    # ------------------------------------------------------------- metrics

    def metrics(self) -> GatewayMetrics:
        lat = sorted(self._latencies)
        with self._heap_lock:
            depth = len(self._heap)
        return GatewayMetrics(
            queue_depth=depth,
            calls_total=self._calls_total,
            latency_p50_ms=statistics.median(lat) if lat else 0.0,
            latency_p90_ms=_percentile(lat, 90),
            latency_p99_ms=_percentile(lat, 99),
        )

    @property
    def execution_order(self) -> list[tuple[int, int]]:
        return list(self._exec_order)
