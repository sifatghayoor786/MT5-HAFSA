"""Tick backtester: bid/ask fills, latency sim, pending triggers at touch,
stressed costs, and the look-ahead harness."""

from datetime import UTC, datetime, timedelta

import pytest

from aegis_velocity.backtest.tick_engine import (
    StressConfig,
    TickBacktester,
    run_strategy_over_ticks,
)
from aegis_velocity.core.events import Side, Signal
from aegis_velocity.mt5.protocol import Tick
from aegis_velocity.mt5.sim import default_spec

T0 = datetime(2026, 7, 14, 10, 0, 0, tzinfo=UTC)
SPEC = default_spec("EURUSD")


def _ticks(mids: list[float], spread_pts: float = 8.0, step_s: float = 1.0) -> list[Tick]:
    half = spread_pts * SPEC.point / 2
    out = []
    for i, mid in enumerate(mids):
        t = T0 + timedelta(seconds=i * step_s)
        out.append(Tick(time=t, bid=mid - half, ask=mid + half,
                        time_msc=int(t.timestamp() * 1000)))
    return out


def _signal(side: Side = Side.BUY, sl: int = 50, tp: int = 100, trigger: str = "tick_armed",
            pending_type: str = "", entry: float = 0.0, max_hold: int = 900) -> Signal:
    return Signal(
        strategy_id="F1", symbol="EURUSD", side=side, trigger=trigger,  # type: ignore[arg-type]
        pending_type=pending_type,  # type: ignore[arg-type]
        entry_price=entry, sl_points=sl, tp_points=tp, max_hold_s=max_hold,
    )


def test_market_entry_has_latency_and_no_lookahead() -> None:
    mids = [1.10000 + i * 0.00001 for i in range(30)]
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    decision_msc = int((T0 + timedelta(seconds=5)).timestamp() * 1000)
    fill = bt.run_signal(_signal(), decision_msc)
    assert fill is not None
    # entry strictly AFTER decision time + latency (100-400 ms => next 1s tick)
    assert fill.entry_time_msc > decision_msc
    assert 100.0 <= fill.latency_ms <= 400.0


def test_sl_executes_at_touch_with_costs() -> None:
    mids = [1.10000] * 3 + [1.09990, 1.09960, 1.09930]  # falls through SL
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    fill = bt.run_signal(_signal(sl=30, tp=300), int(T0.timestamp() * 1000))
    assert fill is not None
    assert fill.exit_reason == "SL"
    assert fill.gross_r == pytest.approx(-1.0, abs=0.01)  # exact stop at touch
    assert fill.net_r < fill.gross_r  # costs always make it worse


def test_tp_executes_at_touch() -> None:
    mids = [1.10000] * 3 + [1.10040, 1.10090, 1.10140]
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    fill = bt.run_signal(_signal(sl=50, tp=100), int(T0.timestamp() * 1000))
    assert fill is not None
    assert fill.exit_reason == "TP"
    assert fill.gross_r == pytest.approx(2.0, abs=0.05)


def test_time_stop_enforced() -> None:
    mids = [1.10000] * 100  # nothing moves
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    fill = bt.run_signal(_signal(sl=500, tp=500, max_hold=30), int(T0.timestamp() * 1000))
    assert fill is not None
    assert fill.exit_reason == "TIME_STOP"


def test_pending_stop_triggers_at_touch_without_latency() -> None:
    level = 1.10050
    mids = [1.10000, 1.10010, 1.10030, 1.10048, 1.10055, 1.10080, 1.10160]
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    signal = _signal(trigger="pending", pending_type="buy_stop", entry=level,
                     sl=50, tp=100)
    fill = bt.run_signal(signal, int(T0.timestamp() * 1000))
    assert fill is not None
    assert fill.entry_price == level  # server-side: filled AT the level
    assert fill.latency_ms == 0.0


def test_pending_never_triggered_returns_none() -> None:
    mids = [1.10000] * 20
    bt = TickBacktester(SPEC, _ticks(mids), commission_per_lot_per_side_points=3.0)
    signal = _signal(trigger="pending", pending_type="buy_stop", entry=1.10500)
    assert bt.run_signal(signal, int(T0.timestamp() * 1000)) is None


def test_stressed_costs_strictly_worse() -> None:
    mids = [1.10000] * 3 + [1.10040, 1.10090, 1.10140, 1.10200]
    base = TickBacktester(SPEC, _ticks(mids), 3.0)
    stressed = TickBacktester(SPEC, _ticks(mids), 3.0, stress=StressConfig.stressed())
    f_base = base.run_signal(_signal(sl=50, tp=100), int(T0.timestamp() * 1000))
    f_stress = stressed.run_signal(_signal(sl=50, tp=100), int(T0.timestamp() * 1000))
    assert f_base is not None and f_stress is not None
    assert f_stress.net_r <= f_base.net_r  # wider spread, worse fills


def test_bar_backtests_are_refused() -> None:
    with pytest.raises(ValueError, match="NON-EVIDENCE"):
        TickBacktester(SPEC, [], 3.0)


def test_walker_feeds_trailing_windows_only() -> None:
    """Look-ahead harness: the window handed to the strategy must end exactly at
    the decision tick — no future tick may be visible."""
    mids = [1.10000 + i * 0.00001 for i in range(200)]
    ticks = _ticks(mids)
    seen_windows: list[tuple[int, int]] = []

    def maker(window: tuple[Tick, ...]) -> list[Signal]:
        seen_windows.append((window[0].time_msc, window[-1].time_msc))
        return []

    run_strategy_over_ticks("F1", SPEC, ticks, maker, window=80, step=20)
    assert seen_windows
    all_msc = [t.time_msc for t in ticks]
    for first, last in seen_windows:
        idx = all_msc.index(last)
        assert all_msc[idx - 79] == first  # exactly the trailing 80 ticks
