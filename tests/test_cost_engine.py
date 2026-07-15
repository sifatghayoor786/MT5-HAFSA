"""Cost Engine gate #1: clearance math, RR floor + WR_be, liquidity window,
sessions, rollover/Friday, news blackout incl. stale-calendar fail-closed."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis_velocity.core.config import load_desk_config
from aegis_velocity.cost.calendar import (
    NewsCalendar,
    NewsStatus,
    import_calendar_csv,
    symbol_currencies,
)
from aegis_velocity.cost.engine import (
    CostBurnMeter,
    CostCandidate,
    CostEngine,
    commission_points,
    wr_breakeven,
)
from aegis_velocity.cost.liquidity import SpreadHistory, percentile
from aegis_velocity.cost.sessions import check_session
from aegis_velocity.mt5.sim import default_spec

REPO = Path(__file__).resolve().parents[1]
CFG = load_desk_config(REPO, env={})

# Tuesday 10:30 server time = inside london_open window
IN_SESSION = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)


def _engine(tmp_path: Path, with_history: bool = True, calendar_csv: bool = True) -> CostEngine:
    if calendar_csv:
        csv = tmp_path / "cal.csv"
        csv.write_text(
            "utc_time,currency,impact,title\n"
            f"{(IN_SESSION + timedelta(hours=3)).isoformat()},USD,high,CPI\n"
        )
        import_calendar_csv(csv, tmp_path / "calendar.json")
    calendar = NewsCalendar(tmp_path / "calendar.json", CFG.sessions.news_blackout)
    spreads = SpreadHistory()
    if with_history:
        for i in range(200):
            spreads.record("EURUSD", IN_SESSION - timedelta(minutes=i % 50), 8.0 + (i % 5))
    return CostEngine(CFG.costs, CFG.risk, CFG.sessions, calendar, spreads)


def _candidate(
    tp: int = 100, sl: int = 80, spread: float = 8.0, when: datetime = IN_SESSION
) -> CostCandidate:
    return CostCandidate(
        symbol="EURUSD", sl_points=sl, tp_points=tp, spread_points=spread, server_now=when
    )


def test_commission_conversion_to_points() -> None:
    spec = default_spec("EURUSD")  # tick_value $1/point/lot
    assert commission_points(spec, 3.0) == 6.0  # $3/side -> $6 round trip -> 6 points
    jpy = default_spec("USDJPY")  # tick_value 0.68
    assert abs(commission_points(jpy, 3.0) - 6.0 / 0.68) < 1e-9


def test_cost_gate_passes_when_cleared(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    # cost = 8 spread + 6 commission + 3 slippage prior = 17; k=4 -> tp must be >= 68
    verdict = engine.evaluate(_candidate(tp=150, sl=80), default_spec("EURUSD"))
    assert verdict.ok, verdict.reasons
    assert abs(verdict.cost_points - 17.0) < 1e-9
    assert verdict.cost_multiple > 8.8
    # net RR = (150-17)/(80+17); WR_be displayed alongside
    assert abs(verdict.net_rr - 133.0 / 97.0) < 1e-9
    assert abs(verdict.wr_be - 1.0 / (1.0 + 133.0 / 97.0)) < 1e-9


def test_cost_gate_blocks_tp_below_k_multiple(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    verdict = engine.evaluate(_candidate(tp=60, sl=30), default_spec("EURUSD"))
    assert not verdict.ok
    assert "COST_GATE_FAIL" in verdict.reasons


def test_rr_floor_blocks_negative_economics(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    # clears k (tp 100 >= 68) but net RR = (100-17)/(150+17) = 0.50 < 1.0
    verdict = engine.evaluate(_candidate(tp=100, sl=150), default_spec("EURUSD"))
    assert not verdict.ok
    assert "RR_TOO_LOW" in verdict.reasons
    assert verdict.wr_be > 0.6  # sub-1R economics visible, not hidden


def test_measured_slippage_replaces_prior(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.update_measured_slippage("EURUSD", 6.0)
    verdict = engine.evaluate(_candidate(tp=100, sl=80), default_spec("EURUSD"))
    assert abs(verdict.cost_points - 20.0) < 1e-9  # 8 + 6 + 6


def test_liquidity_window_blocks_wide_spread_and_cold_start(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    wide = engine.evaluate(_candidate(spread=13.0), default_spec("EURUSD"))
    assert "LIQUIDITY_WINDOW" in wide.reasons  # 13 > p40 of 8-12 distribution

    cold = _engine(tmp_path, with_history=False)
    verdict = cold.evaluate(_candidate(), default_spec("EURUSD"))
    assert "LIQUIDITY_WINDOW" in verdict.reasons  # no history -> fail closed


def test_hard_spread_cap(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    verdict = engine.evaluate(_candidate(spread=16.0, tp=300), default_spec("EURUSD"))
    assert "SPREAD_TOO_HIGH" in verdict.reasons  # default cap 15


def test_session_windows_rollover_friday() -> None:
    ok = check_session(CFG.sessions, IN_SESSION)
    assert ok.ok

    lunch = check_session(CFG.sessions, IN_SESSION.replace(hour=14, minute=0))
    assert not lunch.ok and lunch.reason == "SESSION_BLOCKED"

    rollover = check_session(CFG.sessions, IN_SESSION.replace(hour=23, minute=57))
    assert not rollover.ok and rollover.reason == "ROLLOVER_BLACKOUT"
    past_midnight = check_session(CFG.sessions, IN_SESSION.replace(hour=0, minute=5))
    assert not past_midnight.ok and past_midnight.reason == "ROLLOVER_BLACKOUT"

    friday_late = datetime(2026, 7, 17, 21, 0, tzinfo=UTC)  # Friday after 20:00 cutoff
    cut = check_session(CFG.sessions, friday_late)
    assert not cut.ok and cut.reason == "FRIDAY_CUTOFF"

    saturday = datetime(2026, 7, 18, 11, 0, tzinfo=UTC)
    assert not check_session(CFG.sessions, saturday).ok


def test_news_blackout_currency_scoped(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    at_news = IN_SESSION + timedelta(hours=3, minutes=5)  # inside +/-20min of USD high
    verdict = engine.evaluate(_candidate(when=at_news, tp=100, sl=80), default_spec("EURUSD"))
    assert "NEWS_BLACKOUT" in verdict.reasons

    calendar = NewsCalendar(tmp_path / "calendar.json", CFG.sessions.news_blackout)
    # GBPJPY has no USD leg: not affected by USD news
    assert calendar.status("GBPJPY", at_news).status is NewsStatus.OK


def test_stale_or_missing_calendar_blocks(tmp_path: Path) -> None:
    missing = NewsCalendar(tmp_path / "absent.json", CFG.sessions.news_blackout)
    assert missing.status("EURUSD", IN_SESSION).status is NewsStatus.UNKNOWN

    csv = tmp_path / "c.csv"
    csv.write_text("utc_time,currency,impact,title\n2026-07-14T12:30:00Z,USD,high,CPI\n")
    import_calendar_csv(csv, tmp_path / "cal2.json")
    calendar = NewsCalendar(tmp_path / "cal2.json", CFG.sessions.news_blackout)
    much_later = IN_SESSION + timedelta(hours=72)  # beyond stale_calendar_hours=48
    assert calendar.status("EURUSD", much_later).status is NewsStatus.UNKNOWN

    engine = _engine(tmp_path, calendar_csv=False)
    verdict = engine.evaluate(_candidate(tp=100, sl=80), default_spec("EURUSD"))
    assert "NEWS_UNKNOWN_BLOCKED" in verdict.reasons


def test_symbol_currency_extraction() -> None:
    assert symbol_currencies("EURUSD") == ("EUR", "USD")
    assert symbol_currencies("XAUUSD") == ("XAU", "USD")
    assert symbol_currencies("frxGBPJPY") == ("GBP", "JPY")  # normalised


def test_invalid_calendar_impact_rejected(tmp_path: Path) -> None:
    csv = tmp_path / "bad.csv"
    csv.write_text("utc_time,currency,impact,title\n2026-07-14T12:30:00Z,USD,huge,CPI\n")
    with pytest.raises(ValueError, match="impact"):
        import_calendar_csv(csv, tmp_path / "out.json")


def test_percentile_math() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 40) == pytest.approx(40.6)
    assert percentile([5.0], 40) == 5.0


def test_cost_burn_meter_daily_reset() -> None:
    meter = CostBurnMeter()
    meter.record_fill("EURUSD", IN_SESSION, cost_ccy=1.7, gross_pnl_ccy=5.0)
    meter.record_fill("XAUUSD", IN_SESSION, cost_ccy=4.0, gross_pnl_ccy=-2.0)
    assert meter.total_costs == pytest.approx(5.7)
    assert meter.total_gross == pytest.approx(3.0)
    meter.record_fill("EURUSD", IN_SESSION + timedelta(days=1), cost_ccy=1.0, gross_pnl_ccy=0.0)
    assert meter.total_costs == pytest.approx(1.0)  # new day resets


def test_wr_be_edge_cases() -> None:
    assert wr_breakeven(1.0) == 0.5
    assert wr_breakeven(0.0) == 1.0  # zero or negative RR can never break even
