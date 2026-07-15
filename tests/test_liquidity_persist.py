"""SpreadHistory persistence + pruning."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aegis_velocity.cost.liquidity import SpreadHistory

NOW = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "spreads.json"
    hist = SpreadHistory(persist_path=path)
    for i in range(40):
        hist.record("EURUSD", NOW - timedelta(minutes=i), 8.0 + i % 3)
    hist.save()
    reloaded = SpreadHistory(persist_path=path)
    assert reloaded.spread_percentile("EURUSD", NOW, 50) is not None


def test_old_samples_pruned_beyond_history_days(tmp_path: Path) -> None:
    hist = SpreadHistory(history_days=20)
    ancient = NOW - timedelta(days=30)
    for _ in range(40):
        hist.record("EURUSD", ancient, 8.0)
    # recording something fresh prunes the ancient bucket
    hist.record("EURUSD", NOW.replace(minute=0), 9.0)
    assert hist.spread_percentile("EURUSD", NOW, 40) is None  # thin after prune


def test_corrupt_persist_file_ignored(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{broken")
    hist = SpreadHistory(persist_path=path)
    assert hist.spread_percentile("EURUSD", NOW, 40) is None
