"""CLI human-only guard (release-blocking) + dashboard serves bus/ledger data."""

from typing import Any

from fastapi.testclient import TestClient

from aegis_velocity.cli import main
from aegis_velocity.dashboard.app import create_app


def test_human_only_commands_refused_non_interactively(capsys: Any) -> None:
    """Under pytest stdin/stdout are not TTYs: live commands MUST refuse."""
    for argv in (
        ["activate-live"],
        ["promote-live-full"],
        ["run", "--mode", "live"],
        ["run", "--mode", "live_canary"],
    ):
        code = main(argv)
        out = capsys.readouterr().out
        assert code == 3, f"{argv} must refuse non-interactively"
        assert "HUMAN-ONLY" in out


def test_doctor_and_validate_config_run(capsys: Any) -> None:
    assert main(["doctor"]) == 0
    assert "SIM-ONLY" in capsys.readouterr().out
    assert main(["validate-config"]) == 0
    assert "config OK" in capsys.readouterr().out


def test_dashboard_serves_status_and_ledger() -> None:
    status = {"mode": "SHADOW [SIM]", "equity": 10_000.0}
    rows = [{"kind": "decision", "payload": {"verdict": "REJECT"}}]
    app = create_app(lambda: status, lambda kind, limit: rows[:limit])
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200 and "AEGIS VELOCITY" in page.text

    api = client.get("/api/status")
    assert api.json()["mode"] == "SHADOW [SIM]"

    ledger = client.get("/api/ledger?kind=decision&limit=10")
    assert ledger.json()[0]["payload"]["verdict"] == "REJECT"
