# AEGIS VELOCITY MT5 — BUILDLOG

Stage-by-stage record. Gate command output is verbatim. Completion states are truthful:
COMPLETE / PARTIALLY COMPLETE (gaps) / BLOCKED (blockers).

---

## Stage 1 — Inspect — COMPLETE

Environment: Linux container (remote Claude Code session), NOT Windows. Python 3.11.15,
64-bit. `ruff`, `mypy`, `pytest` available. No MT5 terminal or MetaEditor can exist on
this host (the `MetaTrader5` package and terminal are Windows-only).

**Declared path: SIM-ONLY.** All build/test work runs against `SimMt5Client`. Every
real-terminal step is a USER-ACTION for the Windows trading host. No terminal, broker,
tick, or fill output is fabricated anywhere in this log.

`python -m aegis_velocity doctor` (verbatim):

```
AEGIS VELOCITY doctor
  [       PASS] python: 3.11.15 64-bit on linux
  [       INFO] platform: Linux-6.18.5-x86_64-with-glibc2.39 — MT5 terminal requires Windows; sim-only here
  [USER-ACTION] mt5_package: not installed (Windows-only); on the trading host: pip install MetaTrader5
  [USER-ACTION] terminal: MT5_TERMINAL_PATH unset or missing — set it in .env on the trading host
  [USER-ACTION] mt5_login: not set — fill .env from .env.example
  [USER-ACTION] mt5_password: not set — fill .env from .env.example
  [USER-ACTION] mt5_server: not set — fill .env from .env.example
  [       PASS] configs: correlations.yaml, costs.yaml, risk.yaml, sessions.yaml, strategies.yaml, symbols.yaml
  [       PASS] data_dir: writable: /home/user/MT5-HAFSA/data
  execution path: SIM-ONLY (SimMt5Client)
```

`.gitignore` verified to cover `.env` (line 151) before first commit.

## Stage 2 — Foundation — COMPLETE

Built: `core/security.py` (log-record-factory redaction: registered secrets,
password-assignment patterns, login masked to last 4 — installed before any MT5 code),
`core/config.py` (pydantic fail-closed loader for risk/costs/symbols/sessions/strategies/
correlations + EnvSettings with SecretStr; hot-reload tightening-only guard; k-floor ≥ 3
hard; F6 scaffold cannot be enabled), `core/events.py` (correlation-threaded events incl.
LatencyWaterfall + ExecutionRecord), `core/state.py` (order state machine, requote retry
budget = 2 then forced REJECTED; UNKNOWN_OUTCOME resolvable only to broker truth; PARTIAL
can never chase), `core/clock.py` (median broker offset, persisted; 500 ms tick freshness;
bar-close = open+tf+2s AND newer bar; DST alert; >30 s drift halt), `core/ledger.py`
(hash-chained SQLite WAL + fsynced JSONL journal, `verify()` walks chain), `core/anchors.py`
(atomic persisted day/week/peak anchors + rolling counters), `core/bus.py`.
CLI skeleton with HUMAN-ONLY guard (`activate-live`, `promote-live-full`,
`run --mode live|live_canary` refuse without an interactive TTY).

Gate (verbatim):

```
$ ruff check .
All checks passed!
$ python3 -m mypy src/aegis_velocity/core src/aegis_velocity/doctor.py
Success: no issues found in 10 source files
$ python3 -m pytest tests/ -q
....................................
36 passed in 0.35s
$ python -m aegis_velocity validate-config
config OK: 10 symbols, strategies enabled: F1, F2, F3, F4, F5, mode=SHADOW, config_hash=43f6dabed6dcdf1a
```

Tamper test: editing a ledger payload or deleting a row makes `verify()` fail — covered in
`tests/test_ledger.py`.

## Stage 3 — MT5 layer + tick recorder — COMPLETE (sim path)

Built: `mt5/protocol.py` (`Mt5Client` Protocol + broker dataclasses), `mt5/retcodes.py`
(§4.2 policy table — execution is table-driven; `None` result ⇒ UNKNOWN_OUTCOME),
`mt5/sim.py` (`SimMt5Client`: scripted ticks, retcode injection incl. execute-anyway for
unknown-outcome truth, partial fills, latency injection, hedging/netting toggle, investor
block, pending trigger/expiry at touch, SL/TP execution, floating-P&L equity),
`mt5/real.py` (`RealMt5Client`, import-guarded, Windows-only — NOT exercised here),
`mt5/gateway.py` (single worker thread owns every client call; P0–P3 priority heap;
tick pump 20–50 ms with dedupe; queue/latency metrics; TOO_MANY_REQUESTS backoff),
`mt5/symbols.py` (discovery: exact/suffix/prefix, ambiguity reported never guessed;
measured scalp eligibility failing closed on thin samples), `mt5/recorder.py`
(JSONL tick recorder + loader).

Stage gate integration test (`tests/test_stage3_integration.py`):
connect → discover (EURUSD→EURUSDm) → pump+record → pending place → trigger at touch →
partial fill (10010) → unknown-outcome (no result, broker executed) → reconcile by
comment `AEG|{key}` + magic finds the position. PASSES on sim.

Gate (verbatim):

```
$ ruff check .
All checks passed!
$ python3 -m mypy src/aegis_velocity/core src/aegis_velocity/mt5 src/aegis_velocity/doctor.py
Success: no issues found in 18 source files
$ python3 -m pytest tests/
61 passed in 2.35s
```

USER-ACTION (Windows host): `python -m aegis_velocity test-mt5` to record the real
terminal round-trip; real tick recording via `record-ticks --hours N`.

## Stage 4 — Cost Engine + sentinel — COMPLETE

