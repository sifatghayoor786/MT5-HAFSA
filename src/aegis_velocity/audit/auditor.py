"""Auditor (§10): environment-separated TCA over the ledger + write-behind
analytics. Order FACTS are written synchronously by the execution engine;
this worker only enriches asynchronously — a crash here loses no facts."""

from __future__ import annotations

import queue
import statistics
import threading
from dataclasses import dataclass, field

from aegis_velocity.audit.stats import (
    WilsonCI,
    expectancy,
    max_drawdown,
    profit_factor,
    wilson_ci,
)
from aegis_velocity.core.ledger import Ledger


@dataclass
class StrategyStats:
    environment: str
    strategy_id: str
    symbol: str
    returns_r: list[float] = field(default_factory=list)
    slippages: list[float] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    costs_ccy: float = 0.0
    gross_ccy: float = 0.0

    @property
    def wilson(self) -> WilsonCI:
        wins = sum(1 for r in self.returns_r if r > 0)
        return wilson_ci(wins, len(self.returns_r))

    def summary(self) -> dict[str, object]:
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            return lat[min(len(lat) - 1, round(p / 100 * (len(lat) - 1)))]

        return {
            "environment": self.environment,
            "strategy": self.strategy_id,
            "symbol": self.symbol,
            "n": len(self.returns_r),
            "win_rate": self.wilson.display(),
            "expectancy_r": round(expectancy(self.returns_r), 4),
            "profit_factor": round(profit_factor(self.returns_r), 3),
            "max_dd_r": round(max_drawdown(self.returns_r), 2),
            "cost_burn_ccy": round(self.costs_ccy, 2),
            "gross_ccy": round(self.gross_ccy, 2),
            "slippage_p50": statistics.median(self.slippages) if self.slippages else 0.0,
            "latency_p50_ms": pct(50),
            "latency_p90_ms": pct(90),
            "latency_p99_ms": pct(99),
        }


@dataclass(frozen=True)
class TradeFact:
    environment: str  # BACKTEST | SHADOW | DEMO | LIVE — never merged
    strategy_id: str
    symbol: str
    return_r: float
    slippage_points: float
    latency_ms: float
    cost_ccy: float
    gross_ccy: float


class Auditor:
    """Write-behind analytics; environments held strictly apart."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._stats: dict[tuple[str, str, str], StrategyStats] = {}
        self._queue: queue.Queue[TradeFact | None] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="auditor", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def submit(self, fact: TradeFact) -> None:
        self._queue.put(fact)

    def _run(self) -> None:
        while True:
            fact = self._queue.get()
            try:
                if fact is None:
                    return
                self._ingest(fact)
                self._ledger.append(
                    "trade_analytics",
                    {
                        "environment": fact.environment,
                        "strategy": fact.strategy_id,
                        "symbol": fact.symbol,
                        "return_r": fact.return_r,
                        "slippage_points": fact.slippage_points,
                        "latency_ms": fact.latency_ms,
                    },
                )
            finally:
                self._queue.task_done()

    def _ingest(self, fact: TradeFact) -> None:
        key = (fact.environment, fact.strategy_id, fact.symbol)
        with self._lock:
            stats = self._stats.get(key)
            if stats is None:
                stats = StrategyStats(fact.environment, fact.strategy_id, fact.symbol)
                self._stats[key] = stats
            stats.returns_r.append(fact.return_r)
            stats.slippages.append(fact.slippage_points)
            stats.latencies_ms.append(fact.latency_ms)
            stats.costs_ccy += fact.cost_ccy
            stats.gross_ccy += fact.gross_ccy

    def ingest_sync(self, fact: TradeFact) -> None:
        """Synchronous path for reports/tests."""
        self._ingest(fact)

    def drain(self) -> None:
        """Wait for queued enrichment; order FACTS are already safe regardless."""
        self._queue.join()

    def report(self, environment: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            rows = [
                s.summary()
                for (env, _, _), s in sorted(self._stats.items())
                if environment is None or env == environment
            ]
        return rows

    def returns_for(self, environment: str, strategy_id: str) -> list[float]:
        with self._lock:
            out: list[float] = []
            for (env, sid, _), s in self._stats.items():
                if env == environment and sid == strategy_id:
                    out.extend(s.returns_r)
        return out
