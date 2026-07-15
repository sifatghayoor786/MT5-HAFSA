"""Pre-flight before EVERY send (§9) — cached with strict TTLs so speed never
skips safety. Any TTL expiry re-validates against the terminal."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from aegis_velocity.core.config import LIVE_MODES, EnvSettings, TradingMode
from aegis_velocity.execution.intents import ArmedIntent
from aegis_velocity.mt5.gateway import P1_ORDER, P2_ACCOUNT, Mt5Gateway
from aegis_velocity.mt5.protocol import AccountInfo, Tick

ACCOUNT_TTL_S = 5.0
MODE_TTL_S = 5.0


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Cached:
    value: object = None
    expires_at: float = 0.0


class Preflight:
    def __init__(
        self,
        gateway: Mt5Gateway,
        env: EnvSettings,
        mode: TradingMode,
        clock_fresh_fn: Callable[[Tick], bool],
        cost_recheck_fn: Callable[[ArmedIntent, Tick], bool],
    ) -> None:
        self._gateway = gateway
        self._env = env
        self._mode = mode
        self._clock_fresh = clock_fresh_fn
        self._cost_recheck = cost_recheck_fn
        self._account_cache = _Cached()
        self.checks_run = 0
        self.cache_refreshes = 0

    def _account(self) -> AccountInfo | None:
        now = time.monotonic()
        if self._account_cache.expires_at < now:
            self.cache_refreshes += 1
            info = self._gateway.call(P2_ACCOUNT, "account_info")
            self._account_cache = _Cached(
                value=info if isinstance(info, AccountInfo) else None,
                expires_at=now + ACCOUNT_TTL_S,
            )
        value = self._account_cache.value
        return value if isinstance(value, AccountInfo) else None

    def expire_caches(self) -> None:
        self._account_cache = _Cached()

    def run(
        self,
        intent: ArmedIntent,
        tick: Tick,
        server_now: datetime,
        idempotency_free: bool,
    ) -> PreflightResult:
        self.checks_run += 1
        reasons: list[str] = []

        # 1. mode permits execution at all
        if self._mode in LIVE_MODES and not self._env.live_trading_enabled:
            reasons.append("LIVE_NOT_ENABLED")
        if self._mode is TradingMode.SHADOW:
            reasons.append("SHADOW_MODE_NO_ORDERS")

        # 2-3. account identity, allowlist, permissions (cached 5 s)
        account = self._account()
        if account is None:
            reasons.append("NO_ACCOUNT_INFO")
        else:
            if self._env.login is not None and account.login != self._env.login:
                reasons.append("ACCOUNT_MISMATCH")
            if self._env.server and account.server != self._env.server:
                reasons.append("SERVER_MISMATCH")
            if self._mode in LIVE_MODES and account.login not in (
                self._env.live_account_allowlist or ()
            ):
                reasons.append("NOT_ALLOWLISTED")
            if not account.trade_allowed or account.is_investor:
                reasons.append("TRADE_NOT_ALLOWED")
            if account.margin_mode != "HEDGING":
                reasons.append("NETTING_ACCOUNT")
            if self._mode in LIVE_MODES and account.trade_mode == "DEMO":
                reasons.append("DEMO_ACCOUNT_IN_LIVE_MODE")

        # 4. tick freshness (<= 500 ms on the server clock)
        if not self._clock_fresh(tick):
            reasons.append("TICK_STALE")

        # 5. live spread + cost gate re-check
        if not self._cost_recheck(intent, tick):
            reasons.append("COST_GATE_FAIL")

        # 6. risk verdict age <= 2 s
        if intent.risk_stale(server_now):
            reasons.append("RISK_VERDICT_STALE")

        # 7. armed intent staleness
        if intent.stale(server_now):
            reasons.append("INTENT_STALE")

        # 8. idempotency
        if not idempotency_free:
            reasons.append("DUPLICATE_INTENT")

        # 9. order_check at the broker
        if not reasons:
            check = self._gateway.call(P1_ORDER, "order_check", intent.request)
            if check is None or check.retcode != 0:
                code = check.retcode if check is not None else "none"
                reasons.append(f"ORDER_CHECK_FAIL:{code}")

        return PreflightResult(ok=not reasons, reasons=reasons)
