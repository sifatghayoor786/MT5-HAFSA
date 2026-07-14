"""Order-intent state machine (§3.6). Illegal transitions raise; retries are capped."""

from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    PROPOSED = "PROPOSED"
    COST_OK = "COST_OK"
    CONSENSUS_OK = "CONSENSUS_OK"
    RISK_OK = "RISK_OK"
    ARMED = "ARMED"
    CHECKED = "CHECKED"
    SUBMITTED = "SUBMITTED"  # market order sent
    PLACED = "PLACED"  # pending order resting at broker
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    REQUOTED = "REQUOTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.CANCELLED}
)

_PRE_SUBMIT_ABORTS = frozenset({OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED})

ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PROPOSED: frozenset({OrderState.COST_OK}) | _PRE_SUBMIT_ABORTS,
    OrderState.COST_OK: frozenset({OrderState.CONSENSUS_OK}) | _PRE_SUBMIT_ABORTS,
    OrderState.CONSENSUS_OK: frozenset({OrderState.RISK_OK}) | _PRE_SUBMIT_ABORTS,
    OrderState.RISK_OK: frozenset({OrderState.ARMED}) | _PRE_SUBMIT_ABORTS,
    OrderState.ARMED: frozenset({OrderState.CHECKED}) | _PRE_SUBMIT_ABORTS,
    OrderState.CHECKED: frozenset({OrderState.SUBMITTED, OrderState.PLACED})
    | _PRE_SUBMIT_ABORTS,
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.FILLED,
            OrderState.PARTIAL,
            OrderState.REJECTED,
            OrderState.REQUOTED,
            OrderState.UNKNOWN_OUTCOME,
        }
    ),
    OrderState.PLACED: frozenset(
        {
            OrderState.FILLED,
            OrderState.PARTIAL,
            OrderState.EXPIRED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.UNKNOWN_OUTCOME,
        }
    ),
    OrderState.REQUOTED: frozenset({OrderState.CHECKED, OrderState.REJECTED}),
    OrderState.PARTIAL: frozenset({OrderState.FILLED, OrderState.CANCELLED}),
    OrderState.UNKNOWN_OUTCOME: frozenset(
        # resolved ONLY by reconciliation against broker truth
        {OrderState.FILLED, OrderState.PARTIAL, OrderState.REJECTED, OrderState.CANCELLED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}

MAX_REQUOTE_RETRIES = 2


class IllegalTransition(Exception):
    pass


class RetryBudgetExceeded(IllegalTransition):
    pass


class OrderStateMachine:
    def __init__(self, initial: OrderState = OrderState.PROPOSED) -> None:
        self.state = initial
        self.retries = 0
        self.history: list[OrderState] = [initial]

    def transition(self, to: OrderState) -> OrderState:
        if to not in ALLOWED_TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {to.value} is not allowed")
        if to is OrderState.REQUOTED:
            self.retries += 1
            if self.retries > MAX_REQUOTE_RETRIES:
                # budget exhausted: the only legal continuation is REJECTED
                self.state = OrderState.REJECTED
                self.history.append(OrderState.REJECTED)
                raise RetryBudgetExceeded(
                    f"requote retry budget ({MAX_REQUOTE_RETRIES}) exhausted; forced REJECTED"
                )
        self.state = to
        self.history.append(to)
        return self.state

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES
