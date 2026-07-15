"""Shared builders for execution-layer tests."""

from __future__ import annotations

from pathlib import Path

from aegis_velocity.core.config import DeskConfig, load_desk_config
from aegis_velocity.core.events import Side, Signal
from aegis_velocity.core.ledger import Ledger
from aegis_velocity.execution.engine import ExecutionEngine
from aegis_velocity.execution.idempotency import (
    IntentStore,
    comment_for,
    idem_key,
    magic_for,
)
from aegis_velocity.execution.intents import (
    ArmedIntent,
    build_market_request,
    build_pending_request,
)
from aegis_velocity.mt5.gateway import Mt5Gateway
from aegis_velocity.mt5.protocol import FillingMode, SymbolSpec
from aegis_velocity.mt5.sim import SimMt5Client, default_spec
from aegis_velocity.risk.guards import OrderStormFuse

REPO = Path(__file__).resolve().parents[1]
CFG: DeskConfig = load_desk_config(REPO, env={})


class ExecHarness:
    def __init__(self, tmp_path: Path, spec: SymbolSpec | None = None) -> None:
        self.spec = spec if spec is not None else default_spec("EURUSD")
        self.sim = SimMt5Client(specs={"EURUSD": self.spec}, balance=100_000.0)
        self.sim.initialize("", self.sim.login, "pw", self.sim.server)
        self.sim.symbol_select("EURUSD", True)
        self.sim.push_tick("EURUSD", 1.10000, 1.10010)
        self.gateway = Mt5Gateway(self.sim)
        self.gateway.start()
        self.ledger = Ledger(tmp_path / "ledger.db")
        self.intents = IntentStore(self.ledger.connection, self.ledger.db_lock)
        self.fuse = OrderStormFuse(CFG.risk)
        self.halts: list[str] = []
        self.engine = ExecutionEngine(
            gateway=self.gateway,
            ledger=self.ledger,
            intents=self.intents,
            storm_fuse=self.fuse,
            now_fn=lambda: self.sim.server_time,
            on_halt=self.halts.append,
        )

    def close(self) -> None:
        self.gateway.stop()
        self.ledger.close()

    def market_intent(
        self,
        trigger_id: str = "trig000000001",
        sl_points: int = 100,
        tp_points: int = 200,
        volume: float = 0.10,
        deviation: int = 20,
        side: Side = Side.BUY,
        filling: FillingMode = FillingMode.FOK,
    ) -> ArmedIntent:
        signal = Signal(
            strategy_id="F1", strategy_version=1, symbol="EURUSD", side=side,
            trigger="tick_armed", trigger_id=trigger_id, sl_points=sl_points,
            tp_points=tp_points, signal_time_utc=self.sim.server_time,
            tick_time_utc=self.sim.server_time,
        )
        key = idem_key(self.sim.login, "EURUSD", "F1", trigger_id, side.value)
        tick = self.sim.symbol_info_tick("EURUSD")
        assert tick is not None
        request = build_market_request(
            signal, self.spec, tick, volume, magic_for("F1", 1),
            comment_for(key), deviation, filling,
        )
        return ArmedIntent(
            key=key, signal=signal, request=request, spec=self.spec,
            armed_at=self.sim.server_time, risk_verdict_at=self.sim.server_time,
        )

    def pending_intent(
        self,
        trigger_id: str = "pend00000001",
        pending_type: str = "buy_stop",
        level: float = 1.10100,
        sl_points: int = 100,
        tp_points: int = 200,
        oco_group: str = "",
    ) -> ArmedIntent:
        side = Side.BUY if pending_type.startswith("buy") else Side.SELL
        signal = Signal(
            strategy_id="F3", strategy_version=1, symbol="EURUSD", side=side,
            trigger="pending", trigger_id=trigger_id, entry_price=level,
            pending_type=pending_type,  # type: ignore[arg-type]
            sl_points=sl_points, tp_points=tp_points, oco_group=oco_group,
            signal_time_utc=self.sim.server_time, tick_time_utc=self.sim.server_time,
        )
        key = idem_key(self.sim.login, "EURUSD", "F3", trigger_id, side.value)
        request = build_pending_request(
            signal, self.spec, 0.10, magic_for("F3", 1), comment_for(key),
            FillingMode.FOK, expiration=None,
        )
        return ArmedIntent(
            key=key, signal=signal, request=request, spec=self.spec,
            armed_at=self.sim.server_time, risk_verdict_at=self.sim.server_time,
        )
