# AEGIS VELOCITY MT5 — project conventions and state

Tick-driven MT5 scalping desk. Python control plane originates all orders; an MQL5 guardian EA
(`mql5/AegisFastGuard.mq5`) manages positions at tick speed and NEVER originates trades.
Full specification lives in the V3 master prompt; `BUILDLOG.md` records every stage gate verbatim.

## Current stage

Stages 1–11 COMPLETE on the sim path (Linux, no MT5 terminal possible here).
Stage 12 (demo/live prep) NOT EXECUTED — USER-ACTION on the Windows trading host.
Open gaps: RealMt5Client never exercised against a terminal; EA source uncompiled;
promotion pipeline evidence = INSUFFICIENT_DATA (no recorded broker ticks); dashboard
implements a subset of §14 panels. BUILDLOG.md carries the verbatim gates and the
full §18 final report incl. the numbered launch checklist.

## Hard rules (do not relax)

- HUMAN-ONLY commands: `activate-live`, `run --mode live|live_canary`, `promote-live-full`.
  The CLI refuses them non-interactively (`aegis_velocity/cli.py::_require_human`). Never bypass.
- Never read/print/commit `.env` or credentials. `core/security.py` redaction filter is
  installed on the root logger before any MT5 code runs; tests in `tests/test_security.py`.
- No stubs in risk/cost/execution/reconciliation/arming/bridge code. If it can't be done
  correctly, mark the stage BLOCKED in BUILDLOG.md.
- Fills are reported only after verification against positions/deals (§2.5 execution truth).
- All timestamps UTC; broker time handled via `core/clock.py` empirical offset.

## Architecture map (src/aegis_velocity/)

- `core/` — config (pydantic, fail-closed), events (correlation_id threading), order state
  machine, broker clock, security/redaction, hash-chained ledger (SQLite WAL + journal),
  equity anchors, in-process event bus.
- `mt5/` — `Mt5Client` Protocol; `SimMt5Client` (deterministic CI simulator: scripted ticks,
  retcode injection, partial fills, latency, hedging/netting toggle, pending triggers);
  `RealMt5Client` (import-guarded, Windows only); single-thread gateway with P0–P3 priority
  queue + tick pump; retcode policy table; symbol discovery + measured scalp eligibility;
  tick recorder.
- `cost/` — Cost Engine (gate #1): spread+commission+slippage clearance, k-floor, RR floor with
  WR_be, liquidity windows, rollover/Friday/news blackouts (stale calendar ⇒ blocked).
- `risk/` — Risk Commander: dual-method sizing (tick math × order_calc_profit, 2% agreement),
  floor-only rounding, frequency-scaled limits, persisted anchors/counters, velocity guards
  (hourly/daily caps, loss-velocity halt, micro-cooldown, anti-churn, order-storm fuse,
  slippage breaker), canary logic.
- `bridge/` — EA↔Python JSON-lines TCP bridge, 1 s heartbeats, PROTECT/SAFE bridge-loss
  protocol, `SimEa` for CI.
- `strategies/` — F1–F5 pure deterministic functions (tick window + closed bars + config) →
  Signal | None; F6 scaffolded OFF. Strategies never place orders.
- `consensus/` — hard gates then slim quality score; ≤10 ms decision path; full record to
  ledger asynchronously.
- `execution/` — armed intents, idempotency (UNIQUE key, one in-flight per symbol), preflight
  TTL caches, retcode-table-driven send with ≤2 retries, Unknown-Outcome Protocol, pending
  lifecycle + OCO sibling-cancel, startup reconciliation (MT5 = source of truth), manual
  override detection.
- `audit/` — Wilson CI / WR_be stats, environment-separated TCA, quarantine rules,
  write-behind analytics (order facts are written synchronously by execution).
- `backtest/` — tick-level backtester (bid/ask fills, latency sim 100–400 ms, pending
  triggers at touch, commission, stressed costs). Bar backtests are NON-EVIDENCE.
- `pipeline/` — promotion gates (OOS counts, stressed costs, plateau, Monte Carlo, BH-FDR).
- `dashboard/` — FastAPI + SSE on 127.0.0.1:8000, event-bus data only.
- `arming.py` — arming token lifecycle and invalidation.
- `cli.py` — all §15 commands; human-only guard.

## Conventions

- Python 3.11, `src/` layout. Gates per stage: `ruff check .` clean, `mypy` strict clean on
  core/mt5/risk/execution/cost/bridge, `pytest` green. Run all three before every commit.
- Magic number scheme `77_SSS_VV` (strategy id, version); order comment `AEG|{idem_key}`.
- Idempotency key: `sha1(account|broker_symbol|strategy_id|trigger_id|direction)[:12]`.
- Points (int, in `point` units) are the working unit for spreads/distances; prices are float.
- Every event carries `correlation_id`; ledger row kinds are snake_case.
- Tests: safety behaviour including failure paths; no vacuous asserts.

## Environment notes

- This dev environment: Linux, Python 3.11.15 64-bit, no MT5 terminal → SimMt5Client only.
  Real-terminal steps are USER-ACTION items (Windows 10/11, Python 3.11+ 64-bit, MT5 terminal,
  MetaEditor `metaeditor64.exe /compile` for the EA).
- Run tests: `python3 -m pytest`. Lint: `ruff check .`. Types: `python3 -m mypy src/aegis_velocity`.
