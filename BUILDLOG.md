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
