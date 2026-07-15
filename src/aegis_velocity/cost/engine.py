"""Cost Engine — GATE #1 (§5).

Every candidate (entry, SL, TP) must clear round-trip costs BEFORE consensus:
cost_points = spread_now + commission_points + slippage_p50; TP >= k x cost;
net RR >= floor (WR_be computed and carried everywhere); liquidity window,
session, rollover, Friday and news gates. Also owns the daily cost-burn meter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis_velocity.core.config import CostsConfig, RiskConfig, SessionsConfig
from aegis_velocity.cost.calendar import NewsCalendar, NewsStatus
from aegis_velocity.cost.liquidity import SpreadHistory
from aegis_velocity.cost.sessions import check_session
from aegis_velocity.mt5.protocol import SymbolSpec


@dataclass(frozen=True)
class CostCandidate:
    symbol: str
    sl_points: int
    tp_points: int
    spread_points: float
    server_now: datetime
    k_override: float | None = None  # strategy may demand MORE than config, never less
    min_rr_override: float | None = None


@dataclass(frozen=True)
class CostVerdict:
    ok: bool
    reasons: list[str]
    cost_points: float
    spread_points: float
    commission_points: float
    slippage_points: float
    cost_multiple: float  # tp_points / cost_points
    net_rr: float
    wr_be: float  # breakeven win rate implied by net RR
    detail: str = ""


def commission_points(spec: SymbolSpec, commission_per_lot_per_side: float) -> float:
    """Convert round-trip commission (account ccy / lot) into price points."""
    money_per_point_per_lot = spec.tick_value * (spec.point / spec.tick_size)
    if money_per_point_per_lot <= 0:
        raise ValueError(f"{spec.name}: non-positive point value; spec broken")
    return 2.0 * commission_per_lot_per_side / money_per_point_per_lot


def wr_breakeven(net_rr: float) -> float:
    return 1.0 / (1.0 + net_rr) if net_rr > 0 else 1.0


@dataclass
class CostBurnMeter:
    """Daily Σ(spread+commission+slippage) vs gross P&L, per symbol and total."""

    day: str = ""
    costs_by_symbol: dict[str, float] = field(default_factory=dict)
    gross_by_symbol: dict[str, float] = field(default_factory=dict)

    def record_fill(
        self, symbol: str, server_now: datetime, cost_ccy: float, gross_pnl_ccy: float
    ) -> None:
        today = server_now.date().isoformat()
        if today != self.day:
            self.day = today
            self.costs_by_symbol = {}
            self.gross_by_symbol = {}
        self.costs_by_symbol[symbol] = self.costs_by_symbol.get(symbol, 0.0) + cost_ccy
        self.gross_by_symbol[symbol] = self.gross_by_symbol.get(symbol, 0.0) + gross_pnl_ccy

    @property
    def total_costs(self) -> float:
        return sum(self.costs_by_symbol.values())

    @property
    def total_gross(self) -> float:
        return sum(self.gross_by_symbol.values())


class CostEngine:
    def __init__(
        self,
        costs: CostsConfig,
        risk: RiskConfig,
        sessions: SessionsConfig,
        calendar: NewsCalendar,
        spreads: SpreadHistory,
    ) -> None:
        self._costs = costs
        self._risk = risk
        self._sessions = sessions
        self._calendar = calendar
        self._spreads = spreads
        self.burn = CostBurnMeter()
        # measured slippage p50 per symbol (points), fed by live telemetry
        self._measured_slippage: dict[str, float] = {}

    @property
    def spreads(self) -> SpreadHistory:
        return self._spreads

    def update_measured_slippage(self, symbol: str, p50_points: float) -> None:
        self._measured_slippage[symbol] = p50_points

    def slippage_points(self, symbol: str) -> float:
        """Measured p50 when available; conservative prior until then."""
        measured = self._measured_slippage.get(symbol)
        prior = self._costs.slippage_prior(symbol)
        if measured is None:
            return prior
        return max(measured, 0.0)

    def evaluate(self, candidate: CostCandidate, spec: SymbolSpec) -> CostVerdict:
        reasons: list[str] = []
        details: list[str] = []

        comm_pts = commission_points(spec, self._costs.commission(candidate.symbol))
        slip_pts = self.slippage_points(candidate.symbol)
        cost_pts = candidate.spread_points + comm_pts + slip_pts

        # Clearance: TP distance must be a multiple of round-trip cost
        gate = self._costs.cost_gate
        k = max(gate.k_multiple, candidate.k_override or 0.0)
        multiple = candidate.tp_points / cost_pts if cost_pts > 0 else 0.0
        if candidate.tp_points < k * cost_pts:
            reasons.append("COST_GATE_FAIL")
            details.append(
                f"tp {candidate.tp_points}pt < {k:.1f} x cost {cost_pts:.1f}pt"
            )

        # Net RR after costs, both directions
        net_gain = candidate.tp_points - cost_pts
        net_loss = candidate.sl_points + cost_pts
        net_rr = net_gain / net_loss if net_loss > 0 else 0.0
        min_rr = max(gate.min_net_rr, candidate.min_rr_override or 0.0)
        if net_rr < min_rr:
            reasons.append("RR_TOO_LOW")
            details.append(f"net RR {net_rr:.2f} < floor {min_rr:.2f}")

        # Hard spread cap
        if candidate.spread_points > self._risk.spread_cap(candidate.symbol):
            reasons.append("SPREAD_TOO_HIGH")
            details.append(
                f"spread {candidate.spread_points:.1f} > cap "
                f"{self._risk.spread_cap(candidate.symbol)}"
            )

        # Liquidity window: current spread vs same-hour historical percentile
        threshold = self._spreads.spread_percentile(
            candidate.symbol,
            candidate.server_now,
            float(self._costs.liquidity_window.spread_percentile_max),
        )
        if threshold is None:
            reasons.append("LIQUIDITY_WINDOW")
            details.append("insufficient same-hour spread history (fail closed)")
        elif candidate.spread_points > threshold:
            reasons.append("LIQUIDITY_WINDOW")
            details.append(
                f"spread {candidate.spread_points:.1f} > "
                f"p{self._costs.liquidity_window.spread_percentile_max} {threshold:.1f}"
            )

        session = check_session(self._sessions, candidate.server_now)
        if not session.ok:
            reasons.append(session.reason)

        news = self._calendar.status(candidate.symbol, candidate.server_now)
        if news.status is NewsStatus.BLACKOUT:
            reasons.append("NEWS_BLACKOUT")
            details.append(news.detail)
        elif news.status is NewsStatus.UNKNOWN:
            reasons.append("NEWS_UNKNOWN_BLOCKED")
            details.append(news.detail)

        return CostVerdict(
            ok=not reasons,
            reasons=reasons,
            cost_points=cost_pts,
            spread_points=candidate.spread_points,
            commission_points=comm_pts,
            slippage_points=slip_pts,
            cost_multiple=multiple,
            net_rr=net_rr,
            wr_be=wr_breakeven(net_rr),
            detail="; ".join(details),
        )
