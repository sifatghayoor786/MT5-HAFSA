"""Stage 3 gate: sim integration — connect → discover → pump → pending place →
trigger → partial → unknown-outcome → reconcile-by-comment."""

import time
from pathlib import Path

from aegis_velocity.mt5 import retcodes as rc
from aegis_velocity.mt5.gateway import P1_ORDER, P2_ACCOUNT, P3_DATA, Mt5Gateway
from aegis_velocity.mt5.protocol import OrderKind, OrderRequest, RequestAction, Tick
from aegis_velocity.mt5.recorder import TickRecorder, load_recorded_ticks
from aegis_velocity.mt5.sim import SimMt5Client, default_spec
from aegis_velocity.mt5.symbols import discover_symbols


def test_full_sim_integration(tmp_path: Path) -> None:
    sim = SimMt5Client(
        specs={"EURUSDm": default_spec("EURUSD"), "XAUUSD": default_spec("XAUUSD")}
    )
    # rename spec to the broker's suffixed name
    sim.specs["EURUSDm"] = type(sim.specs["EURUSDm"])(
        **{**sim.specs["EURUSDm"].__dict__, "name": "EURUSDm"}
    )

    # 1. connect
    assert sim.initialize("", sim.login, "pw", sim.server)

    # 2. discover
    gw = Mt5Gateway(sim)
    gw.start()
    try:
        names = gw.call(P3_DATA, "symbols_get_names")
        result = discover_symbols(names, ["EURUSD", "XAUUSD"])
        assert result.mapping == {"EURUSD": "EURUSDm", "XAUUSD": "XAUUSD"}
        broker_symbol = result.mapping["EURUSD"]
        assert gw.call(P3_DATA, "symbol_select", broker_symbol, True)

        # 3. pump + recorder
        recorder = TickRecorder(tmp_path / "ticks")
        gw.subscribe_ticks(recorder.on_tick)
        gw2 = gw  # pump started below with the same gateway
        gw2._pump_symbols = [broker_symbol]
        import threading

        pump = threading.Thread(target=gw2._run_pump, daemon=True)
        pump.start()
        sim.push_tick(broker_symbol, 1.10000, 1.10010)
        time.sleep(0.12)

        # 4. place pending buy stop above market
        place = gw.call(
            P1_ORDER,
            "order_send",
            OrderRequest(
                action=RequestAction.PENDING, symbol=broker_symbol, volume=0.10,
                kind=OrderKind.BUY_STOP, price=1.10100, sl=1.10000, tp=1.10300,
                magic=77_003_01, comment="AEG|itest0000001",
            ),
        )
        assert place is not None and place.retcode == rc.PLACED

        # 5. trigger at touch
        sim.push_tick(broker_symbol, 1.10090, 1.10100)
        time.sleep(0.12)  # let the pump observe the trigger tick
        positions = gw.call(P2_ACCOUNT, "positions_get", broker_symbol)
        assert len(positions) == 1 and positions[0].price_open == 1.10100

        # 6. partial fill on a second market order
        sim.partial_fill_fraction = 0.5
        partial = gw.call(
            P1_ORDER,
            "order_send",
            OrderRequest(
                action=RequestAction.DEAL, symbol=broker_symbol, volume=0.10,
                kind=OrderKind.SELL, magic=77_001_01, comment="AEG|itest0000002",
            ),
        )
        assert partial is not None and partial.retcode == rc.DONE_PARTIAL
        assert abs(partial.volume - 0.05) < 1e-9

        # 7. unknown outcome: send times out but broker executed
        sim.inject(None, execute_anyway=True)
        lost = gw.call(
            P1_ORDER,
            "order_send",
            OrderRequest(
                action=RequestAction.DEAL, symbol=broker_symbol, volume=0.10,
                kind=OrderKind.BUY, magic=77_001_01, comment="AEG|itest0000003",
            ),
        )
        assert lost is None

        # 8. reconcile: broker truth by idempotency comment + magic
        found = [
            p
            for p in gw.call(P2_ACCOUNT, "positions_get", broker_symbol)
            if p.comment == "AEG|itest0000003" and p.magic == 77_001_01
        ]
        assert len(found) == 1, "unknown-outcome order MUST be discoverable by comment key"

        # recorder captured the pumped ticks and round-trips
        recorder.close()
        recorded = load_recorded_ticks(tmp_path / "ticks", broker_symbol)
        assert len(recorded) >= 2
        assert all(isinstance(t, Tick) for t in recorded)
    finally:
        gw.stop()
