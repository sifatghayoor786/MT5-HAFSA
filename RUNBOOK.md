# AEGIS VELOCITY MT5 — Complete Runbook (from zero)

This guide assumes nothing is installed yet. Follow it top to bottom.

Two ways to run:
- **SIM mode** — works on any computer (Windows/Mac/Linux), needs no broker account,
  sends no orders anywhere. Start here.
- **REAL mode** — needs a Windows PC with the MetaTrader 5 terminal. Live trading is
  reachable only through evidence gates and a human-typed arming ceremony.

---

## PART 1 — Install the tools (Windows)

### Step 1. Install Python 3.11 or newer (64-bit)
1. Go to https://www.python.org/downloads/ and download the latest Python 3.x
   **Windows installer (64-bit)**.
2. Run the installer. On the FIRST screen, tick **"Add python.exe to PATH"** — do not
   skip this — then click "Install Now".
3. Verify: open **PowerShell** (press Windows key, type `powershell`, Enter) and run:
   ```powershell
   python --version
   ```
   You must see `Python 3.11.x` or newer.

### Step 2. Install Git
1. Download from https://git-scm.com/download/win and install with all defaults.
2. Verify in PowerShell: `git --version`

### Step 3. Install the MetaTrader 5 terminal (skip for SIM-only)
1. Download MT5 from **your broker's** website (not a random site).
2. Install it, start it, and log in once with your **login number, MASTER password,
   and exact server name** (File → Login to Trade Account). The desk rejects
   investor-password logins by design.
3. Your account must be a **hedging** account. If it is netting, the desk will refuse
   to execute (analysis only). Ask your broker if unsure.

---

## PART 2 — Get the project and install it

Open PowerShell and run these one at a time:

```powershell
cd $HOME
git clone https://github.com/sifatghayoor786/MT5-HAFSA.git
cd MT5-HAFSA
git checkout claude/aegis-velocity-mt5-aksry2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Notes:
- If activation is blocked, run
  `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` once, then retry.
- `(.venv)` at the start of your prompt means the environment is active. Re-activate
  with `.\.venv\Scripts\Activate.ps1` every time you open a new PowerShell window.
- On Mac/Linux the activate command is `source .venv/bin/activate` (SIM mode only).
- For REAL mode also run: `pip install MetaTrader5`

---

## PART 3 — First run in SIM mode (safe on any machine)

Run each command and check the expected result before moving on.

```powershell
python -m aegis_velocity doctor
```
Expected: a checklist. Without MT5 it ends `execution path: SIM-ONLY (SimMt5Client)`.

```powershell
python -m aegis_velocity validate-config
```
Expected: `config OK: 10 symbols, strategies enabled: F1, F2, F3, F4, F5, mode=SHADOW, ...`

```powershell
python -m aegis_velocity calendar-import configs/calendar/sample_calendar.csv
python -m aegis_velocity discover-symbols
python -m aegis_velocity scalp-eligibility
```
Expected: `imported 5 calendar events`, `mapped 10/10 symbols [SIM]`, and a measured
ELIGIBLE/DISQUALIFIED verdict per symbol. Without a calendar import, all entries are
blocked (news status UNKNOWN fails closed) — that is intentional.

```powershell
python -m aegis_velocity run --mode shadow --duration 3600
```
Expected: `[SIM] SHADOW scan over 3600 scripted ticks/symbol (simulation data)` then
`scan done: N signals, N decisions recorded; ledger OK`. Shadow mode NEVER sends
orders; it only observes, decides, and records. Most decisions being REJECT
(SESSION_BLOCKED, LIQUIDITY_WINDOW, COST_GATE_FAIL...) is the safety system working.

```powershell
python -m aegis_velocity verify-ledger
python -m aegis_velocity report
python -m pytest
```
Expected: `ledger: OK ... chain verified`, recent ledger rows, and `198 passed`.

SIM numbers are simulation only — they are never evidence for live trading.

---

## PART 4 — Connect the real terminal (Windows only)

### Step 4a. Create your credentials file
```powershell
copy .env.example .env
notepad .env
```
Fill in and save:
```
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_LOGIN=<your account number>
MT5_PASSWORD=<your MASTER password>
MT5_SERVER=<exact server string from the terminal, e.g. Deriv-Demo>
TRADING_MODE=SHADOW
LIVE_TRADING_ENABLED=false
```
`.env` is git-ignored. Never commit it, never paste it anywhere. Start with a DEMO
account — the pipeline requires demo evidence before live anyway.

### Step 4b. Verify the connection
```powershell
python -m aegis_velocity doctor      # must now say REAL-TERMINAL capable
python -m aegis_velocity test-mt5
```
Expected: your masked login, exact server, currency, `margin_mode=HEDGING`,
`trade_allowed=True`. A netting account prints a refusal for execution.

---

## PART 5 — Compile and attach the guardian EA

1. Compile (adjust the path if MT5 is installed elsewhere):
   ```powershell
   & "C:\Program Files\MetaTrader 5\metaeditor64.exe" /compile:"mql5\AegisFastGuard.mq5" /log
   ```
   Success = `AegisFastGuard.ex5` appears next to the .mq5 with 0 errors in the log.
   Copy the .ex5 into the terminal's `MQL5\Experts\` folder
   (File → Open Data Folder in MT5 shows where that is).
2. In MT5: Tools → Options → Expert Advisors → enable **Allow algorithmic trading**
   and allow connections for **127.0.0.1** (needed for the EA↔Python socket bridge).
3. Drag **AegisFastGuard** from the Navigator onto exactly ONE chart (any symbol).
   The smiley icon means it is running.

The EA only manages exits (break-even, trailing, time-stop, spread-blowout, OCO
cancel, missing-SL enforcement, emergency flatten). It can never open a trade — that
invariant is enforced by a test on its source code. If the bridge drops, the EA
enters PROTECT (keeps enforcing exits, changes nothing else) and Python enters SAFE
(no new entries).

---

## PART 6 — Gather real evidence (this decides everything)

```powershell
python -m aegis_velocity discover-symbols
python -m aegis_velocity scalp-eligibility
python -m aegis_velocity record-ticks --hours 8
```
Record ticks during London/NY hours, across several days. Then shadow, at least
5 separate sessions:
```powershell
python -m aegis_velocity run --mode shadow
```
Watch the dashboard at http://127.0.0.1:8000 while it runs. Then evaluate:
```powershell
python -m aegis_velocity backtest --strategy F1 --symbol EURUSD
python -m aegis_velocity walkforward --strategy F1
```
(repeat for F2–F5). Gates: ≥1,000 out-of-sample trades, profit factor ≥ 1.10 with
positive expectancy under STRESSED costs (spread ×1.5, slippage ×2, latency ×2),
parameter plateau, Monte Carlo, FDR. `INSUFFICIENT_DATA` means keep recording.
**"No strategy clears costs" is a valid, honest outcome — the desk will not trade
on hope.**

---

## PART 7 — Demo validation

Only after walk-forward gates pass, with a DEMO account in `.env`:
```powershell
python -m aegis_velocity run --mode demo
```
Requirements before live: ≥300 demo fills, slippage within 1.5× model, ≥3 restarts
with clean reconciliation (`python -m aegis_velocity reconcile`), zero critical
incidents.

---

## PART 8 — Going live (only you can do this)

These commands refuse to run from scripts or automation — you must type them in an
interactive console. Update `.env` first:
```
TRADING_MODE=LIVE_CANARY
LIVE_TRADING_ENABLED=true
LIVE_ACCOUNT_ALLOWLIST=<your live account number>
```
Then:
```powershell
python -m aegis_velocity activate-live        # shows account/limits, runs self-tests,
                                              # requires typing I_ACCEPT_LIVE_TRADING_RISK
