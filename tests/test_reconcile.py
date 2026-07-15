"""Startup reconciliation: MT5 is the source of truth."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.exec_helpers import ExecHarness

from aegis_velocity.core.state import OrderState
from aegis_velocity.execution.reconcile import startup_reconcile
from aegis_velocity.mt5.protocol import OrderKind, OrderRequest, RequestAction


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[ExecHarness]:
    harness = ExecHarness(tmp_path)
    yield harness
    harness.close()


def _reconcile(h: ExecHarness):
    return startup_reconcile(
        gateway=h.gateway,
        ledger=h.ledger,
        intents=h.intents,
        now_fn=lambda: h.sim.server_time,
        on_manual_override=h.halts.append,
    )


def test_known_position_matched_by_comment(h: ExecHarness) -> None:
    outcome = h.engine.execute_market(h.market_intent())
    report = _reconcile(h)
    assert report.positions_matched == 1
    assert report.orphans_adopted == []
    assert outcome.record.intent_key not in report.manual_overrides


def test_orphan_with_our_magic_adopted_and_sl_enforced(h: ExecHarness) -> None:
    # a position appears with our magic but no known intent (e.g. DB lost)
    h.sim.order_send(
        OrderRequest(
            action=RequestAction.DEAL, symbol="EURUSD", volume=0.05, kind=OrderKind.BUY,
            magic=77_001_01, comment="AEG|feedbeef0000",
        )
    )
    report = _reconcile(h)
    assert len(report.orphans_adopted) == 1
    assert len(report.sl_enforced) == 1  # SL was missing: enforced immediately
    positions = h.sim.positions_get("EURUSD")
    assert positions[0].sl > 0
    adopted = list(h.ledger.rows(kind="adopted_position"))
    assert adopted and adopted[0].payload["flag"] == "ADOPTED"


def test_foreign_magic_ignored(h: ExecHarness) -> None:
    h.sim.order_send(
        OrderRequest(
            action=RequestAction.DEAL, symbol="EURUSD", volume=0.05, kind=OrderKind.BUY,
            magic=99_000_01, comment="someone-else", sl=1.09000,
        )
    )
    report = _reconcile(h)
    assert report.orphans_adopted == []


def test_unknown_outcome_resolved_at_startup(h: ExecHarness) -> None:
    # intent recorded as UNKNOWN, broker actually filled it
    h.sim.inject(None, execute_anyway=True)
    intent = h.market_intent("trigX00000001")
    assert h.intents.try_claim(intent.key, "EURUSD", "F1", "BUY")
    result = h.sim.order_send(intent.request)
    assert result is None
    h.intents.set_state(intent.key, OrderState.UNKNOWN_OUTCOME)

    report = _reconcile(h)
    assert report.unknown_resolved.get(intent.key) == "FILLED"

    # and one that is provably absent resolves to ABSENT
    ghost = h.market_intent("trigY00000001")
    assert h.intents.try_claim(ghost.key, "GBPUSD", "F1", "BUY")
    h.intents.set_state(ghost.key, OrderState.UNKNOWN_OUTCOME)
    report2 = _reconcile(h)
    assert report2.unknown_resolved.get(ghost.key) == "ABSENT"


def test_manual_override_detected_and_paused(h: ExecHarness) -> None:
    outcome = h.engine.execute_market(h.market_intent())
    row = h.intents.get(outcome.record.intent_key)
    assert row is not None
    # a human closes the position from the terminal: OUT deal WITHOUT our magic
    pos = h.sim.positions_get("EURUSD")[0]
    del h.sim._positions[pos.ticket]
    from aegis_velocity.mt5.protocol import Deal

    h.sim._deals.append(
        Deal(
            ticket=999999, order=pos.ticket, position_id=pos.ticket, symbol="EURUSD",
            side=pos.side.opposite, entry="OUT", volume=pos.volume, price=1.10050,
            profit=4.0, commission=0.0, magic=0, comment="manual close",
            time=h.sim.server_time,
        )
    )
    report = _reconcile(h)
    assert row.key in report.manual_overrides
    assert any("EURUSD" in halt for halt in h.halts)  # symbol x strategy paused
    override_rows = list(h.ledger.rows(kind="manual_override"))
    assert override_rows


def test_our_sl_close_is_not_an_override(h: ExecHarness) -> None:
    outcome = h.engine.execute_market(h.market_intent(sl_points=50))
    h.sim.push_tick("EURUSD", 1.09940, 1.09950)  # SL hit: sim closes with OUR magic
    report = _reconcile(h)
    assert report.manual_overrides == []
    assert outcome.record.intent_key not in report.manual_overrides


def test_pending_matched_and_report_row_in_ledger(h: ExecHarness) -> None:
    h.engine.place_pending(h.pending_intent())
    report = _reconcile(h)
    assert report.pendings_matched == 1
    rows = list(h.ledger.rows(kind="startup_reconcile"))
    assert rows and rows[0].payload["pendings_matched"] == 1
