"""Symbol discovery and measured scalp eligibility (§4.1)."""

from pathlib import Path

from aegis_velocity.core.config import ScalpEligibilityCfg
from aegis_velocity.mt5.sim import default_spec
from aegis_velocity.mt5.symbols import (
    discover_symbols,
    load_symbol_map,
    measure_scalp_eligibility,
    persist_symbol_map,
)

CFG = ScalpEligibilityCfg(
    max_stops_level_points=30,
    max_spread_p50_points={"default": 15, "XAUUSD": 40},
    min_ticks_per_minute=10,
)


def test_discovery_exact_suffix_prefix_and_missing() -> None:
    broker = ("EURUSDm", "GBPUSDm", "frxUSDJPY", "XAUUSD", "BTCUSD")
    result = discover_symbols(broker, ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "NZDUSD"])
    assert result.mapping == {
        "EURUSD": "EURUSDm",
        "GBPUSD": "GBPUSDm",
        "USDJPY": "frxUSDJPY",
        "XAUUSD": "XAUUSD",
    }
    assert result.missing == ["NZDUSD"]
    assert result.ambiguous == {}


def test_discovery_ambiguity_is_reported_not_guessed() -> None:
    broker = ("EURUSD.r", "EURUSD_i")
    result = discover_symbols(broker, ["EURUSD"])
    assert "EURUSD" not in result.mapping
    assert result.ambiguous == {"EURUSD": ["EURUSD.r", "EURUSD_i"]}


def test_symbol_map_persistence(tmp_path: Path) -> None:
    persist_symbol_map(tmp_path / "map.json", {"EURUSD": "EURUSDm"})
    assert load_symbol_map(tmp_path / "map.json") == {"EURUSD": "EURUSDm"}
    assert load_symbol_map(tmp_path / "absent.json") == {}


def test_eligibility_pass_with_measurements() -> None:
    v = measure_scalp_eligibility(
        default_spec("EURUSD"), [8.0] * 120, window_minutes=10.0, cfg=CFG
    )
    assert v.eligible
    assert v.spread_p50_points == 8.0
    assert v.ticks_per_minute == 12.0


def test_eligibility_fails_on_wide_spread() -> None:
    v = measure_scalp_eligibility(
        default_spec("EURUSD"), [22.0] * 120, window_minutes=10.0, cfg=CFG
    )
    assert not v.eligible
    assert any("SPREAD_P50" in r for r in v.reasons)


def test_eligibility_fails_on_stops_level() -> None:
    spec = default_spec("EURUSD")
    wide = type(spec)(**{**spec.__dict__, "trade_stops_level": 50})
    v = measure_scalp_eligibility(wide, [8.0] * 120, window_minutes=10.0, cfg=CFG)
    assert not v.eligible
    assert any("STOPS_LEVEL" in r for r in v.reasons)


def test_eligibility_fails_on_tick_rate_and_thin_sample() -> None:
    slow = measure_scalp_eligibility(
        default_spec("EURUSD"), [8.0] * 60, window_minutes=10.0, cfg=CFG
    )
    assert not slow.eligible  # 6 ticks/min < 10
    assert any("TICK_RATE" in r for r in slow.reasons)

    thin = measure_scalp_eligibility(
        default_spec("EURUSD"), [8.0] * 10, window_minutes=1.0, cfg=CFG
    )
    assert not thin.eligible  # fails closed on insufficient sample
    assert any("INSUFFICIENT_SAMPLE" in r for r in thin.reasons)


def test_xauusd_uses_its_own_cap() -> None:
    v = measure_scalp_eligibility(
        default_spec("XAUUSD"), [30.0] * 200, window_minutes=10.0, cfg=CFG
    )
    assert v.eligible  # 30 <= XAUUSD cap 40, would fail default cap 15
