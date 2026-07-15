"""Hash-chained ledger: durability, tamper detection, chain continuity."""

import json
import sqlite3
from pathlib import Path

from aegis_velocity.core.ledger import Ledger


def _make(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", journal_path=tmp_path / "journal.jsonl")


def test_append_and_verify(tmp_path: Path) -> None:
    led = _make(tmp_path)
    led.append("signal", {"symbol": "EURUSD", "side": "BUY"}, correlation_id="c1")
    led.append("decision", {"verdict": "REJECT", "reasons": ["COST_GATE_FAIL"]}, "c1")
    result = led.verify()
    assert result.ok and result.rows == 2
    rows = list(led.rows(kind="decision"))
    assert rows[0].payload["verdict"] == "REJECT"
    led.close()


def test_payload_tamper_detected(tmp_path: Path) -> None:
    led = _make(tmp_path)
    led.append("order", {"volume": 0.01}, "c1")
    led.append("deal", {"price": 1.1}, "c1")
    led.close()

    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.execute("UPDATE ledger SET payload = ? WHERE kind = 'order'", ('{"volume":9.99}',))
    conn.commit()
    conn.close()

    led2 = Ledger(tmp_path / "ledger.db")
    result = led2.verify()
    assert not result.ok
    assert "mismatch" in result.detail
    led2.close()


def test_row_deletion_detected(tmp_path: Path) -> None:
    led = _make(tmp_path)
    for i in range(3):
        led.append("halt", {"n": i}, "c")
    led.close()
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.execute("DELETE FROM ledger WHERE seq = 2")
    conn.commit()
    conn.close()
    led2 = Ledger(tmp_path / "ledger.db")
    assert not led2.verify().ok
    led2.close()


def test_chain_continues_across_reopen(tmp_path: Path) -> None:
    led = _make(tmp_path)
    led.append("a", {"x": 1})
    led.close()
    led2 = _make(tmp_path)
    led2.append("b", {"x": 2})
    result = led2.verify()
    assert result.ok and result.rows == 2
    led2.close()


def test_journal_mirrors_every_fact_durably(tmp_path: Path) -> None:
    led = _make(tmp_path)
    led.append("execution", {"retcode": 10009}, "corr9")
    led.close()
    lines = (tmp_path / "journal.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "execution"
    assert entry["correlation_id"] == "corr9"
    assert json.loads(entry["payload"])["retcode"] == 10009
