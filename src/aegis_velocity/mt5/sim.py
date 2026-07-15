"""Deterministic MT5 simulator for CI (§3.6).

Capabilities: scripted ticks, retcode injection (optionally executing anyway, for
Unknown-Outcome tests), partial fills, latency injection, hedging/netting toggle,
pending-order trigger/expiry simulation, SL/TP execution, scripted context bars.
Never produces data it wasn't scripted with — nothing here fabricates market truth.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from aegis_velocity.core.events import Side
from aegis_velocity.mt5 import retcodes as rc
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

_T0 = datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)


def default_spec(name: str = "EURUSD") -> SymbolSpec:
    if name == "XAUUSD":
        return SymbolSpec(
            name=name, digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
            tick_value_loss=1.0, contract_size=100.0, volume_min=0.01, volume_max=100.0,
            volume_step=0.01, trade_stops_level=20, freeze_level=5,
            filling_modes=(FillingMode.FOK, FillingMode.IOC),
            currency_profit="USD", currency_margin="XAU",
        )
    if name.endswith("JPY"):
        return SymbolSpec(
            name=name, digits=3, point=0.001, tick_size=0.001, tick_value=0.68,
            tick_value_loss=0.68, contract_size=100_000.0, volume_min=0.01,
            volume_max=200.0, volume_step=0.01, trade_stops_level=10, freeze_level=3,
            filling_modes=(FillingMode.FOK, FillingMode.IOC, FillingMode.RETURN),
            currency_profit="JPY", currency_margin="USD",
        )
    return SymbolSpec(
        name=name, digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
        tick_value_loss=1.0, contract_size=100_000.0, volume_min=0.01, volume_max=200.0,
        volume_step=0.01, trade_stops_level=10, freeze_level=3,
        filling_modes=(FillingMode.FOK, FillingMode.IOC, FillingMode.RETURN),
        currency_profit="USD", currency_margin=name[:3] if len(name) >= 6 else "USD",
    )


@dataclass
class _Injection:
    retcode: int | None  # None => order_send returns no result object at all
    execute_anyway: bool = False  # the broker DID process it (unknown-outcome truth)


@dataclass
class SimMt5Client:
    """Deterministic simulator; satisfies the Mt5Client protocol."""

    specs: dict[str, SymbolSpec] = field(default_factory=dict)
    login: int = 55001122
    server: str = "SimBroker-Demo"
    currency: str = "USD"
    balance: float = 10_000.0
    leverage: int = 100
    margin_mode: str = "HEDGING"
    trade_mode: str = "DEMO"
    trade_allowed: bool = True
    is_investor: bool = False
    accepted_password: str | None = None  # None accepts anything
    commission_per_lot_per_side: float = 3.0
    market_slippage_points: int = 0  # applied against us on market fills
    partial_fill_fraction: float = 0.0  # >0 => next market order fills this fraction
    latency_ms: float = 0.0  # recorded, not slept

    def __post_init__(self) -> None:
        if not self.specs:
            self.specs = {"EURUSD": default_spec("EURUSD")}
        self._connected = False
        self._selected: set[str] = set()
        self._ticks: dict[str, Tick] = {}
        self._tick_history: dict[str, list[Tick]] = {s: [] for s in self.specs}
        self._bars: dict[tuple[str, int], list[Bar]] = {}
        self._positions: dict[int, Position] = {}
        self._orders: dict[int, PendingOrder] = {}
        self._deals: list[Deal] = []
        self._expired_tickets: list[int] = []
        self._injections: deque[_Injection] = deque()
        self._next_ticket = 1000
        self._server_time = _T0
        self.send_count = 0
        self.last_latency_ms = 0.0
        self._last_error: tuple[int, str] = (0, "ok")

    # ------------------------------------------------------------- sim controls

    def inject(self, retcode: int | None, execute_anyway: bool = False, times: int = 1) -> None:
        for _ in range(times):
            self._injections.append(_Injection(retcode, execute_anyway))

    def set_bars(self, symbol: str, timeframe_s: int, bars: list[Bar]) -> None:
        self._bars[(symbol, timeframe_s)] = bars

    def push_tick(
        self,
        symbol: str,
        bid: float,
        ask: float,
        advance_s: float = 1.0,
        time: datetime | None = None,
    ) -> Tick:
        if symbol not in self.specs:
            raise KeyError(f"symbol {symbol} not in sim specs")
        self._server_time = time if time is not None else (
            self._server_time + timedelta(seconds=advance_s)
        )
        tick = Tick(
            time=self._server_time, bid=bid, ask=ask, last=bid,
            time_msc=int(self._server_time.timestamp() * 1000),
        )
        self._ticks[symbol] = tick
        self._tick_history.setdefault(symbol, []).append(tick)
        self._process_pending_triggers(symbol, tick)
        self._process_sl_tp(symbol, tick)
        self._process_expirations()
        return tick

    @property
    def server_time(self) -> datetime:
        return self._server_time

    @property
    def expired_tickets(self) -> list[int]:
        return list(self._expired_tickets)

    def _ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    # ------------------------------------------------------- protocol: session

    def initialize(self, terminal_path: str, login: int, password: str, server: str) -> bool:
        if login != self.login or server != self.server:
            self._last_error = (-6, "authorization failed (login/server mismatch)")
            return False
        if self.accepted_password is not None and password != self.accepted_password:
            self._last_error = (-6, "authorization failed (bad password)")
            return False
        self._connected = True
        return True

    def shutdown(self) -> None:
        self._connected = False

    def account_info(self) -> AccountInfo | None:
        if not self._connected:
            return None
        floating = sum(self._position_profit(p) for p in self._positions.values())
        equity = self.balance + floating
        margin = sum(self._margin_of(p) for p in self._positions.values())
        margin_level = (equity / margin * 100.0) if margin > 0 else 0.0
        return AccountInfo(
            login=self.login, server=self.server, currency=self.currency,
            balance=self.balance, equity=equity, margin=margin,
            margin_free=equity - margin, margin_level=margin_level,
            leverage=self.leverage, margin_mode=self.margin_mode,
            trade_allowed=self.trade_allowed and not self.is_investor,
            trade_mode=self.trade_mode, is_investor=self.is_investor,
        )

    # ------------------------------------------------------- protocol: symbols

    def symbols_get_names(self) -> tuple[str, ...]:
        return tuple(self.specs)

    def symbol_info(self, name: str) -> SymbolSpec | None:
        return self.specs.get(name)

    def symbol_select(self, name: str, enable: bool) -> bool:
        if name not in self.specs:
            return False
        (self._selected.add if enable else self._selected.discard)(name)
        return True

    def symbol_info_tick(self, name: str) -> Tick | None:
        if name not in self._selected:
            return None  # mirrors MT5: symbol must be in Market Watch
        return self._ticks.get(name)

    def copy_ticks_from(self, name: str, from_time: datetime, count: int) -> list[Tick]:
        hist = self._tick_history.get(name, [])
        out = [t for t in hist if t.time >= from_time]
        return out[:count]

    def copy_rates(self, name: str, timeframe_s: int, count: int) -> list[Bar]:
        bars = self._bars.get((name, timeframe_s), [])
        return bars[-count:]

    # ----------------------------------------------------- protocol: calc/misc

    def order_calc_profit(
        self, side: Side, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None:
            return None
        raw_ticks = (price_close - price_open) / spec.tick_size * side.sign
        tick_value = spec.tick_value if raw_ticks >= 0 else spec.tick_value_loss
        return raw_ticks * tick_value * volume

    def order_calc_margin(
        self, side: Side, symbol: str, volume: float, price: float
    ) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None:
            return None
        notional_base = volume * spec.contract_size
        if spec.currency_margin == self.currency:
            # base currency IS the account currency (e.g. USDJPY on a USD account)
            return notional_base / self.leverage
        # convert base->account currency via price (XXXUSD pairs, metals)
        return notional_base * price / self.leverage

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    # ------------------------------------------------------ protocol: trading

    def _validate(self, request: OrderRequest) -> int:
        spec = self.specs.get(request.symbol)
        if spec is None:
            return rc.INVALID
        if spec.trade_mode == "DISABLED":
            return rc.TRADE_DISABLED
        if not self.trade_allowed or self.is_investor:
            return rc.CLIENT_DISABLES_AT
        if request.action in (RequestAction.DEAL, RequestAction.PENDING):
            vol = request.volume
            steps = round(vol / spec.volume_step)
            if (
                vol < spec.volume_min - 1e-12
                or vol > spec.volume_max + 1e-12
                or abs(steps * spec.volume_step - vol) > 1e-9
            ):
                return rc.INVALID_VOLUME
            if request.type_filling not in spec.filling_modes:
                return rc.INVALID_FILL
            tick = self._ticks.get(request.symbol)
            if tick is None:
                return rc.PRICE_OFF
            ref = tick.ask if request.kind.side is Side.BUY else tick.bid
            anchor = request.price if request.kind.is_pending else ref
            min_dist = spec.trade_stops_level * spec.point
            if request.sl and abs(anchor - request.sl) < min_dist - 1e-12:
                return rc.INVALID_STOPS
            if request.tp and abs(anchor - request.tp) < min_dist - 1e-12:
                return rc.INVALID_STOPS
            if request.kind.is_pending and abs(request.price - ref) < min_dist - 1e-12:
                return rc.INVALID_STOPS
        return 0

    def order_check(self, request: OrderRequest) -> OrderResultData | None:
        code = self._validate(request)
        if code == 0:
            margin = self.order_calc_margin(
                request.kind.side, request.symbol, request.volume, request.price or 1.0
            )
            acct = self.account_info()
            if margin is not None and acct is not None and margin > acct.margin_free:
                code = rc.NO_MONEY
        return OrderResultData(retcode=code, comment="check")

    def order_send(self, request: OrderRequest) -> OrderResultData | None:
        self.send_count += 1
        self.last_latency_ms = self.latency_ms
        if self._injections:
            inj = self._injections.popleft()
            if inj.execute_anyway:
                self._execute(request)  # broker processed it; caller never saw a result
            if inj.retcode is None:
                self._last_error = (-1, "no result (injected)")
                return None
            return OrderResultData(retcode=inj.retcode, comment="injected")
        code = self._validate(request)
        if code != 0:
            return OrderResultData(retcode=code, comment=rc.retcode_name(code))
        return self._execute(request)

    # ------------------------------------------------------------ execution

    def _execute(self, request: OrderRequest) -> OrderResultData:
        if request.action is RequestAction.DEAL:
            return self._exec_market(request)
        if request.action is RequestAction.PENDING:
            ticket = self._ticket()
            self._orders[ticket] = PendingOrder(
                ticket=ticket, symbol=request.symbol, kind=request.kind,
                volume=request.volume, price=request.price, sl=request.sl, tp=request.tp,
                magic=request.magic, comment=request.comment, time_setup=self._server_time,
                expiration=request.expiration if request.type_time == "SPECIFIED" else None,
            )
            return OrderResultData(retcode=rc.PLACED, order=ticket, price=request.price)
        if request.action is RequestAction.SLTP:
            pos = self._positions.get(request.position_ticket)
            if pos is None:
                return OrderResultData(retcode=rc.INVALID, comment="no such position")
            self._positions[pos.ticket] = replace(pos, sl=request.sl, tp=request.tp)
            return OrderResultData(retcode=rc.DONE)
        if request.action is RequestAction.MODIFY:
            order = self._orders.get(request.order_ticket)
            if order is None:
                return OrderResultData(retcode=rc.INVALID, comment="no such order")
            self._orders[order.ticket] = replace(
                order, price=request.price or order.price,
                sl=request.sl or order.sl, tp=request.tp or order.tp,
            )
            return OrderResultData(retcode=rc.DONE, order=order.ticket)
        if request.action is RequestAction.REMOVE:
            if request.order_ticket not in self._orders:
                return OrderResultData(retcode=rc.INVALID, comment="no such order")
            del self._orders[request.order_ticket]
            return OrderResultData(retcode=rc.DONE, order=request.order_ticket)
        return OrderResultData(retcode=rc.INVALID, comment="unsupported action")

    def _exec_market(self, request: OrderRequest) -> OrderResultData:
        spec = self.specs[request.symbol]
        tick = self._ticks[request.symbol]
        side = request.kind.side

        opposite = self._closing_position(request)
        if opposite is not None:
            return self._close_position(opposite, request.volume)

        slip = self.market_slippage_points * spec.point
        price = (tick.ask + slip) if side is Side.BUY else (tick.bid - slip)
        if request.deviation and request.price:
            if abs(price - request.price) > request.deviation * spec.point + 1e-12:
                return OrderResultData(retcode=rc.REQUOTE, comment="deviation exceeded")

        volume = request.volume
        retcode = rc.DONE
        if self.partial_fill_fraction > 0:
            steps = round(volume * self.partial_fill_fraction / spec.volume_step)
            volume = max(spec.volume_min, steps * spec.volume_step)
            if volume < request.volume - 1e-12:
                retcode = rc.DONE_PARTIAL
            self.partial_fill_fraction = 0.0

        ticket = self._ticket()
        deal_ticket = self._ticket()
        commission = self.commission_per_lot_per_side * volume
        self._positions[ticket] = Position(
            ticket=ticket, symbol=request.symbol, side=side, volume=volume,
            price_open=price, sl=request.sl, tp=request.tp, profit=0.0,
            magic=request.magic, comment=request.comment, time=self._server_time,
        )
        self._deals.append(
            Deal(
                ticket=deal_ticket, order=ticket, position_id=ticket, symbol=request.symbol,
                side=side, entry="IN", volume=volume, price=price, profit=0.0,
                commission=-commission, magic=request.magic, comment=request.comment,
                time=self._server_time,
            )
        )
        self.balance -= commission
        return OrderResultData(
            retcode=retcode, deal=deal_ticket, order=ticket, volume=volume, price=price
        )

    def _closing_position(self, request: OrderRequest) -> Position | None:
        if request.position_ticket:
            pos = self._positions.get(request.position_ticket)
            if pos is not None and pos.side is not request.kind.side:
                return pos
        return None

    def _close_position(self, pos: Position, volume: float) -> OrderResultData:
        tick = self._ticks[pos.symbol]
        price = tick.bid if pos.side is Side.BUY else tick.ask
        return self._settle_close(pos, min(volume or pos.volume, pos.volume), price)

    def _settle_close(self, pos: Position, volume: float, price: float) -> OrderResultData:
        profit = self.order_calc_profit(pos.side, pos.symbol, volume, pos.price_open, price)
        assert profit is not None
        commission = self.commission_per_lot_per_side * volume
        deal_ticket = self._ticket()
        self._deals.append(
            Deal(
                ticket=deal_ticket, order=pos.ticket, position_id=pos.ticket,
                symbol=pos.symbol, side=pos.side.opposite, entry="OUT", volume=volume,
                price=price, profit=profit, commission=-commission, magic=pos.magic,
                comment=pos.comment, time=self._server_time,
            )
        )
        self.balance += profit - commission
        if volume >= pos.volume - 1e-12:
            del self._positions[pos.ticket]
        else:
            self._positions[pos.ticket] = replace(pos, volume=pos.volume - volume)
        return OrderResultData(
            retcode=rc.DONE, deal=deal_ticket, order=pos.ticket, volume=volume, price=price
        )

    # --------------------------------------------------------- tick processing

    def _process_pending_triggers(self, symbol: str, tick: Tick) -> None:
        for order in [o for o in self._orders.values() if o.symbol == symbol]:
            k = order.kind
            hit = (
                (k is OrderKind.BUY_STOP and tick.ask >= order.price)
                or (k is OrderKind.SELL_STOP and tick.bid <= order.price)
                or (k is OrderKind.BUY_LIMIT and tick.ask <= order.price)
                or (k is OrderKind.SELL_LIMIT and tick.bid >= order.price)
            )
            if not hit:
                continue
            del self._orders[order.ticket]
            deal_ticket = self._ticket()
            commission = self.commission_per_lot_per_side * order.volume
            self._positions[order.ticket] = Position(
                ticket=order.ticket, symbol=symbol, side=k.side, volume=order.volume,
                price_open=order.price, sl=order.sl, tp=order.tp, profit=0.0,
                magic=order.magic, comment=order.comment, time=self._server_time,
            )
            self._deals.append(
                Deal(
                    ticket=deal_ticket, order=order.ticket, position_id=order.ticket,
                    symbol=symbol, side=k.side, entry="IN", volume=order.volume,
                    price=order.price, profit=0.0, commission=-commission,
                    magic=order.magic, comment=order.comment, time=self._server_time,
                )
            )
            self.balance -= commission

    def _process_sl_tp(self, symbol: str, tick: Tick) -> None:
        for pos in [p for p in self._positions.values() if p.symbol == symbol]:
            if pos.side is Side.BUY:
                if pos.sl and tick.bid <= pos.sl:
                    self._settle_close(pos, pos.volume, pos.sl)
                elif pos.tp and tick.bid >= pos.tp:
                    self._settle_close(pos, pos.volume, pos.tp)
            else:
                if pos.sl and tick.ask >= pos.sl:
                    self._settle_close(pos, pos.volume, pos.sl)
                elif pos.tp and tick.ask <= pos.tp:
                    self._settle_close(pos, pos.volume, pos.tp)

    def _process_expirations(self) -> None:
        for order in list(self._orders.values()):
            if order.expiration is not None and self._server_time >= order.expiration:
                del self._orders[order.ticket]
                self._expired_tickets.append(order.ticket)

    # ------------------------------------------------------------- accounting

    def _position_profit(self, pos: Position) -> float:
        tick = self._ticks.get(pos.symbol)
        if tick is None:
            return 0.0
        price = tick.bid if pos.side is Side.BUY else tick.ask
        profit = self.order_calc_profit(pos.side, pos.symbol, pos.volume, pos.price_open, price)
        return profit if profit is not None else 0.0

    def _margin_of(self, pos: Position) -> float:
        margin = self.order_calc_margin(pos.side, pos.symbol, pos.volume, pos.price_open)
        return margin if margin is not None else 0.0

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        out = [
            replace(p, profit=self._position_profit(p))
            for p in self._positions.values()
            if symbol is None or p.symbol == symbol
        ]
        return sorted(out, key=lambda p: p.ticket)

    def orders_get(self, symbol: str | None = None) -> list[PendingOrder]:
        out = [o for o in self._orders.values() if symbol is None or o.symbol == symbol]
        return sorted(out, key=lambda o: o.ticket)

    def history_deals_get(self, from_time: datetime, to_time: datetime) -> list[Deal]:
        return [d for d in self._deals if from_time <= d.time <= to_time]
