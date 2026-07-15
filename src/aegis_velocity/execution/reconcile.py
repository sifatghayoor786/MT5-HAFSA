"""Startup reconciliation (§9): MT5 is the source of truth.

Rebuild positions AND pending orders by magic prefix, match intents by comment
key, adopt orphans (flagged ADOPTED), resolve every UNKNOWN_OUTCOME, enforce SL
presence, detect manual overrides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aegis_velocity.core.ledger import Ledger
from aegis_velocity.core.state import OrderState
from aegis_velocity.execution.idempotency import IntentStore, key_from_comment
from aegis_velocity.mt5.gateway import P1_ORDER, P2_ACCOUNT, P3_DATA, Mt5Gateway
from aegis_velocity.mt5.protocol import OrderRequest, RequestAction


@dataclass
class ReconcileReport:
    positions_matched: int = 0
    pendings_matched: int = 0
    orphans_adopted: list[int] = field(default_factory=list)
    unknown_resolved: dict[str, str] = field(default_factory=dict)
    manual_overrides: list[str] = field(default_factory=list)
    sl_enforced: list[int] = field(default_factory=list)


def _magic_prefix_ok(magic: int, prefix: int = 77) -> bool:
    return magic // 100_000 == prefix


def startup_reconcile(
    gateway: Mt5Gateway,
    ledger: Ledger,
    intents: IntentStore,
    now_fn: Callable[[], datetime],
    fallback_sl_points: int = 100,
    magic_prefix: int = 77,
    on_manual_override: Callable[[str], None] | None = None,
) -> ReconcileReport:
    report = ReconcileReport()
    known_keys = intents.all_keys()

    positions = [
        p for p in gateway.call(P2_ACCOUNT, "positions_get")
        if _magic_prefix_ok(p.magic, magic_prefix)
    ]
    pendings = [
        o for o in gateway.call(P2_ACCOUNT, "orders_get")
        if _magic_prefix_ok(o.magic, magic_prefix)
    ]

    # resolve UNKNOWN_OUTCOME intents FIRST, against broker truth, so their
    # resolutions are reported explicitly rather than absorbed by matching
    deals = gateway.call(
        P2_ACCOUNT, "history_deals_get",
        now_fn() - timedelta(hours=48), now_fn() + timedelta(minutes=5),
    )
    deal_keys = {key_from_comment(d.comment) for d in deals}
    open_keys = {key_from_comment(p.comment) for p in positions}
    pending_keys = {key_from_comment(o.comment) for o in pendings}
    for row in intents.in_state(OrderState.UNKNOWN_OUTCOME):
        if row.key in open_keys or row.key in deal_keys:
            intents.set_state(row.key, OrderState.FILLED)
            report.unknown_resolved[row.key] = "FILLED"
        elif row.key in pending_keys:
            intents.set_state(row.key, OrderState.PLACED)
            report.unknown_resolved[row.key] = "PLACED"
        else:
            intents.set_state(row.key, OrderState.CANCELLED)
            report.unknown_resolved[row.key] = "ABSENT"
        ledger.append(
            "unknown_outcome_resolved",
            {"key": row.key, "resolution": report.unknown_resolved[row.key],
             "source": "startup_reconcile"},
            correlation_id=row.key,
        )

    live_tickets: set[int] = set()
    for p in positions:
        live_tickets.add(p.ticket)
        key = key_from_comment(p.comment)
        if key is not None and key in known_keys:
            report.positions_matched += 1
            intents.set_state(key, OrderState.FILLED, ticket=p.ticket, volume=p.volume)
        else:
            # orphan with our magic: adopt, never abandon a live position
            report.orphans_adopted.append(p.ticket)
            ledger.append(
                "adopted_position",
                {"ticket": p.ticket, "symbol": p.symbol, "comment": p.comment,
                 "flag": "ADOPTED"},
            )
        if p.sl == 0.0:
            spec = gateway.call(P3_DATA, "symbol_info", p.symbol)
            point = spec.point if spec is not None else 0.0001
            sl = (
                p.price_open - fallback_sl_points * point
                if p.side.value == "BUY"
                else p.price_open + fallback_sl_points * point
            )
            gateway.call(
                P1_ORDER,
                "order_send",
                OrderRequest(
                    action=RequestAction.SLTP, symbol=p.symbol, sl=sl, tp=p.tp,
                    position_ticket=p.ticket,
                ),
            )
            report.sl_enforced.append(p.ticket)
            ledger.append("sl_enforced", {"ticket": p.ticket, "sl": sl})

    for o in pendings:
        live_tickets.add(o.ticket)
        key = key_from_comment(o.comment)
        if key is not None and key in known_keys:
            report.pendings_matched += 1
            intents.set_state(key, OrderState.PLACED, ticket=o.ticket)
        else:
            report.orphans_adopted.append(o.ticket)
            ledger.append(
                "adopted_pending",
                {"ticket": o.ticket, "symbol": o.symbol, "comment": o.comment,
                 "flag": "ADOPTED"},
            )

    # manual-override detection: an intent we believe is live but the broker
    # no longer shows. A closing OUT deal carrying OUR magic = normal exit
    # (SL/TP/EA); anything else means someone intervened.
    for row in intents.in_state(OrderState.FILLED, OrderState.PLACED):
        if row.ticket and row.ticket not in live_tickets:
            ours_closed = any(
                d.position_id == row.ticket
                and d.entry == "OUT"
                and _magic_prefix_ok(d.magic, magic_prefix)
                for d in deals
            )
            if not ours_closed:
                report.manual_overrides.append(row.key)
                intents.set_state(row.key, OrderState.CANCELLED)
                ledger.append(
                    "manual_override",
                    {"key": row.key, "ticket": row.ticket, "symbol": row.symbol,
                     "action": "pause symbol x strategy"},
                    correlation_id=row.key,
                )
                if on_manual_override is not None:
                    on_manual_override(f"{row.symbol}:{row.strategy_id}")
            else:
                intents.set_state(row.key, OrderState.FILLED)

    ledger.append(
        "startup_reconcile",
        {
            "positions_matched": report.positions_matched,
            "pendings_matched": report.pendings_matched,
            "orphans_adopted": report.orphans_adopted,
            "unknown_resolved": report.unknown_resolved,
            "manual_overrides": report.manual_overrides,
            "sl_enforced": report.sl_enforced,
        },
    )
    return report
