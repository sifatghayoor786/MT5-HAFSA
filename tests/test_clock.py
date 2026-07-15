"""Broker clock: offset, tick freshness, bar-close rule, DST and drift handling."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.core.clock import BrokerClock

T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def test_offset_is_median_of_samples() -> None:
    clk = BrokerClock()
    for off in (7195.0, 7200.0, 7203.0, 7201.0, 9999.0):  # outlier ignored by median
        clk.observe_tick(T0 + timedelta(seconds=off), local_utc=T0)
    assert abs(clk.offset_s - 7201.0) < 1e-9
    assert clk.server_now(T0) == T0 + timedelta(seconds=7201.0)


def test_tick_freshness_on_server_clock() -> None:
    clk = BrokerClock()
    clk.observe_tick(T0 + timedelta(seconds=7200), local_utc=T0)
    fresh_tick = T0 + timedelta(seconds=7200, milliseconds=-300)
    stale_tick = T0 + timedelta(seconds=7200 - 3)
    assert clk.tick_is_fresh(fresh_tick, local_utc=T0)
    assert not clk.tick_is_fresh(stale_tick, local_utc=T0)


def test_bar_close_requires_grace_and_newer_bar() -> None:
    clk = BrokerClock()  # zero offset
    bar_open = T0
    local_at_close = T0 + timedelta(seconds=300)
    # exactly at open+tf: not closed (needs +2s grace)
    assert not clk.bar_is_closed(bar_open, 300, newer_bar_exists=True, local_utc=local_at_close)
    after_grace = T0 + timedelta(seconds=303)
    assert clk.bar_is_closed(bar_open, 300, newer_bar_exists=True, local_utc=after_grace)
    # without a newer bar the bar may still be forming server-side
    assert not clk.bar_is_closed(bar_open, 300, newer_bar_exists=False, local_utc=after_grace)


def test_persist_and_reload(tmp_path: Path) -> None:
    p = tmp_path / "clock.json"
    clk = BrokerClock(persist_path=p)
    clk.observe_tick(T0 + timedelta(seconds=7200), local_utc=T0)
    clk.persist()
    assert json.loads(p.read_text())["offset_s"] == 7200.0
    clk2 = BrokerClock(persist_path=p)
    assert clk2.calibrated and clk2.offset_s == 7200.0


def test_dst_jump_alerts_and_drift_halts(tmp_path: Path) -> None:
    p = tmp_path / "clock.json"
    base = BrokerClock(persist_path=p)
    base.observe_tick(T0 + timedelta(seconds=7200), local_utc=T0)
    base.persist()

    dst = BrokerClock(persist_path=p)
    for i in range(25):  # offset now 3600s different: DST-sized jump
        dst.observe_tick(T0 + timedelta(seconds=10800 + i * 0.001), local_utc=T0)
    assert dst.status().dst_alert
    assert not dst.status().drift_halt

    drift = BrokerClock(persist_path=p)
    for i in range(25):  # 90s jump: broken local clock -> halt
        drift.observe_tick(T0 + timedelta(seconds=7290 + i * 0.001), local_utc=T0)
    assert drift.status().drift_halt
