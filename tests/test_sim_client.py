"""SimMt5Client: deterministic broker behaviour needed by every safety test."""

from datetime import timedelta

from aegis_velocity.mt5 import retcodes as rc
from aegis_velocity.mt5.protocol import (
    FillingMode,
    OrderKind,
    OrderRequest,
    RequestAction,
)
from aegis_velocity.mt5.sim import SimMt5Client, default_spec


def _client() -> SimMt5Client:
    c = SimMt5Client(specs={"EURUSD": default_spec("EURUSD")})
    assert c.initialize("", c.login, "pw", c.server)
    c.symbol_select("EURUSD", True)
    c.push_tick("EURUSD", 1.10000, 1.10010)
    return c


def _buy(volume: float = 0.10, sl: float = 0.0, tp: float = 0.0) -> OrderRequest:
    return OrderRequest(
        action=RequestAction.DEAL, symbol="EURUSD", volume=volume, kind=OrderKind.BUY,
        sl=sl, tp=tp, magic=77_001_01, comment="AEG|abc123def456", type_filling=FillingMode.FOK,
    )


def test_wrong_login_or_server_fails_closed() -> None:
    c = SimMt5Client()
    assert not c.initialize("", 999, "pw", c.server)
    assert not c.initialize("", c.login, "pw", "WrongServer")
    assert c.account_info() is None  # not connected -> no account data


def test_market_buy_fills_at_ask_with_commission() -> None:
    c = _client()
    result = c.order_send(_buy())
    assert result is not None and result.retcode == rc.DONE
    assert result.price == 1.10010  # ask
    positions = c.positions_get("EURUSD")
    assert len(positions) == 1 and positions[0].comment == "AEG|abc123def456"
    deals = c.history_deals_get(c.server_time - timedelta(days=1), c.server_time)
    assert len(deals) == 1 and deals[0].entry == "IN"
    assert deals[0].commission == -3.0 * 0.10


def test_sl_hit_closes_position_with_correct_pnl() -> None:
    c = _client()
    c.order_send(_buy(volume=0.10, sl=1.09910, tp=1.10210))  # SL 10 points below bid
    c.push_tick("EURUSD", 1.09905, 1.09915)  # bid through SL
    assert c.positions_get("EURUSD") == []
    out = [d for d in c._deals if d.entry == "OUT"]
    assert len(out) == 1
    # loss = (1.09910-1.10010)/0.00001 ticks * $1/tick * 0.10 lots = -$10
    assert abs(out[0].profit - (-10.0)) < 1e-6


def test_pending_buy_stop_triggers_at_touch() -> None:
    c = _client()
    result = c.order_send(
        OrderRequest(
            action=RequestAction.PENDING, symbol="EURUSD", volume=0.05,
            kind=OrderKind.BUY_STOP, price=1.10100, sl=1.10000, tp=1.10300,
            magic=77_003_01, comment="AEG|pend1",
        )
    )
    assert result is not None and result.retcode == rc.PLACED
    c.push_tick("EURUSD", 1.10050, 1.10060)  # below trigger
    assert len(c.orders_get()) == 1 and c.positions_get() == []
    c.push_tick("EURUSD", 1.10095, 1.10105)  # ask touches 1.10100? 1.10105 >= 1.10100
    assert c.orders_get() == []
    positions = c.positions_get()
    assert len(positions) == 1 and positions[0].price_open == 1.10100


def test_pending_expires_at_broker() -> None:
    c = _client()
    exp = c.server_time + timedelta(seconds=60)
    result = c.order_send(
        OrderRequest(
            action=RequestAction.PENDING, symbol="EURUSD", volume=0.05,
            kind=OrderKind.SELL_STOP, price=1.09900, magic=77_003_01, comment="AEG|pend2",
            type_time="SPECIFIED", expiration=exp,
        )
    )
    assert result is not None and result.retcode == rc.PLACED
    c.push_tick("EURUSD", 1.10000, 1.10010, advance_s=61)
    assert c.orders_get() == []
    assert result.order in c.expired_tickets


def test_partial_fill_reports_done_partial() -> None:
    c = _client()
    c.partial_fill_fraction = 0.4
    result = c.order_send(_buy(volume=0.10))
    assert result is not None and result.retcode == rc.DONE_PARTIAL
    assert abs(result.volume - 0.04) < 1e-9
    assert abs(c.positions_get()[0].volume - 0.04) < 1e-9


def test_retcode_injection_and_unknown_outcome_truth() -> None:
    c = _client()
    c.inject(rc.REQUOTE)
    result = c.order_send(_buy())
    assert result is not None and result.retcode == rc.REQUOTE
    assert c.positions_get() == []  # requote did not execute

    # timeout where the broker DID execute: caller sees nothing, truth exists
    c.inject(None, execute_anyway=True)
    result2 = c.order_send(_buy())
    assert result2 is None
    found = [p for p in c.positions_get() if p.comment == "AEG|abc123def456"]
    assert len(found) == 1  # reconciliation by comment key must find it


def test_netting_toggle_and_investor_block() -> None:
    c = SimMt5Client(margin_mode="NETTING")
    c.initialize("", c.login, "pw", c.server)
    info = c.account_info()
    assert info is not None and info.margin_mode == "NETTING"

    inv = SimMt5Client(is_investor=True)
    inv.initialize("", inv.login, "pw", inv.server)
    inv.symbol_select("EURUSD", True)
    inv.push_tick("EURUSD", 1.1, 1.1001)
    info2 = inv.account_info()
    assert info2 is not None and not info2.trade_allowed
    result = inv.order_send(_buy())
    assert result is not None and result.retcode == rc.CLIENT_DISABLES_AT


def test_validation_retcodes() -> None:
    c = _client()
    bad_step = c.order_send(_buy(volume=0.017))
    assert bad_step is not None and bad_step.retcode == rc.INVALID_VOLUME

    tight_sl = c.order_send(_buy(sl=1.10008))  # 0.2 points from ask < stops_level 10
    assert tight_sl is not None and tight_sl.retcode == rc.INVALID_STOPS

    spec = default_spec("EURUSD")
    limited = SimMt5Client(
        specs={
            "EURUSD": type(spec)(
                **{**spec.__dict__, "filling_modes": (FillingMode.IOC,)}
            )
        }
    )
    limited.initialize("", limited.login, "pw", limited.server)
    limited.symbol_select("EURUSD", True)
    limited.push_tick("EURUSD", 1.1, 1.1001)
    fok = limited.order_send(_buy())
    assert fok is not None and fok.retcode == rc.INVALID_FILL


def test_deviation_exceeded_requotes() -> None:
    c = _client()
    c.market_slippage_points = 5
    req = OrderRequest(
        action=RequestAction.DEAL, symbol="EURUSD", volume=0.10, kind=OrderKind.BUY,
        price=1.10010, deviation=3, magic=1, comment="AEG|dev",
    )
    result = c.order_send(req)
    assert result is not None and result.retcode == rc.REQUOTE


def test_equity_reflects_floating_pnl() -> None:
    c = _client()
    c.order_send(_buy(volume=0.10))
    c.push_tick("EURUSD", 1.10100, 1.10110)  # +90 points on bid vs open 1.10010
    info = c.account_info()
    assert info is not None
    floating = (1.10100 - 1.10010) / 0.00001 * 1.0 * 0.10
    assert abs(info.equity - (c.balance + floating)) < 1e-6
