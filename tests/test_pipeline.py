"""Promotion pipeline gates: insufficient data is never a pass; FDR applies."""

import random

from aegis_velocity.pipeline.promotion import (
    GateOutcome,
    WalkForwardEvidence,
    demo_gate,
    fdr_gate,
    pipeline_verdict,
    shadow_gate,
    walkforward_gate,
)


def _good_returns(n: int = 1200, seed: int = 3) -> list[float]:
    rng = random.Random(seed)
    return [1.2 if rng.random() < 0.55 else -1.0 for _ in range(n)]


def test_oos_below_1000_is_insufficient_never_pass() -> None:
    gates = walkforward_gate(WalkForwardEvidence(oos_returns_r=[1.0] * 999))
    assert gates[0].outcome is GateOutcome.INSUFFICIENT_DATA
    assert pipeline_verdict(gates) is GateOutcome.INSUFFICIENT_DATA


def test_good_evidence_passes_all_walkforward_gates() -> None:
    returns = _good_returns()
    evidence = WalkForwardEvidence(
        oos_returns_r=returns,
        base_expectancy=0.2,
        plateau_expectancies={"+20%": 0.18, "-20%": 0.15},
    )
    gates = walkforward_gate(evidence)
    assert pipeline_verdict(gates) is GateOutcome.PASS, [
        (g.name, g.outcome, g.detail) for g in gates
    ]


def test_negative_expectancy_fails_stressed_costs() -> None:
    rng = random.Random(5)
    losers = [0.9 if rng.random() < 0.45 else -1.0 for _ in range(1500)]
    gates = walkforward_gate(WalkForwardEvidence(oos_returns_r=losers))
    stressed = next(g for g in gates if g.name == "stressed_costs")
    assert stressed.outcome is GateOutcome.FAIL
    assert pipeline_verdict(gates) is not GateOutcome.PASS


def test_plateau_failure_detected() -> None:
    evidence = WalkForwardEvidence(
        oos_returns_r=_good_returns(),
        base_expectancy=0.2,
        plateau_expectancies={"+20%": 0.05},  # keeps only 25% of expectancy
    )
    plateau = next(g for g in walkforward_gate(evidence) if g.name == "parameter_plateau")
    assert plateau.outcome is GateOutcome.FAIL


def test_missing_plateau_runs_are_insufficient() -> None:
    evidence = WalkForwardEvidence(oos_returns_r=_good_returns())
    plateau = next(g for g in walkforward_gate(evidence) if g.name == "parameter_plateau")
    assert plateau.outcome is GateOutcome.INSUFFICIENT_DATA


def test_fdr_across_all_examined_candidates() -> None:
    survivors, gate = fdr_gate({"F1/a": 0.001, "F1/b": 0.20, "F2/a": 0.85})
    assert "F1/a" in survivors and "F2/a" not in survivors
    assert gate.outcome is GateOutcome.PASS
    none_survive, gate2 = fdr_gate({"x": 0.5, "y": 0.7})
    assert not none_survive and gate2.outcome is GateOutcome.FAIL


def test_shadow_gate_volume_parity_expectancy() -> None:
    thin = shadow_gate(sessions=2, signals=100, parity_pct=100.0,
                       expectancy_at_live_spreads=0.1)
    assert thin[0].outcome is GateOutcome.INSUFFICIENT_DATA

    broken_parity = shadow_gate(5, 600, parity_pct=99.5, expectancy_at_live_spreads=0.1)
    parity = next(g for g in broken_parity if g.name == "signal_parity")
    assert parity.outcome is GateOutcome.FAIL  # 100% or nothing

    good = shadow_gate(5, 600, 100.0, 0.05)
    assert pipeline_verdict(good) is GateOutcome.PASS


def test_demo_gate_rules() -> None:
    thin = demo_gate(fills=100, slippage_within_model_x=1.0, clean_restarts=3,
                     critical_incidents=0)
    assert thin[0].outcome is GateOutcome.INSUFFICIENT_DATA

    slippy = demo_gate(350, 2.0, 3, 0)
    assert next(g for g in slippy if g.name == "demo_slippage").outcome is GateOutcome.FAIL

    incident = demo_gate(350, 1.2, 3, 1)
    assert next(g for g in incident if g.name == "demo_recovery").outcome is GateOutcome.FAIL

    good = demo_gate(350, 1.2, 4, 0)
    assert pipeline_verdict(good) is GateOutcome.PASS


def test_insufficient_beats_fail_in_verdict() -> None:
    gates = walkforward_gate(WalkForwardEvidence(oos_returns_r=[]))
    assert pipeline_verdict(gates) is GateOutcome.INSUFFICIENT_DATA
