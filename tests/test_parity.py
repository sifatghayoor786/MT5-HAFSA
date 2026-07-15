"""Golden-file signal parity (§7/§11 harness).

Recorded scenario windows replayed through the strategies must reproduce the
committed golden signals bit-for-bit (volatile identity fields excluded).
Regenerate ONLY on an intentional strategy version bump:
    python -m tests.test_parity  (writes tests/golden/*.json)
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.strategy_scenarios import scenarios

from aegis_velocity.core.ledger import canonical_json
from aegis_velocity.strategies import STRATEGY_REGISTRY

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
EXCLUDE = {"event_id", "ts_utc"}


def _canonical(strategy_id: str) -> str:
    ctx = scenarios()[strategy_id]
    signals = STRATEGY_REGISTRY[strategy_id](ctx)
    return canonical_json([s.model_dump(exclude=EXCLUDE, mode="json") for s in signals])


def test_parity_against_goldens() -> None:
    assert GOLDEN_DIR.is_dir(), "golden files missing; run python -m tests.test_parity"
    mismatches: list[str] = []
    for sid in ("F1", "F2", "F3", "F4", "F5"):
        golden_path = GOLDEN_DIR / f"{sid}.json"
        assert golden_path.is_file(), f"missing golden for {sid}"
        expected = canonical_json(json.loads(golden_path.read_text()))
        actual = _canonical(sid)
        if actual != expected:
            mismatches.append(sid)
    assert not mismatches, f"signal parity broken for: {mismatches} (version bump required?)"


def _write_goldens() -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    for sid in ("F1", "F2", "F3", "F4", "F5"):
        payload = json.loads(_canonical(sid))
        (GOLDEN_DIR / f"{sid}.json").write_text(json.dumps(payload, indent=1, sort_keys=True))
        print(f"golden written: {sid} ({len(payload)} signals)")


if __name__ == "__main__":
    _write_goldens()
