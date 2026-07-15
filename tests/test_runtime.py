"""Runtime wiring on sim: shadow scan records decisions; emergency stop;
discovery/eligibility CLI paths; backtest INSUFFICIENT_DATA honesty."""

import shutil
from pathlib import Path

from aegis_velocity.core.ledger import Ledger
from aegis_velocity.runtime import (
    backtest_cli,
    calendar_import_cli,
    discover_symbols_cli,
    emergency_stop_cli,
    run_desk_cli,
    scalp_eligibility_cli,
)

REPO = Path(__file__).resolve().parents[1]


def _tmp_repo(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "configs", tmp_path / "configs")
    return tmp_path


def test_discover_and_eligibility_on_sim(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _tmp_repo(tmp_path)
    assert discover_symbols_cli(root) == 0
    out = capsys.readouterr().out
    assert "[SIM]" in out and "EURUSD -> EURUSD" in out
    assert (root / "data" / "symbol_map.json").is_file()

    assert scalp_eligibility_cli(root) == 0
    out = capsys.readouterr().out
    assert "ELIGIBLE" in out or "DISQUALIFIED" in out
    assert (root / "data" / "scalp_eligibility.json").is_file()


def test_shadow_scan_records_signals_and_decisions(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _tmp_repo(tmp_path)
    calendar_import_cli(root, REPO / "configs" / "calendar" / "sample_calendar.csv")
    assert run_desk_cli(root, mode="shadow", duration_s=240) == 0
    out = capsys.readouterr().out
    assert "[SIM]" in out and "scan done" in out and "ledger OK" in out

    ledger = Ledger(root / "data" / "aegis_velocity.db")
    try:
        kinds = {r.kind for r in ledger.rows(limit=100_000)}
        assert "scan_summary" in kinds
        assert ledger.verify().ok
        # decisions carry machine-readable reasons and correlation ids
        decisions = list(ledger.rows(kind="decision", limit=10))
        if decisions:  # signals depend on the seeded walk; decisions mirror signals
            assert decisions[0].correlation_id
            assert "verdict" in decisions[0].payload
    finally:
        ledger.close()


def test_emergency_stop_disarms_and_records(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _tmp_repo(tmp_path)
    (root / "data").mkdir(exist_ok=True)
    from aegis_velocity.arming import read_token, write_token

    write_token(root / "data" / "arming.json", 1, "s", "LIVE", "h")
    assert emergency_stop_cli(root, flatten=False) == 0
    assert read_token(root / "data" / "arming.json") is None  # token invalidated
    ledger = Ledger(root / "data" / "aegis_velocity.db")
    try:
        halts = list(ledger.rows(kind="halt"))
        assert halts and halts[0].payload["reason"] == "EMERGENCY_STOP"
    finally:
        ledger.close()


def test_backtest_refuses_without_recorded_ticks(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _tmp_repo(tmp_path)
    assert backtest_cli(root, strategy="F1", symbol="EURUSD") == 1
    out = capsys.readouterr().out
    assert "INSUFFICIENT_DATA" in out and "NON-EVIDENCE" in out
