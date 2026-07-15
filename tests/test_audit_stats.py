"""Wilson CI display rule, PF/expectancy/DD, quarantine math, BH-FDR."""

import pytest

from aegis_velocity.audit.stats import (
    benjamini_hochberg,
    expectancy,
    max_drawdown,
    profit_factor,
    quarantine_check,
    wilson_ci,
)


def test_wilson_ci_known_value() -> None:
    ci = wilson_ci(wins=60, n=100)
    assert ci.rate == pytest.approx(0.60)
    assert ci.low == pytest.approx(0.502, abs=0.002)
    assert ci.high == pytest.approx(0.691, abs=0.002)


def test_wilson_display_rule_insufficient_below_30() -> None:
    small = wilson_ci(10, 20)
    assert not small.sufficient
    assert "insufficient sample" in small.display()
    ok = wilson_ci(20, 40)
    assert ok.sufficient
    assert "95% CI" in ok.display() and "n=40" in ok.display()


def test_pf_expectancy_drawdown() -> None:
    returns = [1.0, -0.5, 2.0, -1.0, 0.5]
    assert profit_factor(returns) == pytest.approx(3.5 / 1.5)
    assert expectancy(returns) == pytest.approx(0.4)
    # path: 1.0, 0.5, 2.5, 1.5, 2.0 -> max dd = 2.5-1.5 = 1.0
    assert max_drawdown(returns) == pytest.approx(1.0)
    assert profit_factor([1.0, 2.0]) == float("inf")
    assert profit_factor([]) == 0.0


def test_quarantine_needs_full_window() -> None:
    verdict = quarantine_check([1.0] * 50, wr_be=0.4, dd_limit_r=10)
    assert not verdict.quarantined
    assert "window not full" in verdict.reasons[0]


def test_quarantine_trips_on_wilson_lb_below_wr_be() -> None:
    # 40 wins of +1R, 60 losses of -1R: LB ~0.305 < WR_be 0.5 (and PF 0.67 < 0.85)
    returns = [1.0] * 40 + [-1.0] * 60
    verdict = quarantine_check(returns, wr_be=0.5, dd_limit_r=100)
    assert verdict.quarantined
    assert any("WILSON_LB" in r for r in verdict.reasons)
    assert any("PF" in r for r in verdict.reasons)


def test_quarantine_trips_on_drawdown_only() -> None:
    # profitable overall but one savage streak: PF fine, DD breaches
    returns = ([2.0] * 40) + ([-1.0] * 12) + ([2.0] * 48)
    verdict = quarantine_check(returns, wr_be=0.2, dd_limit_r=8.0)
    assert verdict.quarantined
    assert any("DD" in r for r in verdict.reasons)
    assert not any("PF" in r for r in verdict.reasons)


def test_healthy_strategy_not_quarantined() -> None:
    returns = [1.5, -1.0] * 50  # 50% WR at +1.5R avg win, losses interleaved
    verdict = quarantine_check(returns, wr_be=0.35, dd_limit_r=20.0)
    assert not verdict.quarantined, verdict.reasons


def test_benjamini_hochberg_known_case() -> None:
    p = {"a": 0.001, "b": 0.008, "c": 0.039, "d": 0.041, "e": 0.20, "f": 0.9}
    survivors = benjamini_hochberg(p, q=0.10)
    # thresholds: 0.0167, 0.0333, 0.05, 0.0667, 0.0833, 0.10
    assert survivors == {"a", "b", "c", "d"}
    assert benjamini_hochberg({}, q=0.1) == set()
    assert benjamini_hochberg({"x": 0.5}, q=0.1) == set()
