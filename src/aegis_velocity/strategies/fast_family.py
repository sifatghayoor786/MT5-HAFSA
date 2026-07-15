"""F1-F5 fast-family strategies (§7). Pure functions; F6 is scaffolded OFF.

Each returns a (possibly empty) list of Signals. Deterministic: identical
context in => bit-identical signals out (parity harness enforces this).
"""

from __future__ import annotations

from aegis_velocity.core.events import Side, Signal
from aegis_velocity.strategies.base import (
    StrategyContext,
    make_signal,
    param_float,
    param_int,
    register,
    window_stats,
)


@register("F1")
def f1_momentum_burst(ctx: StrategyContext) -> list[Signal]:
    """Spread-normalised momentum burst: directional tick-burst z-score with the
    spread near its window low; armed-intent market entry; tight time-stop."""
    n = param_int(ctx.cfg, "window_ticks")
    stats = window_stats(ctx, n)
    if stats is None:
        return []
    movement_pts, stdev, ticks_per_min, extreme_adverse = stats
    if ticks_per_min < param_int(ctx.cfg, "min_tick_rate"):
        return []
    if stdev <= 0:
        return []
    z = movement_pts / (stdev * (n**0.5))
    if abs(z) < param_float(ctx.cfg, "burst_z"):
        return []
    # spread must sit at the low end of the window's own spread distribution
    spreads = sorted(ctx.to_points(t.ask - t.bid) for t in ctx.ticks[-n:])
    low_band = spreads[max(0, int(len(spreads) * 0.3) - 1)]
    if ctx.spread_points > low_band + 1e-6:  # epsilon: float noise must not flip gates
        return []
    side = Side.BUY if z > 0 else Side.SELL
    sl_pts = int(max(2 * ctx.spread_points, extreme_adverse + ctx.spread_points, 10))
    tp_pts = 2 * sl_pts
    entry = ctx.last_tick.ask if side is Side.BUY else ctx.last_tick.bid
    return [
        make_signal(
            ctx, side, entry, sl_pts, tp_pts,
            reason=f"burst z={z:.2f} rate={ticks_per_min:.0f}/min",
        )
    ]


@register("F2")
def f2_sweep_snapback(ctx: StrategyContext) -> list[Signal]:
    """Micro liquidity sweep: wick beyond a confirmed bar swing, then a tick-level
    reclaim. SL beyond the sweep extreme."""
    lookback = param_int(ctx.cfg, "swing_lookback_bars")
    sweep_min = param_int(ctx.cfg, "sweep_min_points")
    reclaim_n = param_int(ctx.cfg, "reclaim_ticks")
    if len(ctx.bars) < lookback or len(ctx.ticks) < reclaim_n + 5:
        return []
    swing_bars = ctx.bars[-lookback:]
    swing_low = min(b.low for b in swing_bars)
    swing_high = max(b.high for b in swing_bars)
    mids = [ctx.mid(t) for t in ctx.ticks]
    window_low = min(mids)
    window_high = max(mids)
    reclaim = mids[-reclaim_n:]

    # sweep below swing low, then all reclaim ticks back above the level
    if (
        ctx.to_points(swing_low - window_low) >= sweep_min
        and all(m > swing_low for m in reclaim)
    ):
        sl_pts = int(ctx.to_points(ctx.last_tick.bid - window_low) + ctx.spread_points) + 1
        tp_pts = 2 * sl_pts
        return [
            make_signal(
                ctx, Side.BUY, ctx.last_tick.ask, sl_pts, tp_pts,
                reason=f"sweep of {swing_low} reclaimed", tag="low",
            )
        ]
    if (
        ctx.to_points(window_high - swing_high) >= sweep_min
        and all(m < swing_high for m in reclaim)
    ):
        sl_pts = int(ctx.to_points(window_high - ctx.last_tick.ask) + ctx.spread_points) + 1
        tp_pts = 2 * sl_pts
        return [
            make_signal(
                ctx, Side.SELL, ctx.last_tick.bid, sl_pts, tp_pts,
                reason=f"sweep of {swing_high} reclaimed", tag="high",
            )
        ]
    return []


