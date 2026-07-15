"""Consensus council: hard gates reject with reasons; quality maps verdicts;
decision path stays inside the latency budget."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.consensus.council import Council, CouncilInputs
from aegis_velocity.core.clock import BrokerClock
from aegis_velocity.core.config import load_desk_config
from aegis_velocity.core.events import Side, Signal, Verdict
from aegis_velocity.cost.calendar import NewsCalendar, import_calendar_csv
from aegis_velocity.cost.engine import CostEngine
from aegis_velocity.cost.liquidity import SpreadHistory
from aegis_velocity.mt5.protocol import Tick
from aegis_velocity.mt5.sim import default_spec

REPO = Path(__file__).resolve().parents[1]
CFG = load_desk_config(REPO, env={})
NOW = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)


def _council(tmp_path: Path) -> Council:
    csv = tmp_path / "cal.csv"
    csv.write_text("utc_time,currency,impact,title\n2026-07-20T12:30:00Z,USD,high,FOMC\n")
    import_calendar_csv(csv, tmp_path / "cal.json")
    calendar = NewsCalendar(tmp_path / "cal.json", CFG.sessions.news_blackout)
    spreads = SpreadHistory()
    for i in range(200):
        spreads.record("EURUSD", NOW - timedelta(minutes=i % 50), 8.0 + (i % 5))
    engine = CostEngine(CFG.costs, CFG.risk, CFG.sessions, calendar, spreads)
    clock = BrokerClock()
    clock.observe_tick(NOW, local_utc=NOW)  # zero offset; ticks at NOW are fresh
    return Council(cost_engine=engine, risk_cfg=CFG.risk, clock=clock)


def _signal(sl: int = 80, tp: int = 150, age_s: float = 0.5) -> Signal:
    return Signal(
        strategy_id="F1", strategy_version=1, symbol="EURUSD", side=Side.BUY,
        trigger="tick_armed", trigger_id="t1", sl_points=sl, tp_points=tp,
        signal_time_utc=NOW - timedelta(seconds=age_s), tick_time_utc=NOW,
        correlation_id="c-test",
    )


def _tick(spread_points: float = 8.0, age_s: float = 0.0) -> Tick:
    t = NOW - timedelta(seconds=age_s)
    return Tick(time=t, bid=1.10000, ask=1.10000 + spread_points * 0.00001,
                time_msc=int(t.timestamp() * 1000))


def _inputs(tmp_path: Path, **overrides: object) -> CouncilInputs:
    base: dict[str, object] = {
        "signal": _signal(),
        "spec": default_spec("EURUSD"),
        "tick": _tick(),
        "server_now": NOW,
        "data_integrity": 95.0,
        "conflicting_position": False,
        "in_flight_intent": False,
        "strategy_quarantined": False,
        "strategy_reliability": 0.7,
        "recent_slippage_ok": True,
    }
    base.update(overrides)
    return CouncilInputs(**base)  # type: ignore[arg-type]


def test_clean_signal_approved_with_wr_be_carried(tmp_path: Path) -> None:
    result = _council(tmp_path).decide(_inputs(tmp_path))
    assert result.decision.verdict is Verdict.APPROVE, result.decision.reasons
    assert result.decision.wr_be > 0
    assert result.decision.correlation_id == "c-test"  # threads through
    assert result.decision.decision_latency_ms < 10.0  # §3.4 budget


def test_cost_gate_failure_rejects_with_reason(tmp_path: Path) -> None:
    result = _council(tmp_path).decide(_inputs(tmp_path, signal=_signal(sl=80, tp=40)))
    assert result.decision.verdict is Verdict.REJECT
    assert "COST_GATE_FAIL" in result.decision.reasons


def test_data_integrity_gate(tmp_path: Path) -> None:
    result = _council(tmp_path).decide(_inputs(tmp_path, data_integrity=50.0))
    assert result.decision.verdict is Verdict.REJECT
    assert "DATA_STALE" in result.decision.reasons


def test_stale_tick_rejected(tmp_path: Path) -> None:
    result = _council(tmp_path).decide(_inputs(tmp_path, tick=_tick(age_s=2.0)))
    assert result.decision.verdict is Verdict.REJECT
    assert "DATA_STALE" in result.decision.reasons


def test_old_signal_rejected(tmp_path: Path) -> None:
    result = _council(tmp_path).decide(_inputs(tmp_path, signal=_signal(age_s=5.0)))
    assert result.decision.verdict is Verdict.REJECT
    assert "SIGNAL_TOO_OLD" in result.decision.reasons


def test_conflict_and_quarantine_gates(tmp_path: Path) -> None:
    conflict = _council(tmp_path).decide(_inputs(tmp_path, conflicting_position=True))
    assert "CONFLICTING_EXPOSURE" in conflict.decision.reasons
    quarantined = _council(tmp_path).decide(_inputs(tmp_path, strategy_quarantined=True))
    assert "STRATEGY_QUARANTINED" in quarantined.decision.reasons


def test_quality_score_maps_to_reduced_and_shadow(tmp_path: Path) -> None:
    # sl=34/tp=68 clears both cost gates with minimal headroom (multiple = 4.0)
    tight = _signal(sl=34, tp=68)
    reduced = _council(tmp_path).decide(
        _inputs(tmp_path, signal=tight, strategy_reliability=0.25)
    )
    assert reduced.decision.verdict in (Verdict.APPROVE_REDUCED, Verdict.SHADOW_ONLY)
    shadow = _council(tmp_path).decide(
        _inputs(
            tmp_path,
            signal=tight,
            strategy_reliability=0.0,
            recent_slippage_ok=False,
            data_integrity=70.0,
        )
    )
    assert shadow.decision.verdict in (Verdict.SHADOW_ONLY, Verdict.REJECT)