Built `cost/`: engine (cost_points = spread + commission→points + slippage_p50; TP ≥ k×cost
with k=4 config/3 hard floor; net-RR floor with WR_be computed and carried on every verdict;
hard spread caps), liquidity windows (per symbol×server-hour spread distributions, entry only
at ≤ p40 of 20-day same-hour, cold start FAILS CLOSED), sessions (entry windows, rollover
23:55–00:10 crossing midnight, Friday cutoff, weekends), news calendar (CSV import, high ±20 /
medium ±10 min for affected currencies, stale/missing ⇒ UNKNOWN ⇒ blocked), daily cost-burn
meter.

Gate: `ruff` clean; `mypy` strict clean (22 files); `pytest` 76 passed. Commit 15e06f3.

## Stage 5 — Guardian EA + bridge — COMPLETE (compile = USER-ACTION)

Built `mql5/AegisFastGuard.mq5` (never originates: only PositionModify/PositionClose/
OrderDelete appear — enforced by a static source test; BE move, trailing, hard time-stop,
spread-blowout exit, missing-SL enforcement + alert, pending expiry backstop,
transaction-driven race-safe OCO sibling cancel, emergency flatten, socket bridge with 1 s
heartbeats + Common\Files mailbox fallback, PROTECT mode on bridge loss with pending-cancel
grace) and `bridge/` (JSON-lines schema v1, threaded TCP server, SAFE-mode signal
`entries_allowed`, watchdog, resync handshake required for recovery, `SimEa` for CI).

Tested: EA silence ⇒ Python SAFE; Python silence ⇒ SimEa PROTECT; recovery requires
heartbeat + resync; commands/OCO pairs delivered; malformed lines harmless.

NOT verified here (impossible on Linux): MQL5 compilation and behaviour. USER-ACTION:
`metaeditor64.exe /compile:"mql5\AegisFastGuard.mq5" /log` on the Windows host; record
the compile log; attach EA to one chart; allow socket connections for 127.0.0.1.

Gate: `ruff` clean; `mypy` strict clean (26 files); `pytest` 84 passed. Commit fa43626.

## Stage 6 — Risk Commander — COMPLETE

Built `risk/`: dual-method sizing (tick math × order_calc_profit, 2% agreement or
SPEC_MISMATCH REJECT; None from broker calc ⇒ REJECT — accurate risk or no trade),
floor-only rounding (floored < volume_min ⇒ RISK_TOO_SMALL_FOR_MIN_LOT, never round up),
max-risk cap re-floor, margin projection ≥ 500% floor, persisted halt ladder
(daily 1% / weekly 2.5% / hard 5% from anchors), position caps, correlation-weighted
open-risk cap (tightened `correlations.yaml` cap 0.0040 < total 0.0060 so the check is
meaningful), velocity guards each independent + restart-safe (hourly/daily caps,
loss-velocity 6R/60min ⇒ 60min pause, micro-cooldown 3 losses ⇒ 15 min per symbol×strategy,
anti-churn 90 s same-direction, latching order-storm fuse, per-symbol slippage breaker),
canary (min-lot regardless of size, skip if min-lot risk > 1.5× target, exits after 50 fills).
Sim margin model fixed to respect margin currency (base-ccy notional for FX, price-based
for metals).

Gate: `ruff` clean; `mypy` strict clean (29 files); `pytest` 107 passed. Commit d107a2e.

## Stage 7 — Strategies + parity harness — COMPLETE

Built `strategies/`: pure deterministic contract (tick window + closed bars + config →
signals; `trigger_id` tick-window scoped; config_hash embedded), F1 momentum burst,
F2 sweep & snap-back, F3 session-open expansion (pending), F4 compression OCO straddle
(pending pair, shared oco_group), F5 stop-run reversion, F6 scaffold permanently returning
nothing (and rejected by config validation if enabled at version 0).
Golden-file parity harness: committed goldens `tests/golden/F{1..5}.json`; replay must
reproduce signals bit-for-bit (volatile identity fields excluded). Parity: 5/5 = 100%
on fixtures.

Gate: `ruff` clean; `mypy` strict clean (32 files); `pytest` 117 passed. Commit 3f6b3a4.

## Stage 8 — Consensus + execution — COMPLETE

Built `consensus/council.py` (hard gates: cost verdict, data integrity ≥ 70, tick ≤ 500 ms
on server clock, signal age, conflicting exposure, quarantine; slim quality score →
APPROVE / APPROVE_REDUCED / SHADOW_ONLY / REJECT; measured decision latency — sub-ms in
tests, budget ≤ 10 ms) and `execution/`: idempotency store (UNIQUE key, one in-flight per
symbol, OCO pair counts as ONE logical intent, UNKNOWN_OUTCOME locks the symbol), armed
intents with price-refresh retries, preflight with 5 s TTL caches + order_check, engine
driven ONLY by the §4.2 retcode table (verified fills, partial accept never chase, requote
budget = 2, INVALID_STOPS recompute once, 10030 filling-ladder switch once, NO_MONEY /
TRADE_DISABLED halt callbacks, storm fuse pre-send), Unknown-Outcome Protocol
(reconcile-before-resend for market AND pending; absent ⇒ re-evaluation permitted, never a
blind resend), pending place/cancel lifecycle, Python-side OCO backstop (EA primary),
startup reconciliation (MT5 = truth: match by comment key, adopt orphans ADOPTED, enforce
missing SLs, resolve UNKNOWNs first, manual-override detection via closing-deal magic ⇒
pause symbol×strategy).

Gate (verbatim):

```
$ ruff check .
All checks passed!
$ python3 -m mypy src/aegis_velocity/{core,mt5,cost,bridge,risk,execution,consensus,strategies}
Success: no issues found in 40 source files
$ python3 -m pytest tests/
147 passed in 5.69s
```
