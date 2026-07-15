"""Risk Commander (§6) — the ABSOLUTE VETO.

Dual-method sizing (tick math cross-checked against order_calc_profit, 2%
agreement), floor-only volume rounding, halt ladder from persisted anchors,
frequency guards, correlation-weighted open risk, canary sizing. If accurate
risk cannot be computed: REJECT.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from aegis_velocity.core.anchors import EquityAnchors, PersistedCounters
from aegis_velocity.core.config import CorrelationsConfig, RiskConfig, TradingMode
from aegis_velocity.core.events import Side
from aegis_velocity.mt5.protocol import SymbolSpec
from aegis_velocity.risk.guards import (
    AntiChurnGuard,
    GuardVerdict,
    LossVelocityGuard,
    MicroCooldownGuard,
    OrderStormFuse,
    SlippageBreaker,
    TradeRateGuard,
)

DUAL_METHOD_TOLERANCE = 0.02
CANARY_MAX_RISK_MULTIPLE = 1.5

# order_calc_profit(side, symbol, volume, price_open, price_close) -> ccy or None
ProfitCalc = Callable[[Side, str, float, float, float], float | None]
# order_calc_margin(side, symbol, volume, price) -> ccy or None
MarginCalc = Callable[[Side, str, float, float], float | None]


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    lots: float = 0.0
    risk_ccy: float = 0.0
    risk_frac: float = 0.0
    reasons: list[str] = field(default_factory=list)
    tick_math_loss: float = 0.0
    broker_calc_loss: float = 0.0
    margin_required: float = 0.0
    canary: bool = False
    detail: str = ""


@dataclass(frozen=True)
class OpenExposure:
    symbol: str
    risk_frac: float  # fraction of equity at risk to the SL


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    sizing: SizingResult | None = None
    ts: datetime | None = None  # verdicts expire (preflight checks age <= 2 s)


def floor_to_step(volume: float, step: float) -> float:
    steps = int(volume / step + 1e-9)
    return round(steps * step, 8)


class RiskCommander:
    def __init__(
        self,
        cfg: RiskConfig,
        correlations: CorrelationsConfig,
        anchors: EquityAnchors,
        counters: PersistedCounters,
        mode: TradingMode,
    ) -> None:
        self._cfg = cfg
        self._corr = correlations
        self._anchors = anchors
        self._counters = counters
        self._mode = mode
        self.rate_guard = TradeRateGuard(cfg, counters)
        self.loss_velocity = LossVelocityGuard(cfg, counters)
        self.micro_cooldown = MicroCooldownGuard(cfg, counters)
        self.anti_churn = AntiChurnGuard(cfg, counters)
        self.storm_fuse = OrderStormFuse(cfg)
        self.slippage_breaker = SlippageBreaker(cfg)

    # ------------------------------------------------------------------ sizing

    def size_position(
        self,
        spec: SymbolSpec,
        side: Side,
        entry: float,
        sl: float,
        equity: float,
        calc_profit: ProfitCalc,
        calc_margin: MarginCalc,
        margin_used: float,
    ) -> SizingResult:
        reasons: list[str] = []
        if equity <= 0:
            return SizingResult(False, reasons=["NO_EQUITY"])
        distance = abs(entry - sl)
        if distance <= 0 or sl <= 0:
            return SizingResult(False, reasons=["NO_STOP"])

        canary = self._canary_active()
        risk_ccy = equity * self._cfg.risk_per_trade
        ticks = distance / spec.tick_size
        loss_per_lot = ticks * spec.tick_value_loss
        if loss_per_lot <= 0:
            return SizingResult(False, reasons=["SPEC_MISMATCH"], detail="loss_per_lot<=0")

        lots_raw = risk_ccy / loss_per_lot
        lots = floor_to_step(lots_raw, spec.volume_step)  # FLOOR only, never round up

        if canary:
            lots = spec.volume_min
            min_lot_risk = spec.volume_min * loss_per_lot
            if min_lot_risk > CANARY_MAX_RISK_MULTIPLE * risk_ccy:
                return SizingResult(
                    False,
                    reasons=["CANARY_MIN_LOT_TOO_RISKY"],
                    canary=True,
                    detail=f"min-lot risk {min_lot_risk:.2f} > 1.5x {risk_ccy:.2f}",
                )
        elif lots < spec.volume_min:
            return SizingResult(
                False,
                reasons=["RISK_TOO_SMALL_FOR_MIN_LOT"],
                detail=f"floored {lots} < volume_min {spec.volume_min}",
            )
        lots = min(lots, spec.volume_max)

        # cap by max_risk_per_trade
        actual_risk_ccy = lots * loss_per_lot
        if actual_risk_ccy > equity * self._cfg.max_risk_per_trade:
            capped_raw = (equity * self._cfg.max_risk_per_trade) / loss_per_lot
            lots = floor_to_step(capped_raw, spec.volume_step)
            if lots < spec.volume_min:
                return SizingResult(False, reasons=["RISK_TOO_SMALL_FOR_MIN_LOT"])
            actual_risk_ccy = lots * loss_per_lot

        # dual-method cross-check (mandatory)
        broker_loss_raw = calc_profit(side, spec.name, lots, entry, sl)
        if broker_loss_raw is None:
            return SizingResult(False, reasons=["SPEC_MISMATCH"], detail="order_calc_profit=None")
        broker_loss = abs(broker_loss_raw)
        tick_loss = lots * loss_per_lot
        if broker_loss <= 0 or abs(broker_loss - tick_loss) / broker_loss > DUAL_METHOD_TOLERANCE:
            return SizingResult(
                False,
                reasons=["SPEC_MISMATCH"],
                tick_math_loss=tick_loss,
                broker_calc_loss=broker_loss,
                detail=f"tick {tick_loss:.2f} vs broker {broker_loss:.2f} disagree >2%",
            )

        margin = calc_margin(side, spec.name, lots, entry)
        if margin is None:
            return SizingResult(False, reasons=["SPEC_MISMATCH"], detail="order_calc_margin=None")
        projected_margin = margin_used + margin
        if projected_margin > 0:
            projected_level = equity / projected_margin * 100.0
            if projected_level < self._cfg.min_margin_level_pct:
                return SizingResult(
                    False,
                    reasons=["MARGIN_FLOOR"],
                    margin_required=margin,
                    detail=f"projected margin level {projected_level:.0f}% "
                    f"< {self._cfg.min_margin_level_pct}%",
                )

        return SizingResult(
            ok=not reasons,
            lots=lots,
            risk_ccy=actual_risk_ccy,
            risk_frac=actual_risk_ccy / equity,
            reasons=reasons,
            tick_math_loss=tick_loss,
            broker_calc_loss=broker_loss,
            margin_required=margin,
            canary=canary,
        )

    # ------------------------------------------------------------------ gates

    def check_halts(self, equity: float) -> GuardVerdict:
        st = self._anchors.status(equity)
        if st.peak_drawdown_frac >= self._cfg.hard_drawdown_halt:
            return GuardVerdict(
                False, "HARD_DRAWDOWN", f"{st.peak_drawdown_frac:.2%} from peak"
            )
        if st.weekly_loss_frac >= self._cfg.weekly_equity_loss_halt:
            return GuardVerdict(False, "WEEKLY_LOSS_HALT", f"{st.weekly_loss_frac:.2%}")
        if st.daily_loss_frac >= self._cfg.daily_equity_loss_halt:
            return GuardVerdict(False, "DAILY_LOSS_HALT", f"{st.daily_loss_frac:.2%}")
        return GuardVerdict(True)

    def check_position_caps(
        self, symbol: str, open_positions: list[OpenExposure]
    ) -> GuardVerdict:
        if len(open_positions) >= self._cfg.max_simultaneous_positions:
            return GuardVerdict(False, "MAX_POSITIONS", f"{len(open_positions)} open")
        per_symbol = sum(1 for p in open_positions if p.symbol == symbol)
        if per_symbol >= self._cfg.max_positions_per_symbol:
            return GuardVerdict(False, "MAX_POSITIONS_SYMBOL", symbol)
        return GuardVerdict(True)

    def check_open_risk(
        self, candidate_symbol: str, candidate_risk_frac: float,
        open_positions: list[OpenExposure],
    ) -> GuardVerdict:
        total = sum(p.risk_frac for p in open_positions) + candidate_risk_frac
        if total > self._cfg.max_total_open_risk:
            return GuardVerdict(
                False, "MAX_OPEN_RISK", f"{total:.4f} > {self._cfg.max_total_open_risk}"
            )
        weighted = candidate_risk_frac + sum(
            p.risk_frac * self._corr.weight(candidate_symbol, p.symbol)
            for p in open_positions
        )
        if weighted > self._corr.max_correlation_weighted_risk:
            return GuardVerdict(
                False,
                "CORRELATED_EXPOSURE",
                f"corr-weighted {weighted:.4f} > {self._corr.max_correlation_weighted_risk}",
            )
        return GuardVerdict(True)

    def evaluate(
        self,
        symbol: str,
        strategy: str,
        direction: Side,
        now: datetime,
        equity: float,
        open_positions: list[OpenExposure],
        sizing: SizingResult,
    ) -> RiskVerdict:
        """Full veto chain; ALL reasons are collected for the ledger."""
        reasons: list[str] = []
        if not sizing.ok:
            reasons.extend(sizing.reasons)
        for verdict in (
            self.check_halts(equity),
            self.check_position_caps(symbol, open_positions),
            self.check_open_risk(symbol, sizing.risk_frac, open_positions),
            self.rate_guard.check(symbol, now),
            self.loss_velocity.check(now),
            self.micro_cooldown.check(symbol, strategy, now),
            self.anti_churn.check(symbol, direction.value, now),
            self.storm_fuse.check(now),
            self.slippage_breaker.check(symbol),
        ):
            if not verdict.ok:
                reasons.append(verdict.reason)
        return RiskVerdict(ok=not reasons, reasons=reasons, sizing=sizing, ts=now)

    # --------------------------------------------------------------- feedback

    def record_fill(self, symbol: str, now: datetime) -> None:
        self.rate_guard.record_trade(symbol, now)
        if self._canary_active():
            self._counters.set_int("canary_fills", self._counters.get_int("canary_fills") + 1)

    def record_close(
        self,
        symbol: str,
        strategy: str,
        direction: Side,
        r_multiple: float,
        stopped_out: bool,
        now: datetime,
    ) -> None:
        self.loss_velocity.record_result(r_multiple, now)
        self.micro_cooldown.record_result(symbol, strategy, r_multiple > 0, now)
        if stopped_out:
            self.anti_churn.record_stopout(symbol, direction.value, now)

    # ----------------------------------------------------------------- canary

    def _canary_active(self) -> bool:
        if self._mode is not TradingMode.LIVE_CANARY:
            return False
        return self._counters.get_int("canary_fills") < self._cfg.canary_fills

    @property
    def canary_fills_done(self) -> int:
        return self._counters.get_int("canary_fills")

    def canary_complete(self) -> bool:
        return self._counters.get_int("canary_fills") >= self._cfg.canary_fills
