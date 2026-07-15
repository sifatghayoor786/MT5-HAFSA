"""Risk Commander: dual sizing, floor rounding, min-lot reject, halts, caps, canary."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aegis_velocity.core.anchors import EquityAnchors, PersistedCounters, PersistedState
from aegis_velocity.core.config import TradingMode, load_desk_config
from aegis_velocity.core.events import Side
from aegis_velocity.mt5.sim import SimMt5Client, default_spec
from aegis_velocity.risk.commander import (
    OpenExposure,
    RiskCommander,
    floor_to_step,
)

REPO = Path(__file__).resolve().parents[1]
CFG = load_desk_config(REPO, env={})
NOW = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)


def _commander(tmp_path: Path, mode: TradingMode = TradingMode.SHADOW) -> RiskCommander:
    anchors = EquityAnchors(PersistedState(tmp_path / "anchors.json"))
    anchors.roll(NOW, 100_000.0)
    counters = PersistedCounters(PersistedState(tmp_path / "counters.json"))
    return RiskCommander(CFG.risk, CFG.correlations, anchors, counters, mode)


def _sim() -> SimMt5Client:
    c = SimMt5Client(
        specs={
            "EURUSD": default_spec("EURUSD"),
            "USDJPY": default_spec("USDJPY"),
            "XAUUSD": default_spec("XAUUSD"),
        },
        balance=100_000.0,
    )
    c.initialize("", c.login, "pw", c.server)
    return c


def test_sizing_dual_method_agreement_eurusd(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path), _sim()
    # equity 100k, risk 0.02% = $20; SL 100 points = 100 ticks; $1/tick/lot -> 0.2 lots
    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert result.ok, result.reasons
    assert result.lots == pytest.approx(0.2)
    assert result.risk_ccy == pytest.approx(20.0)
    assert abs(result.tick_math_loss - result.broker_calc_loss) < 1e-9


def test_sizing_jpy_and_xauusd_cases(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path), _sim()
    jpy = rc.size_position(
        default_spec("USDJPY"), Side.SELL, 155.000, 155.100, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    # 100 ticks x 0.68 = $68/lot; $20/68 = 0.294 -> floors to 0.29
    assert jpy.ok, jpy.reasons
    assert jpy.lots == pytest.approx(0.29)
    assert jpy.risk_ccy <= 20.0 + 1e-9  # floor never exceeds target risk

    gold = rc.size_position(
        default_spec("XAUUSD"), Side.BUY, 2400.00, 2398.00, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    # 200 ticks x $1 = $200/lot -> 0.1 lots
    assert gold.ok, gold.reasons
    assert gold.lots == pytest.approx(0.1)


def test_floor_only_rounding_and_min_lot_reject(tmp_path: Path) -> None:
    assert floor_to_step(0.299, 0.01) == pytest.approx(0.29)  # NEVER rounds up
    rc, sim = _commander(tmp_path), _sim()
    # tiny equity: raw lots below volume_min must REJECT, not round up
    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 2_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert not result.ok
    assert "RISK_TOO_SMALL_FOR_MIN_LOT" in result.reasons


def test_spec_mismatch_rejected(tmp_path: Path) -> None:
    rc, _ = _commander(tmp_path), _sim()

    def lying_calc(
        side: Side, symbol: str, volume: float, po: float, pc: float
    ) -> float | None:
        return -1.0  # broker disagrees wildly with tick math

    def margin(side: Side, symbol: str, volume: float, price: float) -> float | None:
        return 100.0

    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        lying_calc, margin, margin_used=0.0,
    )
    assert not result.ok and "SPEC_MISMATCH" in result.reasons

    def none_calc(
        side: Side, symbol: str, volume: float, po: float, pc: float
    ) -> float | None:
        return None  # cannot compute accurate risk => REJECT

    result2 = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        none_calc, margin, margin_used=0.0,
    )
    assert not result2.ok and "SPEC_MISMATCH" in result2.reasons


def test_missing_stop_rejected(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path), _sim()
    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 0.0, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert not result.ok and "NO_STOP" in result.reasons


def test_margin_floor_enforced(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path), _sim()
    # margin_used so high that projected level dips under 500%
    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=19_900.0,
    )
    assert not result.ok and "MARGIN_FLOOR" in result.reasons


def test_halt_ladder_daily_weekly_hard(tmp_path: Path) -> None:
    rc = _commander(tmp_path)
    assert rc.check_halts(100_000.0).ok
    daily = rc.check_halts(98_900.0)  # -1.1% on the day
    assert not daily.ok and daily.reason == "DAILY_LOSS_HALT"
    weekly = rc.check_halts(97_400.0)  # -2.6%
    assert not weekly.ok and weekly.reason == "WEEKLY_LOSS_HALT"
    hard = rc.check_halts(94_000.0)  # -6% from peak
    assert not hard.ok and hard.reason == "HARD_DRAWDOWN"


def test_position_caps(tmp_path: Path) -> None:
    rc = _commander(tmp_path)
    open3 = [OpenExposure("EURUSD", 0.0002)] * 3
    assert not rc.check_position_caps("GBPUSD", open3).ok  # max 3 simultaneous
    one_eur = [OpenExposure("EURUSD", 0.0002)]
    assert not rc.check_position_caps("EURUSD", one_eur).ok  # 1 per symbol
    assert rc.check_position_caps("GBPUSD", one_eur).ok


def test_correlation_weighted_open_risk(tmp_path: Path) -> None:
    rc = _commander(tmp_path)
    # EURUSD<->GBPUSD weight 0.8: weighted 0.0015 + 0.8*0.0035 = 0.0043 > cap 0.0040
    # while raw total 0.0050 stays under max_total_open_risk 0.0060
    open_pos = [OpenExposure("EURUSD", 0.0035)]
    verdict = rc.check_open_risk("GBPUSD", 0.0015, open_pos)
    assert not verdict.ok and verdict.reason == "CORRELATED_EXPOSURE"
    # an uncorrelated candidate with the same numbers passes (weight 0)
    ok = rc.check_open_risk("USDJPY", 0.0015, open_pos)
    assert ok.ok
    # raw total cap still enforced independently
    too_much = rc.check_open_risk("USDJPY", 0.003, [OpenExposure("XAUUSD", 0.0035)])
    assert not too_much.ok and too_much.reason == "MAX_OPEN_RISK"


def test_full_evaluate_collects_all_reasons(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path), _sim()
    sizing = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    verdict = rc.evaluate(
        "EURUSD", "F1", Side.BUY, NOW, 100_000.0, [], sizing
    )
    assert verdict.ok

    # trip anti-churn and re-evaluate
    rc.record_close("EURUSD", "F1", Side.BUY, -1.0, stopped_out=True, now=NOW)
    verdict2 = rc.evaluate("EURUSD", "F1", Side.BUY, NOW, 100_000.0, [], sizing)
    assert not verdict2.ok and "ANTI_CHURN" in verdict2.reasons


def test_canary_forces_min_lot_and_counts_fills(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path, mode=TradingMode.LIVE_CANARY), _sim()
    sizing = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert sizing.ok and sizing.canary
    assert sizing.lots == pytest.approx(0.01)  # volume_min regardless of computed size
    for _ in range(CFG.risk.canary_fills):
        rc.record_fill("EURUSD", NOW)
    assert rc.canary_complete()
    after = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 100_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert not after.canary  # canary exits after the fill quota


def test_canary_skips_trade_when_min_lot_risk_too_big(tmp_path: Path) -> None:
    rc, sim = _commander(tmp_path, mode=TradingMode.LIVE_CANARY), _sim()
    # equity 1000: target risk $0.2; min-lot risk on 100pt stop = $1 > 1.5x target
    result = rc.size_position(
        default_spec("EURUSD"), Side.BUY, 1.10000, 1.09900, 1_000.0,
        sim.order_calc_profit, sim.order_calc_margin, margin_used=0.0,
    )
    assert not result.ok and "CANARY_MIN_LOT_TOO_RISKY" in result.reasons
