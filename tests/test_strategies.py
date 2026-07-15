"""F1-F5 behaviour: fire on their setups, abstain otherwise, stay deterministic."""

from tests.strategy_scenarios import (
    f1_burst_context,
    f1_quiet_context,
    f2_sweep_context,
    f3_open_context,
    f4_compression_context,
    f4_wide_box_context,
    f5_stoprun_context,
)

from aegis_velocity.core.events import Side
from aegis_velocity.strategies import STRATEGY_REGISTRY


def test_registry_contains_f1_to_f6() -> None:
    assert set(STRATEGY_REGISTRY) == {"F1", "F2", "F3", "F4", "F5", "F6"}


def test_f1_fires_on_burst_and_abstains_when_quiet() -> None:
    signals = STRATEGY_REGISTRY["F1"](f1_burst_context())
    assert len(signals) == 1
    s = signals[0]
    assert s.side is Side.BUY
    assert s.trigger == "tick_armed"
    assert s.sl_points >= 10 and s.tp_points == 2 * s.sl_points
    assert s.strategy_id == "F1" and s.config_hash
    assert STRATEGY_REGISTRY["F1"](f1_quiet_context()) == []


def test_f2_sweep_reclaim_buys_with_sl_beyond_extreme() -> None:
    signals = STRATEGY_REGISTRY["F2"](f2_sweep_context())
    assert len(signals) == 1
    s = signals[0]
    assert s.side is Side.BUY
    ctx = f2_sweep_context()
    # SL must cover the distance back to the sweep extreme (1.09880)
    assert s.sl_points > ctx.to_points(ctx.last_tick.bid - 1.09880) - 2


def test_f3_emits_directional_pending_stop() -> None:
    signals = STRATEGY_REGISTRY["F3"](f3_open_context())
    assert len(signals) == 1
    s = signals[0]
    assert s.trigger == "pending" and s.pending_type == "buy_stop"
    assert s.entry_price > 1.10040  # above the range high plus offset
    assert s.oco_group == ""


def test_f4_straddle_is_a_race_safe_oco_pair() -> None:
    signals = STRATEGY_REGISTRY["F4"](f4_compression_context())
    assert len(signals) == 2
    buy, sell = signals
    assert buy.pending_type == "buy_stop" and sell.pending_type == "sell_stop"
    assert buy.oco_group == sell.oco_group != ""
    assert buy.trigger_id != sell.trigger_id  # distinct idempotency scopes
    assert buy.entry_price > sell.entry_price
    assert STRATEGY_REGISTRY["F4"](f4_wide_box_context()) == []


def test_f5_fades_the_stop_run() -> None:
    signals = STRATEGY_REGISTRY["F5"](f5_stoprun_context())
    assert len(signals) == 1
    s = signals[0]
    assert s.side is Side.SELL
    assert "1.101" in s.reason


def test_f6_scaffold_never_originates() -> None:
    for ctx_factory in (f1_burst_context, f4_compression_context):
        ctx = ctx_factory()
        object.__setattr__(ctx, "strategy_id", "F6")
        assert STRATEGY_REGISTRY["F6"](ctx) == []


def test_thin_windows_abstain_everywhere() -> None:
    for sid, factory in (
        ("F1", f1_burst_context),
        ("F2", f2_sweep_context),
        ("F5", f5_stoprun_context),
    ):
        ctx = factory()
        object.__setattr__(ctx, "ticks", ctx.ticks[:2])
        assert STRATEGY_REGISTRY[sid](ctx) == []


def test_determinism_bit_for_bit() -> None:
    exclude = {"event_id", "ts_utc"}
    for sid, factory in (
        ("F1", f1_burst_context),
        ("F2", f2_sweep_context),
        ("F3", f3_open_context),
        ("F4", f4_compression_context),
        ("F5", f5_stoprun_context),
    ):
        a = [s.model_dump(exclude=exclude, mode="json") for s in STRATEGY_REGISTRY[sid](factory())]
        b = [s.model_dump(exclude=exclude, mode="json") for s in STRATEGY_REGISTRY[sid](factory())]
        assert a == b, f"{sid} is not deterministic"
        assert all(s["trigger_id"] for s in a)
