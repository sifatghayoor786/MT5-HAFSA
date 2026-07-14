"""Order state machine: legal path, illegal transitions, retry budget."""

import pytest

from aegis_velocity.core.state import (
    IllegalTransition,
    OrderState,
    OrderStateMachine,
    RetryBudgetExceeded,
)


def test_full_happy_path_market() -> None:
    m = OrderStateMachine()
    for s in (
        OrderState.COST_OK,
        OrderState.CONSENSUS_OK,
        OrderState.RISK_OK,
        OrderState.ARMED,
        OrderState.CHECKED,
        OrderState.SUBMITTED,
        OrderState.FILLED,
    ):
        m.transition(s)
    assert m.terminal


def test_pending_path_with_expiry() -> None:
    m = OrderStateMachine()
    for s in (
        OrderState.COST_OK,
        OrderState.CONSENSUS_OK,
        OrderState.RISK_OK,
        OrderState.ARMED,
        OrderState.CHECKED,
        OrderState.PLACED,
        OrderState.EXPIRED,
    ):
        m.transition(s)
    assert m.terminal


def test_cannot_skip_gates() -> None:
    m = OrderStateMachine()
    with pytest.raises(IllegalTransition):
        m.transition(OrderState.SUBMITTED)  # PROPOSED -> SUBMITTED skips every gate
    with pytest.raises(IllegalTransition):
        m.transition(OrderState.FILLED)


def test_filled_is_immutable() -> None:
    m = OrderStateMachine(OrderState.SUBMITTED)
    m.transition(OrderState.FILLED)
    with pytest.raises(IllegalTransition):
        m.transition(OrderState.CANCELLED)


def test_requote_retry_budget_forces_reject() -> None:
    m = OrderStateMachine(OrderState.SUBMITTED)
    m.transition(OrderState.REQUOTED)
    m.transition(OrderState.CHECKED)
    m.transition(OrderState.SUBMITTED)
    m.transition(OrderState.REQUOTED)  # retry 2 (max)
    m.transition(OrderState.CHECKED)
    m.transition(OrderState.SUBMITTED)
    with pytest.raises(RetryBudgetExceeded):
        m.transition(OrderState.REQUOTED)  # third retry refused
    assert m.state is OrderState.REJECTED


def test_unknown_outcome_resolves_only_to_broker_truth() -> None:
    m = OrderStateMachine(OrderState.SUBMITTED)
    m.transition(OrderState.UNKNOWN_OUTCOME)
    with pytest.raises(IllegalTransition):
        m.transition(OrderState.SUBMITTED)  # never resend from UNKNOWN
    m.transition(OrderState.FILLED)
    assert m.terminal


def test_partial_accepts_fill_never_chases() -> None:
    m = OrderStateMachine(OrderState.SUBMITTED)
    m.transition(OrderState.PARTIAL)
    with pytest.raises(IllegalTransition):
        m.transition(OrderState.SUBMITTED)  # chasing the remainder is illegal
