"""Trade retcode policy table (§4.2). Execution is DRIVEN by this table —
no ad-hoc retcode handling anywhere else."""

from __future__ import annotations

from enum import StrEnum

# MT5 trade server return codes
REQUOTE = 10004
REJECT = 10006
CANCEL = 10007
PLACED = 10008
DONE = 10009
DONE_PARTIAL = 10010
ERROR = 10011
TIMEOUT = 10012
INVALID = 10013
INVALID_VOLUME = 10014
INVALID_PRICE = 10015
INVALID_STOPS = 10016
TRADE_DISABLED = 10017
MARKET_CLOSED = 10018
NO_MONEY = 10019
PRICE_CHANGED = 10020
PRICE_OFF = 10021
INVALID_EXPIRATION = 10022
ORDER_CHANGED = 10023
TOO_MANY_REQUESTS = 10024
NO_CHANGES = 10025
SERVER_DISABLES_AT = 10026
CLIENT_DISABLES_AT = 10027
LOCKED = 10028
FROZEN = 10029
INVALID_FILL = 10030
CONNECTION = 10031

RETCODE_NAMES: dict[int, str] = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
}


class RetcodeAction(StrEnum):
    VERIFY_FILL = "VERIFY_FILL"  # success claim: verify against positions/deals
    ACCEPT_PARTIAL = "ACCEPT_PARTIAL"  # accept filled volume; NEVER chase remainder
    RETRY_REFRESH = "RETRY_REFRESH"  # refresh tick, re-run gates, retry (<=2 total)
    RECOMPUTE_STOPS_ONCE = "RECOMPUTE_STOPS_ONCE"  # recompute vs stops_level once, else reject
    RETRY_ONCE = "RETRY_ONCE"  # one retry after full re-check
    REJECT_REFRESH_SPECS = "REJECT_REFRESH_SPECS"  # reject + refresh specs + SPEC_MISMATCH alert
    REJECT_QUARANTINE = "REJECT_QUARANTINE"  # our bug: reject, quarantine strategy, CRITICAL
    DEFER_DATA_CHECK = "DEFER_DATA_CHECK"  # market closed / no quotes: defer + integrity check
    REJECT_HALT_ENTRIES = "REJECT_HALT_ENTRIES"  # NO_MONEY: reject and halt pending risk review
    HALT_TRADING_DISABLED = "HALT_TRADING_DISABLED"  # alert with exact fix (Algo Trading button)
    BACKOFF_STORM_CHECK = "BACKOFF_STORM_CHECK"  # exponential backoff + storm fuse check
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"  # may have executed: reconcile before ANY resend
    SWITCH_FILLING_ONCE = "SWITCH_FILLING_ONCE"  # 10030: next mode on the ladder, once
    REJECT_FINAL = "REJECT_FINAL"


POLICY: dict[int, RetcodeAction] = {
    DONE: RetcodeAction.VERIFY_FILL,
    PLACED: RetcodeAction.VERIFY_FILL,
    DONE_PARTIAL: RetcodeAction.ACCEPT_PARTIAL,
    REQUOTE: RetcodeAction.RETRY_REFRESH,
    PRICE_CHANGED: RetcodeAction.RETRY_REFRESH,
    INVALID_PRICE: RetcodeAction.RETRY_REFRESH,
    INVALID_STOPS: RetcodeAction.RECOMPUTE_STOPS_ONCE,
    REJECT: RetcodeAction.RETRY_ONCE,
    INVALID_VOLUME: RetcodeAction.REJECT_REFRESH_SPECS,
    INVALID: RetcodeAction.REJECT_QUARANTINE,
    MARKET_CLOSED: RetcodeAction.DEFER_DATA_CHECK,
    PRICE_OFF: RetcodeAction.DEFER_DATA_CHECK,
    NO_MONEY: RetcodeAction.REJECT_HALT_ENTRIES,
    TRADE_DISABLED: RetcodeAction.HALT_TRADING_DISABLED,
    SERVER_DISABLES_AT: RetcodeAction.HALT_TRADING_DISABLED,
    CLIENT_DISABLES_AT: RetcodeAction.HALT_TRADING_DISABLED,
    TOO_MANY_REQUESTS: RetcodeAction.BACKOFF_STORM_CHECK,
    CONNECTION: RetcodeAction.UNKNOWN_OUTCOME,
    TIMEOUT: RetcodeAction.UNKNOWN_OUTCOME,
    INVALID_FILL: RetcodeAction.SWITCH_FILLING_ONCE,
}


def action_for(retcode: int | None) -> RetcodeAction:
    """None (no result object at all) is an UNKNOWN OUTCOME by definition."""
    if retcode is None:
        return RetcodeAction.UNKNOWN_OUTCOME
    return POLICY.get(retcode, RetcodeAction.REJECT_FINAL)


def retcode_name(retcode: int | None) -> str:
    if retcode is None:
        return "NO_RESULT"
    return RETCODE_NAMES.get(retcode, f"UNKNOWN_{retcode}")