python -m aegis_velocity run --mode live_canary   # first 50 fills at MINIMUM lot size
python -m aegis_velocity promote-live-full    # only passes after the canary gate
```
The arming token auto-invalidates on account/server/config change, emergency stop,
hard drawdown, or prolonged bridge loss — you must re-arm after any of these.

---

## PART 9 — Daily operations and emergencies

- Status: dashboard at http://127.0.0.1:8000; `positions`, `pendings`, `ea-status`
- Reports: `report`, `cost-report`, `latency-report`, `spread-report`
- Integrity: `verify-ledger`, `parity-check`, `reconcile`
- **EMERGENCY STOP:** `python -m aegis_velocity emergency-stop --flatten`
  (halts entries, invalidates arming, flattens all desk positions)
- Stand down without flattening: `python -m aegis_velocity disarm-live`

Built-in halts that trigger on their own: daily −1% / weekly −2.5% / hard −5% equity,
loss-velocity (6R lost in 60 min), hourly/daily trade caps, anti-churn, micro-cooldowns,
order-storm fuse, slippage breaker, EA time-stops.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` not recognised | Reinstall Python with "Add to PATH" ticked; reopen PowerShell |
| Activate.ps1 blocked | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| doctor says SIM-ONLY on Windows | `pip install MetaTrader5` inside the venv; check `.env` terminal path |
| `initialize failed` | Wrong login/password/server in `.env`; server string must match the terminal exactly |
| Netting refusal | Ask your broker for a hedging account |
| EA socket errors | Allow 127.0.0.1 in Tools → Options → Expert Advisors |
| All decisions REJECT | Usually honest gating: import a fresh calendar, build spread history, trade session hours |
| backtest says INSUFFICIENT_DATA | Record more ticks — bar data is never accepted |
