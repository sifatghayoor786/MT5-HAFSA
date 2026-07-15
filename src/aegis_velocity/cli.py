"""CLI entry point (§15).

HUMAN-ONLY commands (`activate-live`, `run --mode live|live_canary`,
`promote-live-full`) hard-refuse when stdin/stdout are not an interactive TTY,
so no agent, script, or scheduler can ever reach live execution.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aegis_velocity.core.security import install_redaction

HUMAN_ONLY_COMMANDS = frozenset({"activate-live", "promote-live-full"})
HUMAN_ONLY_RUN_MODES = frozenset({"live", "live_canary"})


class HumanOnlyRefusal(Exception):
    pass


def _require_human(what: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HumanOnlyRefusal(
            f"{what} is HUMAN-ONLY: it requires an interactive terminal and may never be "
            "invoked by scripts, schedulers, or agents. Run it yourself in a console."
        )


def _repo_root() -> Path:
    return Path.cwd()


def cmd_doctor(_: argparse.Namespace) -> int:
    from aegis_velocity.doctor import run_doctor

    report = run_doctor(_repo_root())
    print(report.render())
    return 0


def cmd_validate_config(_: argparse.Namespace) -> int:
    from aegis_velocity.core.config import ConfigError, load_desk_config

    try:
        cfg = load_desk_config(_repo_root())
    except ConfigError as exc:
        print(f"CONFIG INVALID: {exc}")
        return 1
    enabled = [sid for sid, s in cfg.strategies.strategies.items() if s.enabled]
    print(
        "config OK: "
        f"{len(cfg.symbols.universe)} symbols, strategies enabled: {', '.join(enabled)}, "
        f"mode={cfg.env.trading_mode.value}, config_hash={cfg.config_hash()}"
    )
    return 0


def cmd_verify_ledger(args: argparse.Namespace) -> int:
    from aegis_velocity.core.ledger import Ledger

    db = Path(args.db) if args.db else _repo_root() / "data" / "aegis_velocity.db"
    if not db.exists():
        print(f"no ledger database at {db}")
        return 1
    ledger = Ledger(db)
    result = ledger.verify()
    ledger.close()
    print(f"ledger: {'OK' if result.ok else 'TAMPERED/BROKEN'} rows={result.rows} {result.detail}")
    return 0 if result.ok else 2


def cmd_test_mt5(_: argparse.Namespace) -> int:
    from aegis_velocity.mt5.real import real_client_available

    available, detail = real_client_available()
    if not available:
        print(f"real MT5 terminal not available here: {detail}")
        print("USER-ACTION: run this on the Windows trading host with .env filled in.")
        return 1
    from aegis_velocity.runtime import connect_and_report

    return connect_and_report(_repo_root())


def cmd_discover_symbols(_: argparse.Namespace) -> int:
    from aegis_velocity.runtime import discover_symbols_cli

    return discover_symbols_cli(_repo_root())


def cmd_scalp_eligibility(_: argparse.Namespace) -> int:
    from aegis_velocity.runtime import scalp_eligibility_cli

    return scalp_eligibility_cli(_repo_root())


def cmd_record_ticks(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import record_ticks_cli

    return record_ticks_cli(_repo_root(), hours=float(args.hours))


def cmd_run(args: argparse.Namespace) -> int:
    mode = str(args.mode).lower()
    if mode in HUMAN_ONLY_RUN_MODES:
        _require_human(f"run --mode {mode}")
    from aegis_velocity.runtime import run_desk_cli

    return run_desk_cli(_repo_root(), mode=mode, duration_s=args.duration)


def cmd_activate_live(_: argparse.Namespace) -> int:
    _require_human("activate-live")
    from aegis_velocity.runtime import activate_live_cli

    return activate_live_cli(_repo_root())


def cmd_promote_live_full(_: argparse.Namespace) -> int:
    _require_human("promote-live-full")
    from aegis_velocity.runtime import promote_live_full_cli

    return promote_live_full_cli(_repo_root())


def cmd_disarm_live(_: argparse.Namespace) -> int:
    from aegis_velocity.arming import disarm

    disarm(_repo_root() / "data" / "arming.json", reason="cli disarm-live")
    print("disarmed: arming token invalidated")
    return 0


def cmd_emergency_stop(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import emergency_stop_cli

    return emergency_stop_cli(_repo_root(), flatten=bool(args.flatten))


def cmd_report(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import report_cli

    return report_cli(_repo_root(), which=args.which)


def cmd_backtest(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import backtest_cli

    return backtest_cli(_repo_root(), strategy=args.strategy, symbol=args.symbol)


def cmd_walkforward(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import walkforward_cli

    return walkforward_cli(_repo_root(), strategy=args.strategy)


def cmd_calendar_import(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import calendar_import_cli

    return calendar_import_cli(_repo_root(), csv_path=Path(args.csv))


def cmd_positions(args: argparse.Namespace) -> int:
    from aegis_velocity.runtime import positions_cli

    return positions_cli(_repo_root(), which=args.command)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aegis_velocity", description="AEGIS VELOCITY MT5 desk")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("validate-config").set_defaults(func=cmd_validate_config)
    vl = sub.add_parser("verify-ledger")
    vl.add_argument("--db", default="")
    vl.set_defaults(func=cmd_verify_ledger)
    sub.add_parser("test-mt5").set_defaults(func=cmd_test_mt5)
    sub.add_parser("discover-symbols").set_defaults(func=cmd_discover_symbols)
    sub.add_parser("scalp-eligibility").set_defaults(func=cmd_scalp_eligibility)

    rt = sub.add_parser("record-ticks")
    rt.add_argument("--hours", default="1")
    rt.set_defaults(func=cmd_record_ticks)

    bt = sub.add_parser("backtest")
    bt.add_argument("--strategy", required=True)
    bt.add_argument("--symbol", required=True)
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser("walkforward")
    wf.add_argument("--strategy", required=True)
    wf.set_defaults(func=cmd_walkforward)

    run = sub.add_parser("run")
    run.add_argument("--mode", required=True, choices=["shadow", "demo", "live_canary", "live"])
    run.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl-C")
    run.set_defaults(func=cmd_run)

    sub.add_parser("activate-live").set_defaults(func=cmd_activate_live)
    sub.add_parser("promote-live-full").set_defaults(func=cmd_promote_live_full)
    sub.add_parser("disarm-live").set_defaults(func=cmd_disarm_live)

    es = sub.add_parser("emergency-stop")
    es.add_argument("--flatten", action="store_true")
    es.set_defaults(func=cmd_emergency_stop)

    for name in ("positions", "pendings", "reconcile", "ea-status"):
        sub.add_parser(name).set_defaults(func=cmd_positions)

    for name in ("report", "cost-report", "latency-report", "spread-report", "parity-check"):
        rp = sub.add_parser(name)
        rp.set_defaults(func=cmd_report, which=name)

    ci = sub.add_parser("calendar-import")
    ci.add_argument("csv")
    ci.set_defaults(func=cmd_calendar_import)

    return p


def main(argv: list[str] | None = None) -> int:
    # Redaction is installed before anything else can log (rule §0.3).
    install_redaction()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except HumanOnlyRefusal as exc:
        print(f"REFUSED: {exc}")
        return 3
