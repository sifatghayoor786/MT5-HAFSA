"""Auditor environment separation + write-behind; arming token lifecycle."""

from pathlib import Path

from aegis_velocity.arming import (
    disarm,
    read_token,
    validate_token,
    write_token,
)
from aegis_velocity.audit.auditor import Auditor, TradeFact
from aegis_velocity.core.ledger import Ledger


def _fact(env: str, r: float) -> TradeFact:
    return TradeFact(
        environment=env, strategy_id="F1", symbol="EURUSD", return_r=r,
        slippage_points=2.0, latency_ms=150.0, cost_ccy=1.7, gross_ccy=r * 20,
    )


def test_auditor_separates_environments(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    auditor = Auditor(ledger)
    auditor.start()
    try:
        for _ in range(5):
            auditor.submit(_fact("SHADOW", 1.0))
        for _ in range(3):
            auditor.submit(_fact("DEMO", -1.0))
        auditor.drain()
        shadow = auditor.report("SHADOW")
        demo = auditor.report("DEMO")
        assert len(shadow) == 1 and shadow[0]["n"] == 5
        assert len(demo) == 1 and demo[0]["n"] == 3
        assert "insufficient sample" in str(shadow[0]["win_rate"])  # n=5 < 30
        # write-behind enrichment landed in the ledger
        rows = list(ledger.rows(kind="trade_analytics"))
        assert len(rows) == 8
    finally:
        auditor.stop()
        ledger.close()


def test_arming_token_roundtrip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "arming.json"
    write_token(path, account=123456, server="Broker-Live", mode="LIVE_CANARY",
                config_hash="abc123")
    status = validate_token(path, 123456, "Broker-Live", "abc123")
    assert status.armed and status.token is not None
    assert status.token.mode == "LIVE_CANARY"


def test_arming_invalidates_on_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "arming.json"
    for kwargs, reason_part in (
        ({"account": 999}, "account"),
        ({"server": "Other-Server"}, "server"),
        ({"config_hash": "zzz"}, "config"),
        ({"emergency_stopped": True}, "emergency"),
        ({"hard_drawdown_halted": True}, "drawdown"),
        ({"bridge_lost_beyond_grace": True}, "bridge"),
    ):
        write_token(path, 123456, "Broker-Live", "LIVE", "abc123")
        args = {"account": 123456, "server": "Broker-Live", "config_hash": "abc123"}
        args.update(kwargs)  # type: ignore[arg-type]
        status = validate_token(path, **args)  # type: ignore[arg-type]
        assert not status.armed
        assert reason_part in status.reason
        # token physically invalidated on disk: a second read must fail too
        assert read_token(path) is None


def test_disarm_writes_audit_stub(tmp_path: Path) -> None:
    path = tmp_path / "arming.json"
    write_token(path, 1, "s", "LIVE", "h")
    disarm(path, reason="test")
    assert read_token(path) is None
    assert "disarmed" in path.read_text()
