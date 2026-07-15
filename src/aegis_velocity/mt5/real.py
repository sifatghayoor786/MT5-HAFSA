"""RealMt5Client: adapter over the official MetaTrader5 package.

Import-guarded — this module loads on any platform, but constructing the client
without the package (Windows-only) raises. Nothing here is reachable in CI.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from typing import Any

from aegis_velocity.core.events import Side
from aegis_velocity.mt5.protocol import (
    AccountInfo,
    Bar,
    Deal,
    FillingMode,
    OrderKind,
    OrderRequest,
    OrderResultData,
    PendingOrder,
    Position,
    RequestAction,
    SymbolSpec,
    Tick,
)


def real_client_available() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, f"platform is {sys.platform}; the MetaTrader5 package is Windows-only"
    try:
        importlib.import_module("MetaTrader5")
    except ImportError:
        return False, "MetaTrader5 package not installed (pip install MetaTrader5)"
    return True, "ok"


_ORDER_TYPE_TO_KIND = {
    0: OrderKind.BUY,
    1: OrderKind.SELL,
    2: OrderKind.BUY_LIMIT,
    3: OrderKind.SELL_LIMIT,
    4: OrderKind.BUY_STOP,
    5: OrderKind.SELL_STOP,
}
_KIND_TO_ORDER_TYPE = {v: k for k, v in _ORDER_TYPE_TO_KIND.items()}
_FILLING_TO_MT5 = {FillingMode.FOK: 0, FillingMode.IOC: 1, FillingMode.RETURN: 2}


class RealMt5Client:
    """Thin, faithful mapping onto the MetaTrader5 module. One instance per process."""

    def __init__(self) -> None:
        available, detail = real_client_available()
        if not available:
            raise RuntimeError(f"RealMt5Client unavailable: {detail}")
        self._mt5: Any = importlib.import_module("MetaTrader5")

    # NOTE: every method below runs on the gateway thread only.

    def initialize(self, terminal_path: str, login: int, password: str, server: str) -> bool:
        ok: bool = self._mt5.initialize(
            path=terminal_path or None, login=login, password=password, server=server
        )
        return ok

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def account_info(self) -> AccountInfo | None:
        info = self._mt5.account_info()
        if info is None:
            return None
        margin_modes = {0: "NETTING", 1: "EXCHANGE", 2: "HEDGING"}
        trade_modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
        return AccountInfo(
            login=info.login,
            server=info.server,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            margin_level=info.margin_level,
            leverage=info.leverage,
            margin_mode=margin_modes.get(info.margin_mode, "NETTING"),
            trade_allowed=bool(info.trade_allowed),
            trade_mode=trade_modes.get(info.trade_mode, "DEMO"),
            is_investor=not bool(info.trade_allowed),
        )

    def symbols_get_names(self) -> tuple[str, ...]:
        symbols = self._mt5.symbols_get()
        return tuple(s.name for s in symbols) if symbols else ()

    def symbol_info(self, name: str) -> SymbolSpec | None:
        s = self._mt5.symbol_info(name)
        if s is None:
            return None
        fillings: list[FillingMode] = []
        if s.filling_mode & 1:
            fillings.append(FillingMode.FOK)
        if s.filling_mode & 2:
            fillings.append(FillingMode.IOC)
        fillings.append(FillingMode.RETURN)
        trade_modes = {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}
        return SymbolSpec(
            name=s.name,
            digits=s.digits,
            point=s.point,
            tick_size=s.trade_tick_size or s.point,
            tick_value=s.trade_tick_value_profit or s.trade_tick_value,
            tick_value_loss=s.trade_tick_value_loss or s.trade_tick_value,
            contract_size=s.trade_contract_size,
            volume_min=s.volume_min,
            volume_max=s.volume_max,
            volume_step=s.volume_step,
            trade_stops_level=s.trade_stops_level,
            freeze_level=s.trade_freeze_level,
            filling_modes=tuple(fillings),
            currency_profit=s.currency_profit,
            currency_margin=s.currency_margin,
            trade_mode=trade_modes.get(s.trade_mode, "FULL"),
        )

    def symbol_select(self, name: str, enable: bool) -> bool:
        return bool(self._mt5.symbol_select(name, enable))

    def symbol_info_tick(self, name: str) -> Tick | None:
        t = self._mt5.symbol_info_tick(name)
        if t is None:
            return None
        return Tick(
            time=datetime.fromtimestamp(t.time_msc / 1000.0, tz=UTC),
            bid=t.bid,
            ask=t.ask,
            last=t.last,
            volume=t.volume,
            time_msc=t.time_msc,
        )

    def copy_ticks_from(self, name: str, from_time: datetime, count: int) -> list[Tick]:
        ticks = self._mt5.copy_ticks_from(name, from_time, count, self._mt5.COPY_TICKS_ALL)
        if ticks is None:
            return []
        return [
            Tick(
                time=datetime.fromtimestamp(int(t["time_msc"]) / 1000.0, tz=UTC),
                bid=float(t["bid"]),
                ask=float(t["ask"]),
                last=float(t["last"]),
                volume=float(t["volume"]),
                time_msc=int(t["time_msc"]),
            )
            for t in ticks
        ]

    def copy_rates(self, name: str, timeframe_s: int, count: int) -> list[Bar]:
        tf_map = {60: self._mt5.TIMEFRAME_M1, 300: self._mt5.TIMEFRAME_M5,
                  900: self._mt5.TIMEFRAME_M15}
        timeframe = tf_map.get(timeframe_s)
        if timeframe is None:
            return []
        rates = self._mt5.copy_rates_from_pos(name, timeframe, 0, count)
        if rates is None:
            return []
        return [
            Bar(
                time=datetime.fromtimestamp(int(r["time"]), tz=UTC),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=float(r["tick_volume"]),
                spread=int(r["spread"]),
            )
            for r in rates
        ]

    def _to_mt5_request(self, request: OrderRequest) -> dict[str, Any]:
        actions = {
            RequestAction.DEAL: self._mt5.TRADE_ACTION_DEAL,
            RequestAction.PENDING: self._mt5.TRADE_ACTION_PENDING,
            RequestAction.SLTP: self._mt5.TRADE_ACTION_SLTP,
            RequestAction.MODIFY: self._mt5.TRADE_ACTION_MODIFY,
            RequestAction.REMOVE: self._mt5.TRADE_ACTION_REMOVE,
        }
        req: dict[str, Any] = {
            "action": actions[request.action],
            "symbol": request.symbol,
            "volume": request.volume,
            "type": _KIND_TO_ORDER_TYPE[request.kind],
            "price": request.price,
            "sl": request.sl,
            "tp": request.tp,
            "deviation": request.deviation,
            "magic": request.magic,
            "comment": request.comment,
            "type_filling": _FILLING_TO_MT5[request.type_filling],
            "type_time": (
                self._mt5.ORDER_TIME_SPECIFIED
                if request.type_time == "SPECIFIED"
                else self._mt5.ORDER_TIME_GTC
            ),
        }
        if request.expiration is not None:
            req["expiration"] = int(request.expiration.timestamp())
        if request.position_ticket:
            req["position"] = request.position_ticket
        if request.order_ticket:
            req["order"] = request.order_ticket
        return req

    def order_check(self, request: OrderRequest) -> OrderResultData | None:
        result = self._mt5.order_check(self._to_mt5_request(request))
        if result is None:
            return None
        return OrderResultData(retcode=result.retcode, comment=result.comment)

    def order_send(self, request: OrderRequest) -> OrderResultData | None:
        result = self._mt5.order_send(self._to_mt5_request(request))
        if result is None:
            return None
        return OrderResultData(
            retcode=result.retcode,
            deal=result.deal,
            order=result.order,
            volume=result.volume,
            price=result.price,
            comment=result.comment,
            request_id=result.request_id,
        )

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        positions = (
            self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
        )
        if positions is None:
            return []
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side=Side.BUY if p.type == 0 else Side.SELL,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                magic=p.magic,
                comment=p.comment,
                time=datetime.fromtimestamp(p.time, tz=UTC),
            )
            for p in positions
        ]

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]:
        orders = self._mt5.orders_get(symbol=symbol) if symbol else self._mt5.orders_get()
        if orders is None:
            return []
        return [
            PendingOrder(
                ticket=o.ticket,
                symbol=o.symbol,
                kind=_ORDER_TYPE_TO_KIND.get(o.type, OrderKind.BUY_STOP),
                volume=o.volume_current,
                price=o.price_open,
                sl=o.sl,
                tp=o.tp,
                magic=o.magic,
                comment=o.comment,
                time_setup=datetime.fromtimestamp(o.time_setup, tz=UTC),
                expiration=(
                    datetime.fromtimestamp(o.time_expiration, tz=UTC)
                    if o.time_expiration
                    else None
                ),
            )
            for o in orders
        ]

    def history_deals_get(self, from_time: datetime, to_time: datetime) -> list[Deal]:
        deals = self._mt5.history_deals_get(from_time, to_time)
        if deals is None:
            return []
        entries = {0: "IN", 1: "OUT", 2: "INOUT"}
        return [
            Deal(
                ticket=d.ticket,
                order=d.order,
                position_id=d.position_id,
                symbol=d.symbol,
                side=Side.BUY if d.type == 0 else Side.SELL,
                entry=entries.get(d.entry, "IN"),
                volume=d.volume,
                price=d.price,
                profit=d.profit,
                commission=d.commission,
                magic=d.magic,
                comment=d.comment,
                time=datetime.fromtimestamp(d.time, tz=UTC),
            )
            for d in deals
            if d.type in (0, 1)
        ]

    def order_calc_profit(
        self, side: Side, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float | None:
        order_type = 0 if side is Side.BUY else 1
        result = self._mt5.order_calc_profit(order_type, symbol, volume, price_open, price_close)
        return float(result) if result is not None else None

    def order_calc_margin(
        self, side: Side, symbol: str, volume: float, price: float
    ) -> float | None:
        order_type = 0 if side is Side.BUY else 1
        result = self._mt5.order_calc_margin(order_type, symbol, volume, price)
        return float(result) if result is not None else None

    def last_error(self) -> tuple[int, str]:
        code, message = self._mt5.last_error()
        return int(code), str(message)
