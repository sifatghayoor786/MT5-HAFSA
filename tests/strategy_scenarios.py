"""Deterministic synthetic scenarios that make each F-strategy fire.

Shared by the unit tests and the golden-file parity harness. Everything is
fixed-seed and wall-clock free: identical inputs on every run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.core.config import load_desk_config
from aegis_velocity.mt5.protocol import Bar, Tick
from aegis_velocity.mt5.sim import default_spec
from aegis_velocity.strategies.base import StrategyContext

REPO = Path(__file__).resolve().parents[1]
CFG = load_desk_config(REPO, env={})
T0 = datetime(2026, 7, 14, 10, 30, 0, tzinfo=UTC)
SPREAD = 0.00008  # 8 points


def _tick(i: int, mid: float, spread: float = SPREAD) -> Tick:
    t = T0 + timedelta(seconds=i)
    return Tick(
        time=t, bid=round(mid - spread / 2, 6), ask=round(mid + spread / 2, 6),
        last=round(mid, 6), time_msc=int(t.timestamp() * 1000),
    )


def _bar(i: int, low: float, high: float, tf_s: int = 300) -> Bar:
    open_time = T0 - timedelta(seconds=tf_s * (60 - i))
    return Bar(time=open_time, open=low, high=high, low=low, close=high)


def _ctx(strategy_id: str, ticks: list[Tick], bars: list[Bar]) -> StrategyContext:
    return StrategyContext(
        strategy_id=strategy_id,
        symbol="EURUSD",
        spec=default_spec("EURUSD"),
        ticks=tuple(ticks),
        bars=tuple(bars),
        server_now=ticks[-1].time if ticks else T0,
        cfg=CFG.strategies.strategies[strategy_id],
    )


def f1_burst_context() -> StrategyContext:
    """Steady up-burst: alternating +1.2/+0.8pt steps, 1 tick/s, constant spread."""
    mids = [1.10000]
    for i in range(70):
        mids.append(mids[-1] + (0.000012 if i % 2 == 0 else 0.000008))
    ticks = [_tick(i, m) for i, m in enumerate(mids)]
    return _ctx("F1", ticks, [])


def f1_quiet_context() -> StrategyContext:
    """No burst: flat mids."""
    ticks = [_tick(i, 1.10000) for i in range(70)]
    return _ctx("F1", ticks, [])


def f2_sweep_context() -> StrategyContext:
    """Swing low 1.09900 swept to 1.09880 (20pt >= 15), then 10-tick reclaim."""
    bars = [_bar(i, 1.09900 + i * 0.00001, 1.10100) for i in range(24)]
    mids = [1.09950, 1.09930, 1.09910, 1.09885, 1.09880]  # the sweep
    mids += [1.09905 + i * 0.000005 for i in range(12)]  # reclaim above 1.09900
    ticks = [_tick(i, m) for i, m in enumerate(mids)]
    return _ctx("F2", ticks, bars)


def f3_open_context() -> StrategyContext:
    """Pre-open range 1.10000-1.10040; price opens above midpoint => buy stop."""
    bars = [_bar(i, 1.10000, 1.10040, tf_s=900) for i in range(8)]
    ticks = [_tick(i, 1.10030) for i in range(5)]
    return _ctx("F3", ticks, bars)


def f4_compression_context() -> StrategyContext:
    """12-bar box 30pt wide (max 40) => OCO straddle."""
    bars = [_bar(i, 1.10000, 1.10030) for i in range(12)]
    ticks = [_tick(i, 1.10015) for i in range(5)]
    return _ctx("F4", ticks, bars)


def f4_wide_box_context() -> StrategyContext:
    bars = [_bar(i, 1.10000, 1.10080) for i in range(12)]  # 80pt: too wide
    ticks = [_tick(i, 1.10040) for i in range(5)]
    return _ctx("F4", ticks, bars)


def f5_stoprun_context() -> StrategyContext:
    """Run 25pt through the 48-bar high 1.10100, then 12 rejection ticks below."""
    bars = [_bar(i, 1.09900, 1.10100) for i in range(48)]
    mids = [1.10080, 1.10100, 1.10118, 1.10125]  # the run
    mids += [1.10092 - i * 0.000004 for i in range(14)]  # rejection back below
    ticks = [_tick(i, m) for i, m in enumerate(mids)]
    return _ctx("F5", ticks, bars)


SCENARIOS: dict[str, StrategyContext] = {}


def scenarios() -> dict[str, StrategyContext]:
    return {
        "F1": f1_burst_context(),
        "F2": f2_sweep_context(),
        "F3": f3_open_context(),
        "F4": f4_compression_context(),
        "F5": f5_stoprun_context(),
    }
