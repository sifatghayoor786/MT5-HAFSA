"""Event models. `correlation_id` threads signal → decision → order → deal."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Event(BaseModel):
    event_id: str = Field(default_factory=new_id)
    ts_utc: datetime = Field(default_factory=utc_now)
    correlation_id: str = ""


class Signal(Event):
    """Output of a strategy: fully describes the proposed trade. Never an order."""

    strategy_id: str = ""
    strategy_version: int = 0
    config_hash: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    trigger: Literal["tick_armed", "pending"] = "tick_armed"
    trigger_id: str = ""  # arming-event id, tick-window scoped
    entry_price: float = 0.0  # for pendings: the stop/limit level
    sl_points: int = 0
    tp_points: int = 0
    pending_type: Literal["", "buy_stop", "sell_stop", "buy_limit", "sell_limit"] = ""
    oco_group: str = ""  # non-empty for straddle siblings
    max_hold_s: int = 0
    signal_time_utc: datetime = Field(default_factory=utc_now)
    tick_time_utc: datetime = Field(default_factory=utc_now)
    reason: str = ""


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    APPROVE_REDUCED = "APPROVE_REDUCED"
    SHADOW_ONLY = "SHADOW_ONLY"
    REJECT = "REJECT"


class Decision(Event):
    signal_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    verdict: Verdict = Verdict.REJECT
    reasons: list[str] = Field(default_factory=list)
    quality_score: float = 0.0
    cost_points: float = 0.0
    cost_multiple: float = 0.0
    net_rr: float = 0.0
    wr_be: float = 0.0  # breakeven win rate implied by net RR
    decision_latency_ms: float = 0.0


class HaltReason(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    HARD_DRAWDOWN = "HARD_DRAWDOWN"
    LOSS_VELOCITY = "LOSS_VELOCITY"
    ORDER_STORM = "ORDER_STORM"
    SLIPPAGE_BREAKER = "SLIPPAGE_BREAKER"
    NO_MONEY = "NO_MONEY"
    TRADING_DISABLED = "TRADING_DISABLED"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    BRIDGE_LOSS = "BRIDGE_LOSS"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    MANUAL = "MANUAL"


class Halt(Event):
    reason: HaltReason = HaltReason.MANUAL
    detail: str = ""
    until_utc: datetime | None = None
    scope: str = "GLOBAL"  # GLOBAL or "symbol:strategy"


class LatencyWaterfall(BaseModel):
    """Milliseconds between stages of one execution attempt."""

    signal_to_decision_ms: float = 0.0
    decision_to_send_ms: float = 0.0
    send_to_broker_ack_ms: float = 0.0
    ack_to_fill_verified_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return (
            self.signal_to_decision_ms
            + self.decision_to_send_ms
            + self.send_to_broker_ack_ms
            + self.ack_to_fill_verified_ms
        )


class ExecutionRecord(Event):
    """Full truth of one order_send attempt, success or failure (§2.5)."""

    intent_key: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    kind: Literal["market", "pending", "cancel", "modify", "close"] = "market"
    request: dict[str, object] = Field(default_factory=dict)
    retcode: int = 0
    retcode_name: str = ""
    broker_comment: str = ""
    order_ticket: int = 0
    deal_ticket: int = 0
    position_ticket: int = 0
    requested_price: float = 0.0
    filled_price: float = 0.0
    requested_volume: float = 0.0
    filled_volume: float = 0.0
    slippage_points: float = 0.0
    attempt: int = 1
    outcome: str = ""  # OrderState value after this attempt
    verified: bool = False  # confirmed against positions/deals
    latency: LatencyWaterfall = Field(default_factory=LatencyWaterfall)
