"""Gateway: single-owner thread, P0 preemption, timeouts, tick pump, backoff."""

import time

from aegis_velocity.mt5.gateway import (
    P0_EMERGENCY,
    P3_DATA,
    GatewayTimeout,
    Mt5Gateway,
)
from aegis_velocity.mt5.protocol import Tick
from aegis_velocity.mt5.sim import SimMt5Client, default_spec


class InstrumentedSim(SimMt5Client):
    """Sim client with controllable slow operations for queue tests."""

    def slow_op(self, seconds: float) -> str:
        time.sleep(seconds)
        return "slow-done"

    def tagged_op(self, tag: str) -> str:
        time.sleep(0.01)
        return tag


def _gw() -> tuple[InstrumentedSim, Mt5Gateway]:
    sim = InstrumentedSim(specs={"EURUSD": default_spec("EURUSD")})
    sim.initialize("", sim.login, "pw", sim.server)
    sim.symbol_select("EURUSD", True)
    gw = Mt5Gateway(sim, call_timeout_s=5.0)
    return sim, gw


def test_calls_route_through_worker_and_return() -> None:
    sim, gw = _gw()
    gw.start()
    try:
        info = gw.call(2, "account_info")
        assert info is not None and info.login == sim.login
    finally:
        gw.stop()


def test_p0_jumps_queue_under_p3_load() -> None:
    _, gw = _gw()
    gw.start()
    try:
        import threading

        results: list[str] = []
        lock = threading.Lock()

        def submit(priority: int, tag: str) -> None:
            out = gw.call(priority, "tagged_op", tag, timeout_s=10.0)
            with lock:
                results.append(out)

        # occupy the worker so subsequent jobs queue up
        blocker = threading.Thread(target=submit, args=(P3_DATA, "blocker"))
        blocker.start()
        time.sleep(0.002)
        threads = [
            threading.Thread(target=submit, args=(P3_DATA, f"p3-{i}")) for i in range(8)
        ]
        for t in threads:
            t.start()
        time.sleep(0.005)  # let P3 jobs enqueue first
        p0 = threading.Thread(target=submit, args=(P0_EMERGENCY, "emergency"))
        p0.start()
        for t in [blocker, *threads, p0]:
            t.join()
        # emergency was enqueued LAST but must execute before most queued P3 work
        emergency_pos = results.index("emergency")
        assert emergency_pos <= 2, f"P0 executed too late: {results}"
    finally:
        gw.stop()


def test_call_timeout_raises() -> None:
    _, gw = _gw()
    gw.start()
    try:
        try:
            gw.call(P3_DATA, "slow_op", 1.0, timeout_s=0.05)
            raise AssertionError("expected GatewayTimeout")
        except GatewayTimeout:
            pass
    finally:
        gw.stop()


def test_tick_pump_feeds_subscribers_and_dedupes() -> None:
    sim, gw = _gw()
    seen: list[tuple[str, Tick]] = []
    gw.subscribe_ticks(lambda s, t: seen.append((s, t)))
    gw.start(pump_symbols=["EURUSD"])
    try:
        sim.push_tick("EURUSD", 1.1, 1.10010)
        time.sleep(0.15)
        first_count = len(seen)
        assert first_count >= 1
        time.sleep(0.15)  # no new tick pushed: dedupe means no new deliveries
        assert len(seen) == first_count
        sim.push_tick("EURUSD", 1.10005, 1.10015)
        time.sleep(0.15)
        assert len(seen) == first_count + 1
        assert gw.last_tick("EURUSD") is not None
    finally:
        gw.stop()


def test_metrics_and_backoff() -> None:
    _, gw = _gw()
    gw.start()
    try:
        for _ in range(5):
            gw.call(P3_DATA, "symbols_get_names")
        m = gw.metrics()
        assert m.calls_total >= 5
        assert m.latency_p50_ms >= 0.0
        gw.apply_backoff(0.2)
        t0 = time.monotonic()
        gw.call(P3_DATA, "symbols_get_names", timeout_s=2.0)
        assert time.monotonic() - t0 >= 0.15  # delayed by backoff
    finally:
        gw.stop()
