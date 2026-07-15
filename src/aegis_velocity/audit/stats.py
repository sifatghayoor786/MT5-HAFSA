"""Statistical honesty primitives (§2.3, §10).

Win rates always ship with n and a Wilson 95% CI; n<30 renders as
"insufficient sample". Environments are never merged. Quarantine math is exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_DISPLAY_SAMPLE = 30
QUARANTINE_WINDOW = 100
QUARANTINE_PF_FLOOR = 0.85

Z95 = 1.959963984540054


@dataclass(frozen=True)
class WilsonCI:
    n: int
    wins: int
    rate: float
    low: float
    high: float

    @property
    def sufficient(self) -> bool:
        return self.n >= MIN_DISPLAY_SAMPLE

    def display(self) -> str:
        if not self.sufficient:
            return f"insufficient sample (n={self.n})"
        return f"{self.rate:.1%} (n={self.n}, 95% CI {self.low:.1%}-{self.high:.1%})"


def wilson_ci(wins: int, n: int, z: float = Z95) -> WilsonCI:
    if n <= 0:
        return WilsonCI(0, 0, 0.0, 0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return WilsonCI(n=n, wins=wins, rate=p, low=max(0.0, centre - margin),
                    high=min(1.0, centre + margin))


def profit_factor(returns: list[float]) -> float:
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def expectancy(returns: list[float]) -> float:
    return sum(returns) / len(returns) if returns else 0.0


def max_drawdown(returns: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative return path (R units)."""
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


@dataclass(frozen=True)
class QuarantineVerdict:
    quarantined: bool
    reasons: list[str]
    window_n: int


def quarantine_check(
    recent_returns_r: list[float],
    wr_be: float,
    dd_limit_r: float,
    window: int = QUARANTINE_WINDOW,
) -> QuarantineVerdict:
    """§10 exact rule over the rolling last `window` closed demo/live trades:
    Wilson 95% LB < cost-implied WR_be, OR rolling PF < 0.85, OR DD > limit."""
    reasons: list[str] = []
    recent = recent_returns_r[-window:]
    n = len(recent)
    if n < window:
        return QuarantineVerdict(False, [f"window not full ({n}/{window})"], n)
    wins = sum(1 for r in recent if r > 0)
    ci = wilson_ci(wins, n)
    if ci.low < wr_be:
        reasons.append(f"WILSON_LB {ci.low:.3f} < WR_be {wr_be:.3f}")
    pf = profit_factor(recent)
    if pf < QUARANTINE_PF_FLOOR:
        reasons.append(f"PF {pf:.2f} < {QUARANTINE_PF_FLOOR}")
    dd = max_drawdown(recent)
    if dd > dd_limit_r:
        reasons.append(f"DD {dd:.1f}R > walk-forward p95 {dd_limit_r:.1f}R")
    return QuarantineVerdict(bool(reasons), reasons, n)


def benjamini_hochberg(p_values: dict[str, float], q: float = 0.10) -> set[str]:
    """BH-FDR: returns the identifiers whose discoveries survive at level q."""
    if not p_values:
        return set()
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(ordered)
    cutoff_rank = 0
    for rank, (_, p) in enumerate(ordered, start=1):
        if p <= q * rank / m:
            cutoff_rank = rank
    return {name for name, _ in ordered[:cutoff_rank]}
