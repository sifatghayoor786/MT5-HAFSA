"""Armed-intent pattern (§3.4): the full order request is pre-built when the
setup arms; on trigger only price refresh + final gates + send remain."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from aegis_velocity.core.events import Signal
from aegis_velocity.mt5.protocol import (
    FillingMode,
    OrderKind,
    OrderRequest,
    RequestAction,
    SymbolSpec,
    Tick,
)

ARMED_INTENT_MAX_AGE_S = 2.0
RISK_VERDICT_MAX_AGE_S = 2.0


@dataclass(frozen=True)
class ArmedIntent:
    key: str
    signal: Signal
    request: OrderRequest  # fully pre-built
    spec: SymbolSpec
    armed_at: datetime  # server time
    risk_verdict_at: datetime

    def stale(self, server_now: datetime) -> bool:
        age = (server_now - self.armed_at).total_seconds()
        limit = 30.0 if self.signal.trigger == "pending" else ARMED_INTENT_MAX_AGE_S
        return age > limit

    def risk_stale(self, server_now: datetime) -> bool:
        return (server_now - self.risk_verdict_at).total_seconds() > RISK_VERDICT_MAX_AGE_S

    def with_refreshed_price(self, tick: Tick) -> ArmedIntent:
        """Retry discipline: price refreshed on every retry; SL/TP distances kept."""
        if self.request.action is not RequestAction.DEAL:
            return self
        new_price = tick.ask if self.request.kind is OrderKind.BUY else tick.bid
        point = self.spec.point
        sl = (
            new_price - self.signal.sl_points * point
            if self.request.kind is OrderKind.BUY
            else new_price + self.signal.sl_points * point
        )
        tp = (
            new_price + self.signal.tp_points * point
            if self.request.kind is OrderKind.BUY
            else new_price - self.signal.tp_points * point
        )
        return replace(
            self,
            request=replace(
                self.request,
                price=self.spec.round_price(new_price),
                sl=self.spec.round_price(sl),
                tp=self.spec.round_price(tp),
            ),
        )


def build_market_request(
    signal: Signal,
    spec: SymbolSpec,
    tick: Tick,
    volume: float,
    magic: int,
    comment: str,
    deviation_points: int,
    filling: FillingMode,
) -> OrderRequest:
    kind = OrderKind.BUY if signal.side.value == "BUY" else OrderKind.SELL
    price = tick.ask if kind is OrderKind.BUY else tick.bid
    point = spec.point
    sl = price - signal.sl_points * point if kind is OrderKind.BUY else (
        price + signal.sl_points * point
    )
    tp = price + signal.tp_points * point if kind is OrderKind.BUY else (
        price - signal.tp_points * point
    )
    return OrderRequest(
        action=RequestAction.DEAL,
        symbol=signal.symbol,
        volume=volume,
        kind=kind,
        price=spec.round_price(price),
        sl=spec.round_price(sl),
        tp=spec.round_price(tp),
        deviation=deviation_points,
        magic=magic,
        comment=comment,
        type_filling=filling,
    )


def build_pending_request(
    signal: Signal,
    spec: SymbolSpec,
    volume: float,
    magic: int,
    comment: str,
    filling: FillingMode,
    expiration: datetime | None,
) -> OrderRequest:
    kind = OrderKind(signal.pending_type)
    point = spec.point
    level = spec.round_price(signal.entry_price)
    is_buy = kind.side.value == "BUY"
    sl = level - signal.sl_points * point if is_buy else level + signal.sl_points * point
    tp = level + signal.tp_points * point if is_buy else level - signal.tp_points * point
    return OrderRequest(
        action=RequestAction.PENDING,
        symbol=signal.symbol,
        volume=volume,
        kind=kind,
        price=level,
        sl=spec.round_price(sl),
        tp=spec.round_price(tp),
        magic=magic,
        comment=comment,
        type_filling=filling,
        type_time="SPECIFIED" if expiration is not None else "GTC",
        expiration=expiration,
    )


def next_filling(spec: SymbolSpec, current: FillingMode) -> FillingMode | None:
    """Filling ladder FOK -> IOC -> RETURN from the symbol's supported bitmask."""
    ladder = [
        m
        for m in (FillingMode.FOK, FillingMode.IOC, FillingMode.RETURN)
        if m in spec.filling_modes
    ]
    if current not in ladder:
        return ladder[0] if ladder else None
    idx = ladder.index(current)
    return ladder[idx + 1] if idx + 1 < len(ladder) else None
