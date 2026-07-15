"""Pre-flight before every send (§9): mode/account/permission gates and the
strict-TTL caches whose expiry re-validates against the terminal."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from tests.exec_helpers import ExecHarness

from aegis_velocity.core.config import EnvSettings, TradingMode
from aegis_velocity.execution.preflight import Preflight, PreflightResult


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[ExecHarness]:
    harness = ExecHarness(tmp_path)
    yield harness
    harness.close()


def _env(h: ExecHarness, **overrides: object) -> EnvSettings:
    base: dict[str, object] = {
        "login": h.sim.login,
        "server": h.sim.server,
        "live_trading_enabled": False,
        "live_account_allowlist": (),
    }
    base.update(overrides)
    return EnvSettings(**base)  # type: ignore[arg-type]


def _preflight(
    h: ExecHarness,
    mode: TradingMode = TradingMode.DEMO,
    env: EnvSettings | None = None,
    tick_fresh: bool = True,
    cost_ok: bool = True,
) -> Preflight:
    return Preflight(
        gateway=h.gateway,
        env=env if env is not None else _env(h),
        mode=mode,
        clock_fresh_fn=lambda tick: tick_fresh,
        cost_recheck_fn=lambda intent, tick: cost_ok,
    )


def _run(pf: Preflight, h: ExecHarness, idempotency_free: bool = True) -> PreflightResult:
    intent = h.market_intent()
    tick = h.sim.symbol_info_tick("EURUSD")
    assert tick is not None
    return pf.run(intent, tick, h.sim.server_time, idempotency_free)


def test_demo_mode_clean_pass(h: ExecHarness) -> None:
    result = _run(_preflight(h), h)
    assert result.ok, result.reasons


def test_shadow_mode_never_sends(h: ExecHarness) -> None:
    result = _run(_preflight(h, mode=TradingMode.SHADOW), h)
    assert not result.ok and "SHADOW_MODE_NO_ORDERS" in result.reasons


def test_live_requires_flag_and_allowlist(h: ExecHarness) -> None:
    result = _run(_preflight(h, mode=TradingMode.LIVE), h)
    assert "LIVE_NOT_ENABLED" in result.reasons
    assert "NOT_ALLOWLISTED" in result.reasons
    assert "DEMO_ACCOUNT_IN_LIVE_MODE" in result.reasons  # sim account is DEMO

    allowed = _preflight(
        h, mode=TradingMode.LIVE,
        env=_env(h, live_trading_enabled=True, live_account_allowlist=(h.sim.login,)),
    )
    result2 = _run(allowed, h)
    assert "LIVE_NOT_ENABLED" not in result2.reasons
    assert "NOT_ALLOWLISTED" not in result2.reasons


def test_account_and_server_mismatch_fail_closed(h: ExecHarness) -> None:
    wrong = _preflight(h, env=_env(h, login=999999, server="Other-Server"))
    result = _run(wrong, h)
    assert "ACCOUNT_MISMATCH" in result.reasons
    assert "SERVER_MISMATCH" in result.reasons


def test_investor_and_netting_blocked(h: ExecHarness) -> None:
    h.sim.is_investor = True
    pf = _preflight(h)
    result = _run(pf, h)
    assert "TRADE_NOT_ALLOWED" in result.reasons

    h.sim.is_investor = False
    h.sim.margin_mode = "NETTING"
    pf.expire_caches()  # force re-validation against the changed terminal
    result2 = _run(pf, h)
    assert "NETTING_ACCOUNT" in result2.reasons


def test_tick_staleness_and_cost_recheck(h: ExecHarness) -> None:
    stale = _run(_preflight(h, tick_fresh=False), h)
    assert "TICK_STALE" in stale.reasons
    costly = _run(_preflight(h, cost_ok=False), h)
    assert "COST_GATE_FAIL" in costly.reasons


def test_stale_risk_verdict_and_intent(h: ExecHarness) -> None:
    pf = _preflight(h)
    intent = h.market_intent()
    tick = h.sim.symbol_info_tick("EURUSD")
    assert tick is not None
    later = h.sim.server_time + timedelta(seconds=5)  # both ages exceed limits
    result = pf.run(intent, tick, later, idempotency_free=True)
    assert "RISK_VERDICT_STALE" in result.reasons
    assert "INTENT_STALE" in result.reasons


def test_duplicate_intent_blocked(h: ExecHarness) -> None:
    result = _run(_preflight(h), h, idempotency_free=False)
    assert "DUPLICATE_INTENT" in result.reasons


def test_order_check_failure_surfaces(h: ExecHarness) -> None:
    pf = _preflight(h)
    intent = h.market_intent(volume=0.017)  # invalid step -> order_check 10014
    tick = h.sim.symbol_info_tick("EURUSD")
    assert tick is not None
    result = pf.run(intent, tick, h.sim.server_time, idempotency_free=True)
    assert any(r.startswith("ORDER_CHECK_FAIL") for r in result.reasons)


def test_account_cache_ttl_avoids_refetch_until_expired(h: ExecHarness) -> None:
    pf = _preflight(h)
    _run(pf, h)
    _run(pf, h)
    assert pf.cache_refreshes == 1  # second run inside the 5 s TTL
    pf.expire_caches()
    _run(pf, h)
    assert pf.cache_refreshes == 2  # expiry re-validates, never skips


def test_expired_cache_sees_terminal_changes(h: ExecHarness) -> None:
    pf = _preflight(h)
    assert _run(pf, h).ok
    h.sim.trade_allowed = False  # permission revoked at the terminal
    # even inside the TTL, the UNCACHED broker order_check blocks immediately
    cached = _run(pf, h)
    assert not cached.ok
    assert any(r.startswith("ORDER_CHECK_FAIL") for r in cached.reasons)
    pf.expire_caches()
    fresh = _run(pf, h)
    assert "TRADE_NOT_ALLOWED" in fresh.reasons  # account re-validation catches it too


