"""Velocity guards (§6). Each guard is independent, restart-safe where stateful,
and answers one question with a machine-readable reason."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aegis_velocity.core.anchors import PersistedCounters
from aegis_velocity.core.config import RiskConfig
from aegis_velocity.cost.liquidity import percentile


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reason: str = ""
    detail: str = ""


class TradeRateGuard:
    """Hourly/daily frequency caps; counters persist across restarts."""

    def __init__(self, cfg: RiskConfig, counters: PersistedCounters) -> None:
        self._cfg = cfg
        self._counters = counters

    def record_trade(self, symbol: str, now: datetime) -> None:
        self._counters.record_event("trades_global", now)
        self._counters.record_event(f"trades_{symbol}", now)

    def check(self, symbol: str, now: datetime) -> GuardVerdict:
        hourly = self._counters.count_within("trades_global", now, 3600)
        if hourly >= self._cfg.max_trades_per_hour_global:
            return GuardVerdict(False, "HOURLY_CAP", f"{hourly} trades in last hour")
        sym_hourly = self._counters.count_within(f"trades_{symbol}", now, 3600)
        if sym_hourly >= self._cfg.max_trades_per_symbol_per_hour:
            return GuardVerdict(False, "HOURLY_CAP", f"{symbol}: {sym_hourly}/h")
        daily = self._counters.count_within("trades_global", now, 86400)
        if daily >= self._cfg.max_trades_per_day_global:
            return GuardVerdict(False, "DAILY_CAP", f"{daily} trades today")
        return GuardVerdict(True)


class LossVelocityGuard:
    """R lost too fast => pause all entries for the configured window."""

    def __init__(self, cfg: RiskConfig, counters: PersistedCounters) -> None:
        self._cfg = cfg.loss_velocity_halt
        self._counters = counters

    def record_result(self, r_multiple: float, now: datetime) -> None:
        if r_multiple < 0:
            self._counters.record_event("r_losses", now)
            # store magnitudes alongside timestamps in a parallel rolling sum
            total = self._counters.get_float("r_loss_ring", 0.0)
            self._counters.set_float("r_loss_ring", total)  # keep key present
            losses = self._counters.get_str("r_loss_samples")
            samples = [s for s in losses.split(";") if s]
            samples.append(f"{now.timestamp():.0f}:{abs(r_multiple):.4f}")
            self._counters.set_str("r_loss_samples", ";".join(samples[-500:]))

    def _lost_in_window(self, now: datetime) -> float:
        cutoff = (now - timedelta(minutes=self._cfg.window_minutes)).timestamp()
        total = 0.0
        for sample in self._counters.get_str("r_loss_samples").split(";"):
            if not sample:
                continue
            ts, mag = sample.split(":")
            if float(ts) >= cutoff:
                total += float(mag)
        return total

    def check(self, now: datetime) -> GuardVerdict:
        paused_until = self._counters.get_float("loss_velocity_pause_until", 0.0)
        if now.timestamp() < paused_until:
            return GuardVerdict(
                False, "LOSS_VELOCITY_HALT", f"paused for {paused_until - now.timestamp():.0f}s"
            )
        lost = self._lost_in_window(now)
        if lost >= self._cfg.R_lost:
            until = now + timedelta(minutes=self._cfg.pause_minutes)
            self._counters.set_float("loss_velocity_pause_until", until.timestamp())
            return GuardVerdict(
                False,
                "LOSS_VELOCITY_HALT",
                f"{lost:.1f}R lost in {self._cfg.window_minutes}min",
            )
        return GuardVerdict(True)


class MicroCooldownGuard:
    """N consecutive losses on a symbol x strategy => minutes of cooldown."""

    def __init__(self, cfg: RiskConfig, counters: PersistedCounters) -> None:
        self._cfg = cfg.consecutive_loss_micro_cooldown
        self._counters = counters

    def record_result(self, symbol: str, strategy: str, won: bool, now: datetime) -> None:
        key = f"consec_{symbol}_{strategy}"
        if won:
            self._counters.set_int(key, 0)
            return
        streak = self._counters.get_int(key) + 1
        self._counters.set_int(key, streak)
        if streak >= self._cfg.losses:
            until = now + timedelta(minutes=self._cfg.minutes)
            self._counters.set_float(f"cooldown_until_{symbol}_{strategy}", until.timestamp())
            self._counters.set_int(key, 0)

    def check(self, symbol: str, strategy: str, now: datetime) -> GuardVerdict:
        until = self._counters.get_float(f"cooldown_until_{symbol}_{strategy}", 0.0)
        if now.timestamp() < until:
            remaining = until - now.timestamp()
            return GuardVerdict(
                False, "MICRO_COOLDOWN", f"{symbol}/{strategy} cooling {remaining:.0f}s"
            )
        return GuardVerdict(True)


class AntiChurnGuard:
    """No same-direction re-entry on a symbol immediately after a stop-out."""

    def __init__(self, cfg: RiskConfig, counters: PersistedCounters) -> None:
        self._window_s = cfg.anti_churn_seconds
        self._counters = counters

    def record_stopout(self, symbol: str, direction: str, now: datetime) -> None:
        self._counters.set_float(f"stopout_{symbol}_{direction}", now.timestamp())

    def check(self, symbol: str, direction: str, now: datetime) -> GuardVerdict:
        last = self._counters.get_float(f"stopout_{symbol}_{direction}", 0.0)
        elapsed = now.timestamp() - last
        if last > 0 and elapsed < self._window_s:
            return GuardVerdict(
                False, "ANTI_CHURN", f"{symbol} {direction} stop-out {elapsed:.0f}s ago"
            )
        return GuardVerdict(True)


class OrderStormFuse:
    """Hard global fuse on order_send calls per minute. Latching: requires reset."""

    def __init__(self, cfg: RiskConfig) -> None:
        self._cap = cfg.order_storm_fuse_per_minute
        self._sends: list[float] = []
        self._blown = False

    def record_send(self, now: datetime) -> None:
        self._sends.append(now.timestamp())
        cutoff = now.timestamp() - 60.0
        self._sends = [t for t in self._sends if t >= cutoff]
        if len(self._sends) >= self._cap:
            self._blown = True

    def check(self, now: datetime) -> GuardVerdict:
        if self._blown:
            return GuardVerdict(False, "ORDER_STORM", "fuse blown; manual reset required")
        cutoff = now.timestamp() - 60.0
        recent = sum(1 for t in self._sends if t >= cutoff)
        if recent >= self._cap:
            self._blown = True
            return GuardVerdict(False, "ORDER_STORM", f"{recent} sends/min >= {self._cap}")
        return GuardVerdict(True)

    def reset(self) -> None:
        self._blown = False
        self._sends.clear()

    @property
    def blown(self) -> bool:
        return self._blown


class SlippageBreaker:
    """Broker fading us: rolling p90 of |fill slippage| vs the model => halt entries."""

    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg.slippage_breaker
        self._samples: dict[str, list[float]] = {}
        self._tripped: set[str] = set()

    def record_fill(self, symbol: str, slippage_points: float, model_p50_points: float) -> None:
        bucket = self._samples.setdefault(symbol, [])
        bucket.append(abs(slippage_points))
        if len(bucket) > self._cfg.window_fills:
            del bucket[: len(bucket) - self._cfg.window_fills]
        if len(bucket) >= self._cfg.window_fills and model_p50_points > 0:
            p90 = percentile(bucket, 90)
            if p90 > self._cfg.p90_multiple * model_p50_points:
                self._tripped.add(symbol)

    def check(self, symbol: str) -> GuardVerdict:
        if symbol in self._tripped:
            return GuardVerdict(
                False, "SLIPPAGE_BREAKER", f"{symbol} p90 slippage above model bound"
            )
        return GuardVerdict(True)

    def reset(self, symbol: str) -> None:
        self._tripped.discard(symbol)
        self._samples.pop(symbol, None)
