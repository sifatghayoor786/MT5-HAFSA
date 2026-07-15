"""Tick-level backtester (§11). Bar-OHLC backtests of scalps are NON-EVIDENCE
and are not implemented here on purpose.

Mechanics: bid/ask fills; market entries delayed by simulated latency
(100-400 ms, deterministic per-trade from a seeded sequence); pending stops
trigger at touch on the correct side; SL/TP execute at touch; per-symbol
commission; optional stressed-cost multipliers (spread x1.5, slippage x2,
latency x2). No look-ahead: every decision uses only ticks at or before the
decision time; the fill price comes from the FIRST tick at/after
decision_time + latency.
"""

from __future__ import annotations

import random
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from aegis_velocity.core.events import Side, Signal
from aegis_velocity.mt5.protocol import SymbolSpec, Tick

SignalMaker = Callable[[tuple[Tick, ...]], list[Signal]]


@dataclass(frozen=True)
class StressConfig:
    spread_multiple: float = 1.0
    slippage_multiple: float = 1.0
    latency_multiple: float = 1.0

    @classmethod
    def stressed(cls) -> StressConfig:
        return cls(spread_multiple=1.5, slippage_multiple=2.0, latency_multiple=2.0)


@dataclass(frozen=True)
class BacktestFill:
    signal_id: str
    strategy_id: str
    symbol: str
    side: Side
    entry_time_msc: int
    entry_price: float
    exit_time_msc: int
    exit_price: float
    exit_reason: str  # TP | SL | TIME_STOP | END_OF_DATA
    latency_ms: float
    gross_r: float
    net_r: float
    costs_points: float


@dataclass
class BacktestResult:
    fills: list[BacktestFill] = field(default_factory=list)
    signals_seen: int = 0
    signals_untriggered: int = 0

    @property
    def returns_r(self) -> list[float]:
        return [f.net_r for f in self.fills]


