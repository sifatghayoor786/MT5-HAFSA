"""Execution engine (§9): retcode-table-driven sends, verified fills only,
Unknown-Outcome Protocol, pending lifecycle with OCO sibling-cancel.

Nothing is ever reported as filled unless verified against positions/deals.
Every attempt (success or failure) writes a full ExecutionRecord to the ledger
synchronously before returning.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from aegis_velocity.core.events import ExecutionRecord, LatencyWaterfall, Side
from aegis_velocity.core.ledger import Ledger
from aegis_velocity.core.state import OrderState
from aegis_velocity.execution.idempotency import IntentStore
from aegis_velocity.execution.intents import ArmedIntent, next_filling
from aegis_velocity.mt5 import retcodes as rc
from aegis_velocity.mt5.gateway import P1_ORDER, P2_ACCOUNT, P3_DATA, Mt5Gateway
from aegis_velocity.mt5.protocol import (
    OrderRequest,
    OrderResultData,
    RequestAction,
    Tick,
)
from aegis_velocity.mt5.retcodes import RetcodeAction, action_for, retcode_name
from aegis_velocity.risk.guards import OrderStormFuse

log = logging.getLogger(__name__)

MAX_TOTAL_RETRIES = 2  # on top of the first attempt

NowFn = Callable[[], datetime]
HaltFn = Callable[[str], None]


@dataclass
class ExecutionOutcome:
    state: OrderState
    record: ExecutionRecord
    halt_reason: str = ""  # non-empty when the desk must halt entries


@dataclass
class ExecutionEngine:
    gateway: Mt5Gateway
    ledger: Ledger
    intents: IntentStore
    storm_fuse: OrderStormFuse
    now_fn: NowFn
    on_halt: HaltFn | None = None
    _oco_map: dict[int, int] = field(default_factory=dict)  # ticket -> sibling
    _oco_cancelled: set[int] = field(default_factory=set)

    # ------------------------------------------------------------- market path

    def execute_market(self, intent: ArmedIntent) -> ExecutionOutcome:
        claimed = self._claim(intent)
        if claimed is not None:
            return claimed
        now = self.now_fn()
        fuse = self.storm_fuse.check(now)
        if not fuse.ok:
            return self._reject(intent, "ORDER_STORM", fuse.detail)

        attempt = 0
        stops_recomputed = False
        filling_switched = False
        extra_retry_used = False
        current = intent

        while True:
            attempt += 1
            self.storm_fuse.record_send(self.now_fn())
            t_send = time.perf_counter()
            try:
                result: OrderResultData | None = self.gateway.call(
                    P1_ORDER, "order_send", current.request
                )
            except Exception as exc:  # gateway timeout == no result
                log.warning("order_send raised: %s", exc)
                result = None
            ack_ms = (time.perf_counter() - t_send) * 1000.0
            retcode = result.retcode if result is not None else None
            action = action_for(retcode)

            if action is RetcodeAction.VERIFY_FILL:
                return self._verify_and_record(current, result, attempt, ack_ms)

            if action is RetcodeAction.ACCEPT_PARTIAL:
                assert result is not None
                record = self._record(
                    current, result, attempt, ack_ms, OrderState.PARTIAL, verified=True
                )
                self.intents.set_state(
                    current.key, OrderState.PARTIAL, ticket=result.order,
                    volume=result.volume,
                )
                # NEVER chase the remainder; risk is recomputed by the caller
                return ExecutionOutcome(OrderState.PARTIAL, record)

            if action is RetcodeAction.UNKNOWN_OUTCOME:
                record = self._record(
                    current, result, attempt, ack_ms, OrderState.UNKNOWN_OUTCOME,
                    verified=False,
                )
                self.intents.set_state(current.key, OrderState.UNKNOWN_OUTCOME)
                resolved = self.resolve_unknown(current)
                return ExecutionOutcome(resolved, record)

            if action is RetcodeAction.RETRY_REFRESH and attempt <= MAX_TOTAL_RETRIES:
                refreshed = self._refresh(current)
                if refreshed is None:
                    return self._reject(current, "DATA_STALE", "no fresh tick for retry")
                current = refreshed
                continue

            if action is RetcodeAction.RECOMPUTE_STOPS_ONCE and not stops_recomputed:
                stops_recomputed = True
                adjusted = self._widen_stops(current)
                if adjusted is not None:
                    current = adjusted
                    continue
                return self._reject(current, "INVALID_STOPS", "cannot satisfy stops_level")

            if action is RetcodeAction.RETRY_ONCE and not extra_retry_used:
                extra_retry_used = True
                refreshed = self._refresh(current)
                if refreshed is not None:
                    current = refreshed
                    continue
                return self._reject(current, "REJECTED", "re-check failed after 10006")

            if action is RetcodeAction.SWITCH_FILLING_ONCE and not filling_switched:
                filling_switched = True
                nxt = next_filling(current.spec, current.request.type_filling)
                if nxt is not None:
                    current = replace(
                        current, request=replace(current.request, type_filling=nxt)
                    )
                    continue
                return self._reject(current, "INVALID_FILL", "filling ladder exhausted")

            if action is RetcodeAction.REJECT_HALT_ENTRIES:
                self._halt("NO_MONEY")
                return self._reject(current, "NO_MONEY", "halting entries for risk review")

            if action is RetcodeAction.HALT_TRADING_DISABLED:
                self._halt("TRADING_DISABLED")
                return self._reject(
                    current,
                    "TRADING_DISABLED",
                    "enable Algo Trading in the terminal / check server permissions",
                )

            if action is RetcodeAction.BACKOFF_STORM_CHECK:
                self.gateway.apply_backoff(1.0 * attempt)
                return self._reject(current, "TOO_MANY_REQUESTS", "backing off")

            if action is RetcodeAction.DEFER_DATA_CHECK:
                return self._reject(
                    current, "DEFERRED", f"{retcode_name(retcode)}: data integrity check"
                )

            if action is RetcodeAction.REJECT_QUARANTINE:
                self._halt(f"QUARANTINE:{current.signal.strategy_id}")
                return self._reject(current, "INVALID_REQUEST_BUG", "strategy quarantined")

            if action is RetcodeAction.REJECT_REFRESH_SPECS:
                return self._reject(current, "SPEC_MISMATCH", "INVALID_VOLUME: refresh specs")

            # retry budget exhausted or unhandled
            return self._reject(
                current, "RETRY_BUDGET", f"{retcode_name(retcode)} after {attempt} attempts"
            )

    # ------------------------------------------------------------ pending path

    def place_pending(self, intent: ArmedIntent) -> ExecutionOutcome:
        claimed = self._claim(intent)
        if claimed is not None:
            return claimed
        now = self.now_fn()
        fuse = self.storm_fuse.check(now)
        if not fuse.ok:
            return self._reject(intent, "ORDER_STORM", fuse.detail)
        self.storm_fuse.record_send(now)
        t_send = time.perf_counter()
        try:
            result: OrderResultData | None = self.gateway.call(
                P1_ORDER, "order_send", intent.request
            )
        except Exception:
            result = None
        ack_ms = (time.perf_counter() - t_send) * 1000.0
        retcode = result.retcode if result is not None else None
        action = action_for(retcode)
        if action is RetcodeAction.VERIFY_FILL:
            assert result is not None
            orders = self.gateway.call(P2_ACCOUNT, "orders_get", intent.signal.symbol)
            verified = any(o.ticket == result.order for o in orders)
            state = OrderState.PLACED if verified else OrderState.UNKNOWN_OUTCOME
            record = self._record(intent, result, 1, ack_ms, state, verified=verified)
            self.intents.set_state(intent.key, state, ticket=result.order)
            return ExecutionOutcome(state, record)
        if action is RetcodeAction.UNKNOWN_OUTCOME:
            record = self._record(
                intent, result, 1, ack_ms, OrderState.UNKNOWN_OUTCOME, verified=False
            )
            self.intents.set_state(intent.key, OrderState.UNKNOWN_OUTCOME)
            resolved = self.resolve_unknown(intent)
            return ExecutionOutcome(resolved, record)
        return self._reject(intent, retcode_name(retcode), "pending placement failed")

    def cancel_pending(self, key: str, ticket: int, symbol: str, reason: str) -> bool:
        result = self.gateway.call(
            P1_ORDER,
            "order_send",
            OrderRequest(action=RequestAction.REMOVE, symbol=symbol, order_ticket=ticket),
        )
        ok = result is not None and result.retcode == rc.DONE
        if ok:
            self.intents.set_state(key, OrderState.CANCELLED)
        self.ledger.append(
            "pending_cancel",
            {"key": key, "ticket": ticket, "reason": reason, "ok": ok},
            correlation_id=key,
        )
        return ok

    # ------------------------------------------------------------- OCO backstop

    def register_oco(self, ticket_a: int, ticket_b: int) -> None:
        self._oco_map[ticket_a] = ticket_b
        self._oco_map[ticket_b] = ticket_a

    def on_fill_detected(self, filled_ticket: int, symbol: str) -> None:
        """Python-side OCO backstop (EA is primary). Race-safe: cancels once."""
        sibling = self._oco_map.pop(filled_ticket, None)
        if sibling is None or sibling in self._oco_cancelled:
            return
        self._oco_map.pop(sibling, None)
        self._oco_cancelled.add(sibling)
        still_open = any(
            o.ticket == sibling
            for o in self.gateway.call(P2_ACCOUNT, "orders_get", symbol)
        )
        if still_open:
            self.gateway.call(
                P1_ORDER,
                "order_send",
                OrderRequest(action=RequestAction.REMOVE, symbol=symbol, order_ticket=sibling),
            )
        self.ledger.append(
            "oco_sibling_cancel",
            {"filled": filled_ticket, "cancelled": sibling, "was_open": still_open},
        )

    # -------------------------------------------------------- unknown outcomes

    def resolve_unknown(self, intent: ArmedIntent) -> OrderState:
        """Reconcile-before-resend: broker truth by comment + magic. Only a
        provably-absent intent may ever be re-evaluated (by the caller, at
        fresh price, through every gate again)."""
        symbol = intent.signal.symbol
        comment = intent.request.comment
        magic = intent.request.magic
        positions = self.gateway.call(P2_ACCOUNT, "positions_get", symbol)
        for p in positions:
            if p.comment == comment and p.magic == magic:
                self.intents.set_state(intent.key, OrderState.FILLED, ticket=p.ticket,
                                       volume=p.volume)
                self.ledger.append(
                    "unknown_outcome_resolved",
                    {"key": intent.key, "found": "position", "ticket": p.ticket},
                    correlation_id=intent.key,
                )
                return OrderState.FILLED
        orders = self.gateway.call(P2_ACCOUNT, "orders_get", symbol)
        for o in orders:
            if o.comment == comment and o.magic == magic:
                self.intents.set_state(intent.key, OrderState.PLACED, ticket=o.ticket)
                self.ledger.append(
                    "unknown_outcome_resolved",
                    {"key": intent.key, "found": "pending", "ticket": o.ticket},
                    correlation_id=intent.key,
                )
                return OrderState.PLACED
        deals = self.gateway.call(
            P2_ACCOUNT,
            "history_deals_get",
            self.now_fn() - timedelta(hours=24),
            self.now_fn() + timedelta(minutes=5),
        )
        for d in deals:
            if d.comment == comment and d.magic == magic:
                self.intents.set_state(intent.key, OrderState.FILLED, ticket=d.position_id,
                                       volume=d.volume)
                self.ledger.append(
                    "unknown_outcome_resolved",
                    {"key": intent.key, "found": "deal", "ticket": d.ticket},
                    correlation_id=intent.key,
                )
                return OrderState.FILLED
        # provably absent
        self.intents.set_state(intent.key, OrderState.CANCELLED)
        self.ledger.append(
            "unknown_outcome_resolved",
            {"key": intent.key, "found": "absent", "note": "re-evaluation permitted"},
            correlation_id=intent.key,
        )
        return OrderState.CANCELLED

    # ---------------------------------------------------------------- helpers

    def _verify_and_record(
        self, intent: ArmedIntent, result: OrderResultData | None, attempt: int, ack_ms: float
    ) -> ExecutionOutcome:
        assert result is not None
        t0 = time.perf_counter()
        positions = self.gateway.call(P2_ACCOUNT, "positions_get", intent.signal.symbol)
        verified = any(
            p.comment == intent.request.comment and p.magic == intent.request.magic
            for p in positions
        )
        verify_ms = (time.perf_counter() - t0) * 1000.0
        if not verified:
            record = self._record(
                intent, result, attempt, ack_ms, OrderState.UNKNOWN_OUTCOME, verified=False
            )
            self.intents.set_state(intent.key, OrderState.UNKNOWN_OUTCOME)
            resolved = self.resolve_unknown(intent)
            return ExecutionOutcome(resolved, record)
        record = self._record(
            intent, result, attempt, ack_ms, OrderState.FILLED, verified=True,
            verify_ms=verify_ms,
        )
        self.intents.set_state(
            intent.key, OrderState.FILLED, ticket=result.order, volume=result.volume
        )
        return ExecutionOutcome(OrderState.FILLED, record)

    def _refresh(self, intent: ArmedIntent) -> ArmedIntent | None:
        tick = self.gateway.call(P3_DATA, "symbol_info_tick", intent.signal.symbol)
        if not isinstance(tick, Tick):
            return None
        return intent.with_refreshed_price(tick)

    def _widen_stops(self, intent: ArmedIntent) -> ArmedIntent | None:
        spec = intent.spec
        min_dist = (spec.trade_stops_level + 1) * spec.point
        req = intent.request
        is_buy = req.kind.side is Side.BUY
        price = req.price
        sl = req.sl
        tp = req.tp
        if sl and abs(price - sl) < min_dist:
            sl = spec.round_price(price - min_dist if is_buy else price + min_dist)
        if tp and abs(price - tp) < min_dist:
            tp = spec.round_price(price + min_dist if is_buy else price - min_dist)
        if sl == req.sl and tp == req.tp:
            return None
        return replace(intent, request=replace(req, sl=sl, tp=tp))

    def _record(
        self,
        intent: ArmedIntent,
        result: OrderResultData | None,
        attempt: int,
        ack_ms: float,
        outcome: OrderState,
        verified: bool,
        verify_ms: float = 0.0,
    ) -> ExecutionRecord:
        req = intent.request
        slippage_pts = 0.0
        if result is not None and result.price and req.price:
            direction = 1.0 if req.kind.side is Side.BUY else -1.0
            slippage_pts = direction * (result.price - req.price) / intent.spec.point
        record = ExecutionRecord(
            correlation_id=intent.key,
            intent_key=intent.key,
            symbol=req.symbol,
            side=req.kind.side,
            kind="pending" if req.action is RequestAction.PENDING else "market",
            request={
                "action": req.action.value,
                "symbol": req.symbol,
                "volume": req.volume,
                "kind": req.kind.value,
                "price": req.price,
                "sl": req.sl,
                "tp": req.tp,
                "deviation": req.deviation,
                "magic": req.magic,
                "comment": req.comment,
                "type_filling": req.type_filling.value,
            },
            retcode=result.retcode if result is not None else 0,
            retcode_name=retcode_name(result.retcode if result is not None else None),
            broker_comment=result.comment if result is not None else "",
            order_ticket=result.order if result is not None else 0,
            deal_ticket=result.deal if result is not None else 0,
            requested_price=req.price,
            filled_price=result.price if result is not None else 0.0,
            requested_volume=req.volume,
            filled_volume=result.volume if result is not None else 0.0,
            slippage_points=slippage_pts,
            attempt=attempt,
            outcome=outcome.value,
            verified=verified,
            latency=LatencyWaterfall(
                send_to_broker_ack_ms=ack_ms, ack_to_fill_verified_ms=verify_ms
            ),
        )
        # order facts are written synchronously BEFORE returning (write-behind
        # applies to analytics enrichment only, never to facts)
        self.ledger.append("execution", record.model_dump(mode="json"), correlation_id=intent.key)
        return record

    def _claim(self, intent: ArmedIntent) -> ExecutionOutcome | None:
        """Idempotency is enforced HERE: duplicate keys and second in-flight
        intents on a symbol never reach the broker."""
        if self.intents.get(intent.key) is not None:
            return self._reject(
                intent, "DUPLICATE_INTENT", "idempotency key already used",
                update_store=False,
            )
        if not self.intents.try_claim(
            intent.key,
            intent.signal.symbol,
            intent.signal.strategy_id,
            intent.signal.side.value,
            oco_group=intent.signal.oco_group,
        ):
            return self._reject(
                intent, "SYMBOL_IN_FLIGHT", "one in-flight intent per symbol",
                update_store=False,
            )
        return None

    def _reject(
        self, intent: ArmedIntent, reason: str, detail: str, update_store: bool = True
    ) -> ExecutionOutcome:
        if update_store:
            self.intents.set_state(intent.key, OrderState.REJECTED)
        self.ledger.append(
            "execution_reject",
            {"key": intent.key, "reason": reason, "detail": detail},
            correlation_id=intent.key,
        )
        record = ExecutionRecord(
            correlation_id=intent.key,
            intent_key=intent.key,
            symbol=intent.signal.symbol,
            side=intent.signal.side,
            outcome=OrderState.REJECTED.value,
            broker_comment=f"{reason}: {detail}",
        )
        return ExecutionOutcome(OrderState.REJECTED, record, halt_reason="")

    def _halt(self, reason: str) -> None:
        self.ledger.append("halt", {"reason": reason, "source": "execution"})
        if self.on_halt is not None:
            self.on_halt(reason)
