"""Equity anchors and counters survive restarts; drawdown math is anchored."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.core.anchors import EquityAnchors, PersistedCounters, PersistedState

MON = datetime(2026, 7, 13, 0, 0, 5, tzinfo=UTC)  # Monday


def test_daily_and_weekly_anchor_roll_and_drawdown(tmp_path: Path) -> None:
    store = PersistedState(tmp_path / "anchors.json")
    anchors = EquityAnchors(store)
    anchors.roll(MON, equity=10_000.0)
    st = anchors.status(equity=9_900.0)
    assert abs(st.daily_loss_frac - 0.01) < 1e-9
    assert abs(st.weekly_loss_frac - 0.01) < 1e-9

    # next day: daily anchor rolls to current equity, weekly stays at Monday's
    anchors.roll(MON + timedelta(days=1), equity=9_900.0)
    st2 = anchors.status(equity=9_800.0)
    assert abs(st2.daily_loss_frac - (100.0 / 9_900.0)) < 1e-9
    assert abs(st2.weekly_loss_frac - (200.0 / 10_000.0)) < 1e-9


def test_anchors_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "anchors.json"
    anchors = EquityAnchors(PersistedState(path))
    anchors.roll(MON, equity=5_000.0)

    reloaded = EquityAnchors(PersistedState(path))
    assert reloaded.initialized
    st = reloaded.status(equity=4_750.0)
    assert abs(st.daily_loss_frac - 0.05) < 1e-9


def test_peak_drawdown_tracks_high_water(tmp_path: Path) -> None:
    anchors = EquityAnchors(PersistedState(tmp_path / "a.json"))
    anchors.roll(MON, 10_000.0)
    anchors.roll(MON + timedelta(hours=1), 11_000.0)  # new peak
    st = anchors.status(equity=10_400.0)
    assert abs(st.peak_drawdown_frac - (600.0 / 11_000.0)) < 1e-9


def test_counters_window_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "counters.json"
    c = PersistedCounters(PersistedState(path))
    now = MON
    for i in range(5):
        c.record_event("trades_global", now + timedelta(minutes=i * 10))
    at = now + timedelta(minutes=50)
    assert c.count_within("trades_global", at, window_s=3600) == 5
    assert c.count_within("trades_global", at, window_s=1500) == 2  # only last 25 min

    c2 = PersistedCounters(PersistedState(path))
    assert c2.count_within("trades_global", at, window_s=3600) == 5


def test_corrupt_state_file_treated_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    store = PersistedState(path)
    assert store.get("anything") is None
    store.set("k", 1)
    assert PersistedState(path).get("k") == 1
