"""Evidence & promotion pipeline (§11) — the ONLY door to live.

RESEARCH -> TICK-BACKTEST -> WALK-FORWARD -> SHADOW -> DEMO -> LIVE_CANARY -> LIVE.
Insufficient data is INSUFFICIENT_DATA, never a pass. Every gate reports its
numbers; nothing is projected or promised.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import StrEnum

from aegis_velocity.audit.stats import (
    benjamini_hochberg,
    expectancy,
    max_drawdown,
    profit_factor,
)

MIN_OOS_TRADES = 1_000
MIN_SHADOW_SESSIONS = 5
MIN_SHADOW_SIGNALS = 500
MIN_DEMO_FILLS = 300
CANARY_FILLS = 50
PF_FLOOR = 1.10
PLATEAU_RETENTION = 0.70
FDR_Q = 0.10
MONTE_CARLO_PATHS = 1_000


class GateOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class GateResult:
    name: str
    outcome: GateOutcome
    detail: str


@dataclass
class WalkForwardEvidence:
    """Out-of-sample net-R returns under STRESSED costs, plus plateau runs."""

    oos_returns_r: list[float] = field(default_factory=list)
    plateau_expectancies: dict[str, float] = field(default_factory=dict)  # perturbation -> exp
    base_expectancy: float = 0.0
    p_value: float = 1.0  # H0: expectancy <= 0 (one-sided t approximation)


def t_test_p_value(returns: list[float]) -> float:
    """One-sided p for H0: mean <= 0, normal approximation (n is large here)."""
    n = len(returns)
    if n < 2:
        return 1.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var == 0:
        return 0.0 if mean > 0 else 1.0
    t = mean / math.sqrt(var / n)
    return 0.5 * math.erfc(t / math.sqrt(2.0))


def walkforward_gate(evidence: WalkForwardEvidence) -> list[GateResult]:
    results: list[GateResult] = []
    returns = evidence.oos_returns_r
    n = len(returns)

    if n < MIN_OOS_TRADES:
        results.append(
            GateResult(
                "oos_count", GateOutcome.INSUFFICIENT_DATA,
                f"{n} OOS trades < {MIN_OOS_TRADES} required",
            )
        )
        return results
    results.append(GateResult("oos_count", GateOutcome.PASS, f"n={n}"))

    pf = profit_factor(returns)
    exp = expectancy(returns)
    ok = pf >= PF_FLOOR and exp > 0
    results.append(
        GateResult(
            "stressed_costs",
            GateOutcome.PASS if ok else GateOutcome.FAIL,
            f"PF={pf:.3f} (floor {PF_FLOOR}), expectancy={exp:.4f}R at spread x1.5,"
            " slippage x2, latency x2",
        )
    )

    if evidence.base_expectancy > 0 and evidence.plateau_expectancies:
        retained = [
            e / evidence.base_expectancy for e in evidence.plateau_expectancies.values()
        ]
        plateau_ok = all(r >= PLATEAU_RETENTION for r in retained)
        results.append(
            GateResult(
                "parameter_plateau",
                GateOutcome.PASS if plateau_ok else GateOutcome.FAIL,
                f"min retention {min(retained):.0%} over ±20% perturbations "
                f"(floor {PLATEAU_RETENTION:.0%})",
            )
        )
    else:
        results.append(
            GateResult("parameter_plateau", GateOutcome.INSUFFICIENT_DATA,
                       "no plateau runs supplied")
        )

    mc = monte_carlo_worst_path(returns)
    mc_ok = mc > -0.5 * abs(sum(returns))  # 5th-pct path must not devastate
    results.append(
        GateResult(
            "monte_carlo",
            GateOutcome.PASS if mc_ok else GateOutcome.FAIL,
            f"5th-percentile resampled path trough {mc:.1f}R",
        )
    )

    without_top5 = sorted(returns, reverse=True)[5:]  # drop exactly 5 trades
    without_top = expectancy(without_top5 or [0.0])
    top_ok = without_top > 0
    results.append(
        GateResult(
            "top5_removal",
            GateOutcome.PASS if top_ok else GateOutcome.FAIL,
            f"expectancy without top-5 trades: {without_top:.4f}R",
        )
    )
    return results


def monte_carlo_worst_path(returns: list[float], paths: int = MONTE_CARLO_PATHS,
                           seed: int = 11) -> float:
    """5th-percentile maximum-drawdown trough of trade-order resampling."""
    rng = random.Random(seed)
    troughs: list[float] = []
    for _ in range(paths):
        shuffled = returns[:]
        rng.shuffle(shuffled)
        troughs.append(-max_drawdown(shuffled))
    troughs.sort()
    return troughs[max(0, int(0.05 * len(troughs)) - 1)]


def fdr_gate(p_values_by_candidate: dict[str, float]) -> tuple[set[str], GateResult]:
    """Benjamini-Hochberg across EVERY strategy/parameter combination examined."""
    survivors = benjamini_hochberg(p_values_by_candidate, q=FDR_Q)
    outcome = GateOutcome.PASS if survivors else GateOutcome.FAIL
    return survivors, GateResult(
        "bh_fdr",
        outcome,
        f"{len(survivors)}/{len(p_values_by_candidate)} candidates survive q={FDR_Q}",
    )


def shadow_gate(sessions: int, signals: int, parity_pct: float,
                expectancy_at_live_spreads: float) -> list[GateResult]:
    out: list[GateResult] = []
    if sessions < MIN_SHADOW_SESSIONS or signals < MIN_SHADOW_SIGNALS:
        out.append(GateResult(
            "shadow_volume", GateOutcome.INSUFFICIENT_DATA,
            f"{sessions} sessions / {signals} signals "
            f"(need >= {MIN_SHADOW_SESSIONS} / {MIN_SHADOW_SIGNALS})",
        ))
        return out
    out.append(GateResult("shadow_volume", GateOutcome.PASS,
                          f"{sessions} sessions, {signals} signals"))
    out.append(GateResult(
        "signal_parity",
        GateOutcome.PASS if parity_pct >= 100.0 else GateOutcome.FAIL,
        f"golden-file parity {parity_pct:.1f}% (must be 100%)",
    ))
    out.append(GateResult(
        "shadow_expectancy",
        GateOutcome.PASS if expectancy_at_live_spreads > 0 else GateOutcome.FAIL,
        f"expectancy at recorded live spreads {expectancy_at_live_spreads:.4f}R",
    ))
    return out


def demo_gate(fills: int, slippage_within_model_x: float, clean_restarts: int,
              critical_incidents: int) -> list[GateResult]:
    out: list[GateResult] = []
    if fills < MIN_DEMO_FILLS:
        out.append(GateResult(
            "demo_fills", GateOutcome.INSUFFICIENT_DATA,
            f"{fills} fills < {MIN_DEMO_FILLS}",
        ))
        return out
    out.append(GateResult("demo_fills", GateOutcome.PASS, f"{fills} fills"))
    out.append(GateResult(
        "demo_slippage",
        GateOutcome.PASS if slippage_within_model_x <= 1.5 else GateOutcome.FAIL,
        f"slippage at {slippage_within_model_x:.2f}x model (max 1.5x)",
    ))
    out.append(GateResult(
        "demo_recovery",
        GateOutcome.PASS if clean_restarts >= 3 and critical_incidents == 0
        else GateOutcome.FAIL,
        f"{clean_restarts} clean restarts, {critical_incidents} critical incidents",
    ))
    return out


def pipeline_verdict(gates: list[GateResult]) -> GateOutcome:
    if any(g.outcome is GateOutcome.INSUFFICIENT_DATA for g in gates):
        return GateOutcome.INSUFFICIENT_DATA
    if all(g.outcome is GateOutcome.PASS for g in gates):
        return GateOutcome.PASS
    return GateOutcome.FAIL