@register("F3")
def f3_session_open_expansion(ctx: StrategyContext) -> list[Signal]:
    """Session-open expansion: pre-placed server-side stop order beyond the
    pre-open range, biased by where price opens relative to the range midpoint."""
    pre_n = param_int(ctx.cfg, "pre_window_bars")
    offset = param_int(ctx.cfg, "level_offset_points")
    if len(ctx.bars) < pre_n or not ctx.ticks:
        return []
    pre = ctx.bars[-pre_n:]
    range_high = max(b.high for b in pre)
    range_low = min(b.low for b in pre)
    midpoint = (range_high + range_low) / 2.0
    last_mid = ctx.mid(ctx.last_tick)
    if last_mid >= midpoint:
        level = range_high + offset * ctx.spec.point
        sl_pts = int(ctx.to_points(level - range_low))
        return [
            make_signal(
                ctx, Side.BUY, ctx.spec.round_price(level), sl_pts, 2 * sl_pts,
                reason=f"open expansion above {range_high}",
                pending_type="buy_stop", tag="up",
            )
        ]
    level = range_low - offset * ctx.spec.point
    sl_pts = int(ctx.to_points(range_high - level))
    return [
        make_signal(
            ctx, Side.SELL, ctx.spec.round_price(level), sl_pts, 2 * sl_pts,
            reason=f"open expansion below {range_low}",
            pending_type="sell_stop", tag="down",
        )
    ]


@register("F4")
def f4_compression_breakout(ctx: StrategyContext) -> list[Signal]:
    """Compression micro-breakout: tight box => OCO straddle of pending stops.
    The EA cancels the sibling on fill."""
    box_n = param_int(ctx.cfg, "box_bars")
    box_max = param_int(ctx.cfg, "box_max_points")
    offset = param_int(ctx.cfg, "breakout_offset_points")
    if len(ctx.bars) < box_n or not ctx.ticks:
        return []
    box = ctx.bars[-box_n:]
    box_high = max(b.high for b in box)
    box_low = min(b.low for b in box)
    if ctx.to_points(box_high - box_low) > box_max:
        return []
    oco = f"oco-{ctx.symbol}-{ctx.last_tick.time_msc}"
    up_level = ctx.spec.round_price(box_high + offset * ctx.spec.point)
    dn_level = ctx.spec.round_price(box_low - offset * ctx.spec.point)
    sl_pts = int(ctx.to_points(up_level - dn_level))
    return [
        make_signal(
            ctx, Side.BUY, up_level, sl_pts, 2 * sl_pts,
            reason=f"compression box {box_low}-{box_high}",
            pending_type="buy_stop", oco_group=oco, tag="up",
        ),
        make_signal(
            ctx, Side.SELL, dn_level, sl_pts, 2 * sl_pts,
            reason=f"compression box {box_low}-{box_high}",
            pending_type="sell_stop", oco_group=oco, tag="down",
        ),
    ]


@register("F5")
def f5_stoprun_reversion(ctx: StrategyContext) -> list[Signal]:
    """Stop-run reversion: a run through an obvious bar-extreme level followed by
    immediate tick-level rejection; strict invalidation beyond the run extreme."""
    lookback = param_int(ctx.cfg, "level_lookback_bars")
    run_min = param_int(ctx.cfg, "run_min_points")
    rejection_n = param_int(ctx.cfg, "rejection_ticks")
    if len(ctx.bars) < lookback or len(ctx.ticks) < rejection_n + 5:
        return []
    levels = ctx.bars[-lookback:]
    level_high = max(b.high for b in levels)
    level_low = min(b.low for b in levels)
    mids = [ctx.mid(t) for t in ctx.ticks]
    run_high = max(mids)
    run_low = min(mids)
    rejection = mids[-rejection_n:]

    # run above the obvious high, rejection back through it: fade short
    if (
        ctx.to_points(run_high - level_high) >= run_min
        and all(m < level_high for m in rejection)
    ):
        sl_pts = int(ctx.to_points(run_high - ctx.last_tick.ask) + ctx.spread_points) + 1
        return [
            make_signal(
                ctx, Side.SELL, ctx.last_tick.bid, sl_pts, 2 * sl_pts,
                reason=f"stop-run through {level_high} rejected", tag="fade-high",
            )
        ]
    if (
        ctx.to_points(level_low - run_low) >= run_min
        and all(m > level_low for m in rejection)
    ):
        sl_pts = int(ctx.to_points(ctx.last_tick.bid - run_low) + ctx.spread_points) + 1
        return [
            make_signal(
                ctx, Side.BUY, ctx.last_tick.ask, sl_pts, 2 * sl_pts,
                reason=f"stop-run through {level_low} rejected", tag="fade-low",
            )
        ]
    return []


@register("F6")
def f6_ml_filter(ctx: StrategyContext) -> list[Signal]:
    """Scaffold ONLY (§7): default OFF, may never originate. Returns nothing.
    A future F6 may scale confidence of F1-F5 signals after passing the full
    §11 evidence pipeline; it will still never produce a Signal of its own."""
    return []
