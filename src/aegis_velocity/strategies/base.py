"""Strategy contract (§7).

Every strategy is a PURE DETERMINISTIC function of
(tick window + closed context bars + config) -> Signal | None.
Strategies never place orders, never read wall-clock time, never mutate state.
`trigger_id` is derived from the tick window (not the bar), so idempotency is
tick-window scoped. Signals embed version + config hash for parity checks.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aegis_velocity.core.config import StrategyCfg
from aegis_velocity.core.events import Side, Signal
from aegis_velocity.mt5.protocol import Bar, SymbolSpec, Tick


@dataclass(frozen=True)
class StrategyContext:
    strategy_id: str
    symbol: str
    spec: SymbolSpec
    ticks: tuple[Tick, ...]  # oldest..newest
    bars: tuple[Bar, ...]  # CLOSED bars only, oldest..newest
    server_now: datetime
    cfg: StrategyCfg

    def mid(self, tick: Tick) -> float:
        return (tick.bid + tick.ask) / 2.0

    def to_points(self, price_delta: float) -> float:
        return price_delta / self.spec.point

    @property
    def last_tick(self) -> Tick:
        return self.ticks[-1]

    @property
    def spread_points(self) -> float:
        return self.to_points(self.last_tick.ask - self.last_tick.bid)


def trigger_id_for(ctx: StrategyContext, tag: str = "") -> str:
    """Tick-window-scoped arming id: same window => same id => idempotent."""
    material = f"{ctx.strategy_id}|{ctx.symbol}|{ctx.last_tick.time_msc}|{tag}"
    return hashlib.sha1(material.encode()).hexdigest()[:12]


def make_signal(
    ctx: StrategyContext,
    side: Side,
    entry_price: float,
    sl_points: int,
    tp_points: int,
    reason: str,
    pending_type: str = "",
    oco_group: str = "",
    tag: str = "",
) -> Signal:
    return Signal(
        strategy_id=ctx.strategy_id,
        strategy_version=ctx.cfg.version,
        config_hash=ctx.cfg.config_hash(),
        symbol=ctx.symbol,
        side=side,
        trigger=ctx.cfg.trigger,
        trigger_id=trigger_id_for(ctx, tag),
        entry_price=entry_price,
        sl_points=sl_points,
        tp_points=tp_points,
        pending_type=pending_type,
        oco_group=oco_group,
        max_hold_s=ctx.cfg.max_hold_s,
        signal_time_utc=ctx.server_now,
        tick_time_utc=ctx.last_tick.time,
        reason=reason,
        correlation_id=trigger_id_for(ctx, tag),
    )


StrategyFn = Callable[[StrategyContext], list[Signal]]

# populated by each strategy module at import time (see strategies/__init__.py)
STRATEGY_REGISTRY: dict[str, StrategyFn] = {}


def register(strategy_id: str) -> Callable[[StrategyFn], StrategyFn]:
    def wrap(fn: StrategyFn) -> StrategyFn:
        STRATEGY_REGISTRY[strategy_id] = fn
        return fn

    return wrap


def param_int(cfg: StrategyCfg, key: str) -> int:
    value = cfg.params[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"param {key} must be numeric")
    return int(value)


def param_float(cfg: StrategyCfg, key: str) -> float:
    value = cfg.params[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"param {key} must be numeric")
    return float(value)


def window_stats(ctx: StrategyContext, n: int) -> tuple[float, float, float, float] | None:
    """(movement_pts, stdev_step_pts, ticks_per_min, extreme_adverse_pts) over last n ticks.

    extreme_adverse_pts: how far the window's opposite extreme sits from the last mid.
    Returns None when the window is too thin to measure.
    """
    if len(ctx.ticks) < n or n < 3:
        return None
    window = ctx.ticks[-n:]
    mids = [ctx.mid(t) for t in window]
    span_s = (window[-1].time - window[0].time).total_seconds()
    if span_s <= 0:
        return None
    movement_pts = ctx.to_points(mids[-1] - mids[0])
    steps = [ctx.to_points(b - a) for a, b in itertools.pairwise(mids)]
    mean = sum(steps) / len(steps)
    var = sum((s - mean) ** 2 for s in steps) / max(1, len(steps) - 1)
    stdev = var**0.5
    ticks_per_min = len(window) / span_s * 60.0
    if movement_pts >= 0:
        extreme_adverse = ctx.to_points(mids[-1] - min(mids))
    else:
        extreme_adverse = ctx.to_points(max(mids) - mids[-1])
    return movement_pts, stdev, ticks_per_min, extreme_adverse
