"""Execution engine: retcode table, retries, unknown-outcome, OCO, idempotency."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.exec_helpers import CFG, ExecHarness

from aegis_velocity.core.state import OrderState
from aegis_velocity.mt5 import retcodes as rc
from aegis_velocity.mt5.protocol import FillingMode
from aegis_velocity.mt5.sim import default_spec


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[ExecHarness]:
    harness = ExecHarness(tmp_path)
    yield harness
    harness.close()


def test_market_fill_is_verified_before_reporting(h: ExecHarness) -> None:
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.FILLED
    assert outcome.record.verified  # §2.5: no fill report without broker verification
    assert outcome.record.retcode == rc.DONE
    row = h.intents.get(outcome.record.intent_key)
    assert row is not None and row.state == "FILLED" and row.ticket > 0
    kinds = [r.kind for r in h.ledger.rows()]
    assert "execution" in kinds  # order fact written synchronously


def test_requote_retries_with_fresh_price_then_fills(h: ExecHarness) -> None:
    h.sim.inject(rc.REQUOTE)
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.FILLED
    assert outcome.record.attempt == 2  # one requote, one successful retry


def test_requote_storm_exhausts_retry_budget(h: ExecHarness) -> None:
    h.sim.inject(rc.REQUOTE, times=5)
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.REJECTED
    assert h.sim.send_count == 3  # initial + exactly 2 retries, never more


def test_unknown_outcome_resolves_to_fill_when_broker_executed(h: ExecHarness) -> None:
    h.sim.inject(None, execute_anyway=True)  # timeout, but the broker filled it
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.FILLED  # reconciled by comment+magic
    row = h.intents.get(outcome.record.intent_key)
    assert row is not None and row.state == "FILLED"
    resolved = [r for r in h.ledger.rows(kind="unknown_outcome_resolved")]
    assert resolved and resolved[0].payload["found"] == "position"


def test_unknown_outcome_provably_absent_allows_reevaluation(h: ExecHarness) -> None:
    h.sim.inject(None, execute_anyway=False)  # timeout, broker did NOT execute
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.CANCELLED
    resolved = [r for r in h.ledger.rows(kind="unknown_outcome_resolved")]
    assert resolved and resolved[0].payload["found"] == "absent"
    assert h.sim.send_count == 1  # NEVER a blind resend


def test_partial_fill_accepted_never_chased(h: ExecHarness) -> None:
    h.sim.partial_fill_fraction = 0.5
    outcome = h.engine.execute_market(h.market_intent(volume=0.10))
    assert outcome.state is OrderState.PARTIAL
    assert outcome.record.filled_volume == pytest.approx(0.05)
    assert h.sim.send_count == 1  # no chase order for the remainder


def test_invalid_stops_recomputed_once(h: ExecHarness) -> None:
    outcome = h.engine.execute_market(h.market_intent(sl_points=5, tp_points=50))
    assert outcome.state is OrderState.FILLED  # widened to stops_level once
    assert h.sim.send_count == 2


def test_filling_ladder_switch_on_10030(tmp_path: Path) -> None:
    spec = default_spec("EURUSD")
    ioc_only = type(spec)(**{**spec.__dict__, "filling_modes": (FillingMode.IOC,)})
    h = ExecHarness(tmp_path, spec=ioc_only)
    try:
        outcome = h.engine.execute_market(h.market_intent(filling=FillingMode.FOK))
        assert outcome.state is OrderState.FILLED
        assert h.sim.send_count == 2  # FOK rejected 10030, IOC accepted
    finally:
        h.close()


def test_no_money_halts_entries(h: ExecHarness) -> None:
    h.sim.inject(rc.NO_MONEY)
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.REJECTED
    assert "NO_MONEY" in h.halts


def test_trading_disabled_halts_with_fix_hint(h: ExecHarness) -> None:
    h.sim.inject(rc.CLIENT_DISABLES_AT)
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.REJECTED
    assert "TRADING_DISABLED" in h.halts
    rejects = [r for r in h.ledger.rows(kind="execution_reject")]
    assert any("Algo Trading" in str(r.payload["detail"]) for r in rejects)


def test_order_storm_fuse_blocks_sends(h: ExecHarness) -> None:
    for _ in range(CFG.risk.order_storm_fuse_per_minute):
        h.fuse.record_send(h.sim.server_time)
    outcome = h.engine.execute_market(h.market_intent())
    assert outcome.state is OrderState.REJECTED
    assert h.sim.send_count == 0  # blocked BEFORE reaching the broker


def test_pending_placed_and_verified(h: ExecHarness) -> None:
    outcome = h.engine.place_pending(h.pending_intent())
    assert outcome.state is OrderState.PLACED
    assert outcome.record.verified
    row = h.intents.get(outcome.record.intent_key)
    assert row is not None and row.state == "PLACED" and row.ticket > 0


def test_pending_cancel(h: ExecHarness) -> None:
    outcome = h.engine.place_pending(h.pending_intent())
    row = h.intents.get(outcome.record.intent_key)
    assert row is not None
    ok = h.engine.cancel_pending(row.key, row.ticket, "EURUSD", reason="invalidation")
    assert ok
    assert h.sim.orders_get() == []
    assert h.intents.get(row.key).state == "CANCELLED"  # type: ignore[union-attr]


def test_oco_sibling_cancel_race_safe(h: ExecHarness) -> None:
    up = h.engine.place_pending(
        h.pending_intent("oco-up", "buy_stop", 1.10100, oco_group="straddle-1")
    )
    down = h.engine.place_pending(
        h.pending_intent("oco-dn", "sell_stop", 1.09900, oco_group="straddle-1")
    )
    up_row = h.intents.get(up.record.intent_key)
    dn_row = h.intents.get(down.record.intent_key)
    assert up_row is not None and dn_row is not None
    h.engine.register_oco(up_row.ticket, dn_row.ticket)

    h.sim.push_tick("EURUSD", 1.10095, 1.10105)  # triggers the buy stop
    h.engine.on_fill_detected(up_row.ticket, "EURUSD")
    assert h.sim.orders_get() == []  # sibling cancelled

    # duplicate fill notification must be a no-op (race safety)
    before = len(list(h.ledger.rows(kind="oco_sibling_cancel")))
    h.engine.on_fill_detected(up_row.ticket, "EURUSD")
    after = len(list(h.ledger.rows(kind="oco_sibling_cancel")))
    assert after == before


def test_idempotency_duplicate_and_one_in_flight(h: ExecHarness) -> None:
    assert h.intents.try_claim("k1", "EURUSD", "F1", "BUY")
    assert not h.intents.try_claim("k1", "EURUSD", "F1", "BUY")  # duplicate key
    assert not h.intents.try_claim("k2", "EURUSD", "F1", "SELL")  # symbol in flight
    assert h.intents.try_claim("k3", "GBPUSD", "F1", "BUY")  # other symbol fine

    # UNKNOWN_OUTCOME keeps the symbol locked: reconcile before ANY new order
    h.intents.set_state("k1", OrderState.UNKNOWN_OUTCOME)
    assert not h.intents.try_claim("k4", "EURUSD", "F1", "BUY")

    # order lifecycle finished (FILLED = position exists, pipeline done):
    # the symbol frees up; exposure is governed by position caps, not the pipeline
    h.intents.set_state("k1", OrderState.FILLED)
    assert h.intents.try_claim("k5", "EURUSD", "F1", "BUY")


def test_execution_record_slippage_math(h: ExecHarness) -> None:
    h.sim.market_slippage_points = 3
    outcome = h.engine.execute_market(h.market_intent(deviation=10))
    assert outcome.state is OrderState.FILLED
    assert outcome.record.slippage_points == pytest.approx(3.0)
