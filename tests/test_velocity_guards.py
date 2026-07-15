"""Each velocity guard tested independently, including persistence across restarts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.core.anchors import PersistedCounters, PersistedState
from aegis_velocity.core.config import load_desk_config
from aegis_velocity.risk.guards import (
    AntiChurnGuard,
    LossVelocityGuard,
    MicroCooldownGuard,
    OrderStormFuse,
    SlippageBreaker,
    TradeRateGuard,
)

REPO = Path(__file__).resolve().parents[1]
CFG = load_desk_config(REPO, env={}).risk
NOW = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


def _counters(tmp_path: Path) -> PersistedCounters:
    return PersistedCounters(PersistedState(tmp_path / "c.json"))


def test_hourly_and_daily_caps(tmp_path: Path) -> None:
    guard = TradeRateGuard(CFG, _counters(tmp_path))
    for i in range(CFG.max_trades_per_hour_global):
        assert guard.check("EURUSD", NOW).ok or i >= CFG.max_trades_per_symbol_per_hour
        guard.record_trade(f"SYM{i}", NOW)  # different symbols: only global cap binds
    blocked = guard.check("EURUSD", NOW)
    assert not blocked.ok and blocked.reason == "HOURLY_CAP"
    # an hour later the rolling window frees up
    assert guard.check("EURUSD", NOW + timedelta(hours=1, seconds=1)).ok


def test_per_symbol_hourly_cap(tmp_path: Path) -> None:
    guard = TradeRateGuard(CFG, _counters(tmp_path))
    for _ in range(CFG.max_trades_per_symbol_per_hour):
        guard.record_trade("EURUSD", NOW)
    verdict = guard.check("EURUSD", NOW)
    assert not verdict.ok and "EURUSD" in verdict.detail
    assert guard.check("GBPUSD", NOW).ok  # other symbols unaffected


def test_daily_cap_binds_beyond_hourly(tmp_path: Path) -> None:
    guard = TradeRateGuard(CFG, _counters(tmp_path))
    t = NOW
    for i in range(CFG.max_trades_per_day_global):
        guard.record_trade(f"SYM{i % 40}", t)  # rotate symbols: hourly caps never bind
        t += timedelta(minutes=6)
    verdict = guard.check("FRESHSYM", t)
    assert not verdict.ok and verdict.reason == "DAILY_CAP"


def test_loss_velocity_halt_and_pause_persistence(tmp_path: Path) -> None:
    counters = _counters(tmp_path)
    guard = LossVelocityGuard(CFG, counters)
    t = NOW
    for _ in range(6):  # 6R lost inside the hour (config: R_lost=6/60min)
        guard.record_result(-1.0, t)
        t += timedelta(minutes=5)
    verdict = guard.check(t)
    assert not verdict.ok and verdict.reason == "LOSS_VELOCITY_HALT"

    # restart: pause survives via persisted counters
    guard2 = LossVelocityGuard(CFG, PersistedCounters(PersistedState(tmp_path / "c.json")))
    assert not guard2.check(t + timedelta(minutes=30)).ok
    assert guard2.check(t + timedelta(minutes=61)).ok  # pause_minutes=60 elapsed


def test_wins_do_not_trip_loss_velocity(tmp_path: Path) -> None:
    guard = LossVelocityGuard(CFG, _counters(tmp_path))
    for _ in range(20):
        guard.record_result(2.0, NOW)
    assert guard.check(NOW).ok


def test_micro_cooldown_per_symbol_strategy(tmp_path: Path) -> None:
    guard = MicroCooldownGuard(CFG, _counters(tmp_path))
    for _ in range(3):  # 3 consecutive losses
        guard.record_result("EURUSD", "F1", won=False, now=NOW)
    blocked = guard.check("EURUSD", "F1", NOW + timedelta(minutes=5))
    assert not blocked.ok and blocked.reason == "MICRO_COOLDOWN"
    assert guard.check("EURUSD", "F2", NOW).ok  # other strategy unaffected
    assert guard.check("GBPUSD", "F1", NOW).ok  # other symbol unaffected
    assert guard.check("EURUSD", "F1", NOW + timedelta(minutes=16)).ok  # 15min elapsed


def test_win_resets_consecutive_losses(tmp_path: Path) -> None:
    guard = MicroCooldownGuard(CFG, _counters(tmp_path))
    guard.record_result("EURUSD", "F1", won=False, now=NOW)
    guard.record_result("EURUSD", "F1", won=False, now=NOW)
    guard.record_result("EURUSD", "F1", won=True, now=NOW)  # streak broken
    guard.record_result("EURUSD", "F1", won=False, now=NOW)
    assert guard.check("EURUSD", "F1", NOW).ok


def test_anti_churn_same_direction_only(tmp_path: Path) -> None:
    guard = AntiChurnGuard(CFG, _counters(tmp_path))
    guard.record_stopout("EURUSD", "BUY", NOW)
    soon = NOW + timedelta(seconds=30)  # within 90s window
    assert not guard.check("EURUSD", "BUY", soon).ok
    assert guard.check("EURUSD", "SELL", soon).ok  # opposite direction allowed
    assert guard.check("GBPUSD", "BUY", soon).ok  # other symbol allowed
    assert guard.check("EURUSD", "BUY", NOW + timedelta(seconds=91)).ok


def test_order_storm_fuse_latches(tmp_path: Path) -> None:
    fuse = OrderStormFuse(CFG)
    t = NOW
    for _ in range(CFG.order_storm_fuse_per_minute):
        fuse.record_send(t)
        t += timedelta(seconds=1)
    verdict = fuse.check(t)
    assert not verdict.ok and verdict.reason == "ORDER_STORM"
    # latching: still blown 10 minutes later without reset
    assert not fuse.check(t + timedelta(minutes=10)).ok
    fuse.reset()
    assert fuse.check(t + timedelta(minutes=10)).ok


def test_slippage_breaker_trips_on_fading(tmp_path: Path) -> None:
    breaker = SlippageBreaker(CFG)
    model_p50 = 3.0
    for _ in range(CFG.slippage_breaker.window_fills):
        breaker.record_fill("EURUSD", slippage_points=9.0, model_p50_points=model_p50)
    verdict = breaker.check("EURUSD")  # p90=9 > 2.0 x 3.0
    assert not verdict.ok and verdict.reason == "SLIPPAGE_BREAKER"
    assert breaker.check("GBPUSD").ok  # per-symbol scope
    breaker.reset("EURUSD")
    assert breaker.check("EURUSD").ok


def test_slippage_breaker_tolerates_good_fills(tmp_path: Path) -> None:
    breaker = SlippageBreaker(CFG)
    for _ in range(CFG.slippage_breaker.window_fills):
        breaker.record_fill("EURUSD", slippage_points=2.0, model_p50_points=3.0)
    assert breaker.check("EURUSD").ok
