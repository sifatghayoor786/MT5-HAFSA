"""Consensus (§8): hard gates first (any fail => REJECT with machine-readable
reason), then a slim quality score. The decision path is lightweight; the FULL
council record lands in the ledger asynchronously via the auditor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from aegis_velocity.core.clock import TICK_MAX_AGE_S, BrokerClock
from aegis_velocity.core.config import RiskConfig
from aegis_velocity.core.events import Decision, Signal, Verdict
from aegis_velocity.cost.engine import CostCandidate, CostEngine, CostVerdict
from aegis_velocity.mt5.protocol import SymbolSpec, Tick


@dataclass
class CouncilInputs:
    signal: Signal
    spec: SymbolSpec
    tick: Tick
    server_now: datetime
    data_integrity: float  # 0-100, tick-freshness weighted
    conflicting_position: bool
    in_flight_intent: bool
    strategy_quarantined: bool
    strategy_reliability: float = 0.5  # Wilson-LB based, 0-1
    recent_slippage_ok: bool = True


@dataclass(frozen=True)
class CouncilResult:
    decision: Decision
    cost: CostVerdict | None = None


@dataclass
class Council:
    cost_engine: CostEngine
    risk_cfg: RiskConfig
    clock: BrokerClock
    min_data_integrity: float = 70.0
    quality_floor_approve: float = 0.60
    quality_floor_reduced: float = 0.45
    quality_floor_shadow: float = 0.30
    _last_decision_ms: float = field(default=0.0, init=False)

    def decide(self, inputs: CouncilInputs) -> CouncilResult:
        t0 = time.perf_counter()
        signal = inputs.signal
        reasons: list[str] = []

        spread_points = float(inputs.tick.spread_points(inputs.spec.point))
        cost_verdict = self.cost_engine.evaluate(
            CostCandidate(
                symbol=signal.symbol,
                sl_points=signal.sl_points,
                tp_points=signal.tp_points,
                spread_points=spread_points,
                server_now=inputs.server_now,
            ),
            inputs.spec,
        )
        # hard gate chain (order matters for reason readability, all are checked)
        if not cost_verdict.ok:
            reasons.extend(cost_verdict.reasons)
        if inputs.data_integrity < self.min_data_integrity:
            reasons.append("DATA_STALE")
        # tick age measured against SERVER time (both sides broker clock): <= 500 ms
        tick_age_s = (inputs.server_now - inputs.tick.time).total_seconds()
        if not -TICK_MAX_AGE_S <= tick_age_s <= TICK_MAX_AGE_S:
            reasons.append("DATA_STALE" if "DATA_STALE" not in reasons else "TICK_STALE")
        age_s = (inputs.server_now - signal.signal_time_utc).total_seconds()
        if age_s > signal_max_age(signal):
            reasons.append("SIGNAL_TOO_OLD")
        if inputs.conflicting_position or inputs.in_flight_intent:
            reasons.append("CONFLICTING_EXPOSURE")
        if inputs.strategy_quarantined:
            reasons.append("STRATEGY_QUARANTINED")

        if reasons:
            decision = self._finalise(inputs, Verdict.REJECT, reasons, 0.0, cost_verdict, t0)
            return CouncilResult(decision, cost_verdict)

        # slim quality score: cost headroom, reliability, integrity, slippage recency
        headroom = min(1.0, cost_verdict.cost_multiple / (2 * 4.0))  # 2x the k floor = 1.0
        integrity = min(1.0, inputs.data_integrity / 100.0)
        slippage = 1.0 if inputs.recent_slippage_ok else 0.4
        quality = (
            0.35 * headroom
            + 0.30 * inputs.strategy_reliability
            + 0.20 * integrity
            + 0.15 * slippage
        )
        if quality >= self.quality_floor_approve:
            verdict = Verdict.APPROVE
        elif quality >= self.quality_floor_reduced:
            verdict = Verdict.APPROVE_REDUCED
        elif quality >= self.quality_floor_shadow:
            verdict = Verdict.SHADOW_ONLY
        else:
            verdict = Verdict.REJECT
            reasons.append("QUALITY_FLOOR")
        decision = self._finalise(inputs, verdict, reasons, quality, cost_verdict, t0)
        return CouncilResult(decision, cost_verdict)

    def _finalise(
        self,
        inputs: CouncilInputs,
        verdict: Verdict,
        reasons: list[str],
        quality: float,
        cost: CostVerdict,
        t0: float,
    ) -> Decision:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._last_decision_ms = latency_ms
        return Decision(
            correlation_id=inputs.signal.correlation_id,
            signal_id=inputs.signal.event_id,
            strategy_id=inputs.signal.strategy_id,
            symbol=inputs.signal.symbol,
            verdict=verdict,
            reasons=reasons,
            quality_score=quality,
            cost_points=cost.cost_points,
            cost_multiple=cost.cost_multiple,
            net_rr=cost.net_rr,
            wr_be=cost.wr_be,
            decision_latency_ms=latency_ms,
        )

    @property
    def last_decision_latency_ms(self) -> float:
        return self._last_decision_ms


def signal_max_age(signal: Signal) -> float:
    """Pendings tolerate longer arming windows than tick-armed market entries."""
    return 30.0 if signal.trigger == "pending" else 2.0
