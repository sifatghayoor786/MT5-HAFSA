"""`Mt5Client` protocol and broker data types.

Every field mirrors the official MetaTrader5 package semantics; `SimMt5Client`
and `RealMt5Client` both satisfy this protocol, so all safety logic is testable
terminal-free (§3.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from aegis_velocity.core.events import Side


class OrderKind(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"

    @property
    def is_pending(self) -> bool:
        return self not in (OrderKind.BUY, OrderKind.SELL)

    @property
    def side(self) -> Side:
        return Side.BUY if self.value.startswith("buy") else Side.SELL


class FillingMode(StrEnum):
    FOK = "FOK"
    IOC = "IOC"
    RETURN = "RETURN"


class RequestAction(StrEnum):
    DEAL = "DEAL"  # market execution
    PENDING = "PENDING"  # place pending order
    SLTP = "SLTP"  # modify position SL/TP
    MODIFY = "MODIFY"  # modify pending order
    REMOVE = "REMOVE"  # cancel pending order


@dataclass(frozen=True)
class Tick:
    time: datetime  # broker server time, tz-aware UTC
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0
    time_msc: int = 0

    def spread_points(self, point: float) -> int:
        return round((self.ask - self.bid) / point)


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # account currency per tick per 1.0 lot (profit direction)
    tick_value_loss: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int  # min distance for stops, in points
    freeze_level: int
    filling_modes: tuple[FillingMode, ...]
    currency_profit: str
    currency_margin: str
    trade_mode: str = "FULL"  # FULL | DISABLED | LONGONLY | SHORTONLY | CLOSEONLY

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.digits)


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float  # percent; 0 when no positions
    leverage: int
    margin_mode: str  # HEDGING | NETTING | EXCHANGE
    trade_allowed: bool  # false also when logged in with investor password
    trade_mode: str  # DEMO | REAL | CONTEST
    is_investor: bool


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    magic: int
    comment: str
    time: datetime


@dataclass(frozen=True)
class PendingOrder:
    ticket: int
    symbol: str
    kind: OrderKind
    volume: float
    price: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: datetime
    expiration: datetime | None


@dataclass(frozen=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: Side
    entry: str  # IN | OUT | INOUT
    volume: float
    price: float
    profit: float
    commission: float
    magic: int
    comment: str
    time: datetime


@dataclass(frozen=True)
class OrderRequest:
    action: RequestAction
    symbol: str
    volume: float = 0.0
    kind: OrderKind = OrderKind.BUY
    price: float = 0.0  # market: expected price; pending: trigger level
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 0  # max slippage, points
    magic: int = 0
    comment: str = ""
    type_filling: FillingMode = FillingMode.FOK
    type_time: str = "GTC"  # GTC | SPECIFIED
    expiration: datetime | None = None
    position_ticket: int = 0  # for SLTP/close-by-ticket
    order_ticket: int = 0  # for MODIFY/REMOVE


@dataclass(frozen=True)
class OrderResultData:
    retcode: int
    deal: int = 0
    order: int = 0
    volume: float = 0.0  # filled volume
    price: float = 0.0  # fill price
    comment: str = ""
    request_id: int = 0


@dataclass
class Bar:
    time: datetime  # bar open, server time
    open: float
    high: float
    low: float
    close: float
    tick_volume: float = 0.0
    spread: int = 0
    is_closed: bool = field(default=True)


class Mt5Client(Protocol):
    """The one interface through which ALL broker interaction flows."""

    def initialize(self, terminal_path: str, login: int, password: str, server: str) -> bool: ...

    def shutdown(self) -> None: ...

    def account_info(self) -> AccountInfo | None: ...

    def symbols_get_names(self) -> tuple[str, ...]: ...

    def symbol_info(self, name: str) -> SymbolSpec | None: ...

    def symbol_select(self, name: str, enable: bool) -> bool: ...

    def symbol_info_tick(self, name: str) -> Tick | None: ...

    def copy_ticks_from(self, name: str, from_time: datetime, count: int) -> list[Tick]: ...

    def copy_rates(self, name: str, timeframe_s: int, count: int) -> list[Bar]: ...

    def order_check(self, request: OrderRequest) -> OrderResultData | None: ...

    def order_send(self, request: OrderRequest) -> OrderResultData | None: ...

    def positions_get(self, symbol: str | None = None) -> list[Position]: ...

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]: ...

    def history_deals_get(self, from_time: datetime, to_time: datetime) -> list[Deal]: ...

    def order_calc_profit(
        self, side: Side, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float | None: ...

    def order_calc_margin(
        self, side: Side, symbol: str, volume: float, price: float
    ) -> float | None: ...

    def last_error(self) -> tuple[int, str]: ...