class TickBacktester:
    def __init__(
        self,
        spec: SymbolSpec,
        ticks: list[Tick],
        commission_per_lot_per_side_points: float,
        stress: StressConfig | None = None,
        seed: int = 7,
    ) -> None:
        if not ticks:
            raise ValueError("tick backtests need ticks; bar backtests are NON-EVIDENCE")
        self._spec = spec
        self._ticks = ticks
        self._times = [t.time_msc for t in ticks]
        self._commission_points = commission_per_lot_per_side_points
        self._stress = stress if stress is not None else StressConfig()
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------ core

    def _stressed_prices(self, tick: Tick) -> tuple[float, float]:
        """Widen the quoted spread around the mid by the stress multiple."""
        if self._stress.spread_multiple == 1.0:
            return tick.bid, tick.ask
        mid = (tick.bid + tick.ask) / 2.0
        half = (tick.ask - tick.bid) / 2.0 * self._stress.spread_multiple
        return mid - half, mid + half

    def _first_index_at_or_after(self, time_msc: int) -> int:
        return bisect_left(self._times, time_msc)

    def _draw_latency_ms(self) -> float:
        return self._rng.uniform(100.0, 400.0) * self._stress.latency_multiple

    def run_signal(self, signal: Signal, decision_time_msc: int) -> BacktestFill | None:
        """Simulate one signal end-to-end. Returns None if never triggered."""
        point = self._spec.point
        side = signal.side

        if signal.trigger == "pending":
            entry_idx, entry_price = self._pending_trigger_index(signal, decision_time_msc)
            latency_ms = 0.0  # server-side execution: no client latency
        else:
            latency_ms = self._draw_latency_ms()
            fill_time = decision_time_msc + int(latency_ms)
            entry_idx = self._first_index_at_or_after(fill_time)
            if entry_idx >= len(self._ticks):
                return None
            bid, ask = self._stressed_prices(self._ticks[entry_idx])
            slip = (
                self._commission_slippage_points() * point * self._stress.slippage_multiple
            )
            entry_price = (ask + slip) if side is Side.BUY else (bid - slip)
        if entry_idx is None or entry_idx >= len(self._ticks):
            return None

        sl_price = (
            entry_price - signal.sl_points * point
            if side is Side.BUY
            else entry_price + signal.sl_points * point
        )
        tp_price = (
            entry_price + signal.tp_points * point
            if side is Side.BUY
            else entry_price - signal.tp_points * point
        )
        deadline = (
            self._ticks[entry_idx].time + timedelta(seconds=signal.max_hold_s or 900)
        )

        exit_price = None
        exit_reason = "END_OF_DATA"
        exit_idx = len(self._ticks) - 1
        for i in range(entry_idx + 1, len(self._ticks)):
            tick = self._ticks[i]
            bid, ask = self._stressed_prices(tick)
            mark = bid if side is Side.BUY else ask
            if side is Side.BUY:
                if bid <= sl_price:
                    exit_price, exit_reason, exit_idx = sl_price, "SL", i
                    break
                if bid >= tp_price:
                    exit_price, exit_reason, exit_idx = tp_price, "TP", i
                    break
            else:
                if ask >= sl_price:
                    exit_price, exit_reason, exit_idx = sl_price, "SL", i
                    break
                if ask <= tp_price:
                    exit_price, exit_reason, exit_idx = tp_price, "TP", i
                    break
            if tick.time >= deadline:
                exit_price, exit_reason, exit_idx = mark, "TIME_STOP", i
                break
        if exit_price is None:
            final = self._ticks[-1]
            bid, ask = self._stressed_prices(final)
            exit_price = bid if side is Side.BUY else ask

        gross_points = (exit_price - entry_price) / point * side.sign
        cost_points = 2 * self._commission_points
        risk_points = float(signal.sl_points) if signal.sl_points else 1.0
        gross_r = gross_points / risk_points
        net_r = (gross_points - cost_points) / risk_points
        return BacktestFill(
            signal_id=signal.event_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=side,
            entry_time_msc=self._ticks[entry_idx].time_msc,
            entry_price=entry_price,
            exit_time_msc=self._ticks[exit_idx].time_msc,
            exit_price=exit_price,
            exit_reason=exit_reason,
            latency_ms=latency_ms,
            gross_r=gross_r,
            net_r=net_r,
            costs_points=cost_points,
        )

    def _pending_trigger_index(
        self, signal: Signal, decision_time_msc: int
    ) -> tuple[int, float]:
        """Pending orders trigger AT TOUCH on the correct side of the book."""
        level = signal.entry_price
        ttl_msc = decision_time_msc + 1_800_000  # 30 min default order TTL
        start = self._first_index_at_or_after(decision_time_msc)
        for i in range(start, len(self._ticks)):
            tick = self._ticks[i]
            if tick.time_msc > ttl_msc:
                break
            bid, ask = self._stressed_prices(tick)
            kind = signal.pending_type
            hit = (
                (kind == "buy_stop" and ask >= level)
                or (kind == "sell_stop" and bid <= level)
                or (kind == "buy_limit" and ask <= level)
                or (kind == "sell_limit" and bid >= level)
            )
            if hit:
                return i, level
        return len(self._ticks), 0.0

    def _commission_slippage_points(self) -> float:
        return 1.0  # deterministic base slippage point on market fills


def run_strategy_over_ticks(
    strategy_id: str,
    spec: SymbolSpec,
    ticks: list[Tick],
    make_signals: SignalMaker,
    window: int = 80,
    step: int = 20,
    commission_points: float = 3.0,
    stress: StressConfig | None = None,
) -> BacktestResult:
    """Walk the tick series; at each step feed the trailing window to the
    strategy exactly as the live engine would (no look-ahead possible: the
    window ends at the decision tick)."""
    bt = TickBacktester(spec, ticks, commission_points, stress)
    result = BacktestResult()
    open_until_msc = 0
    for end in range(window, len(ticks), step):
        window_ticks = tuple(ticks[end - window : end])
        decision_msc = window_ticks[-1].time_msc
        if decision_msc < open_until_msc:
            continue  # one position at a time, like the live desk
        signals = make_signals(window_ticks)
        for signal in signals:
            result.signals_seen += 1
            fill = bt.run_signal(signal, decision_msc)
            if fill is None:
                result.signals_untriggered += 1
                continue
            result.fills.append(fill)
            open_until_msc = fill.exit_time_msc
            break
    return result
