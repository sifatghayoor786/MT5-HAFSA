"""Pydantic-validated configuration. Fail-closed: any schema error refuses startup.

Credentials are read from the environment only (populated from a git-ignored .env
by the launcher); passwords live in SecretStr and are registered with the
redaction filter the moment they are loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from aegis_velocity.core.security import register_secret


class ConfigError(Exception):
    """Raised on any invalid configuration; startup must abort."""


class TradingMode(StrEnum):
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    DEMO = "DEMO"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE = "LIVE"


LIVE_MODES = frozenset({TradingMode.LIVE_CANARY, TradingMode.LIVE})


class LossVelocityCfg(BaseModel):
    R_lost: float = Field(gt=0)
    window_minutes: int = Field(gt=0)
    pause_minutes: int = Field(gt=0)


class MicroCooldownCfg(BaseModel):
    losses: int = Field(gt=0)
    minutes: int = Field(gt=0)


class SlippageBreakerCfg(BaseModel):
    window_fills: int = Field(gt=0)
    p90_multiple: float = Field(gt=1.0)


class RiskConfig(BaseModel):
    risk_per_trade: float = Field(gt=0, le=0.01)
    max_risk_per_trade: float = Field(gt=0, le=0.02)
    max_total_open_risk: float = Field(gt=0, le=0.05)
    daily_equity_loss_halt: float = Field(gt=0, le=0.10)
    weekly_equity_loss_halt: float = Field(gt=0, le=0.20)
    hard_drawdown_halt: float = Field(gt=0, le=0.30)
    max_simultaneous_positions: int = Field(gt=0)
    max_positions_per_symbol: int = Field(gt=0)
    max_trades_per_hour_global: int = Field(gt=0)
    max_trades_per_symbol_per_hour: int = Field(gt=0)
    max_trades_per_day_global: int = Field(gt=0)
    loss_velocity_halt: LossVelocityCfg
    consecutive_loss_micro_cooldown: MicroCooldownCfg
    anti_churn_seconds: int = Field(ge=0)
    order_storm_fuse_per_minute: int = Field(gt=0)
    slippage_breaker: SlippageBreakerCfg
    max_hold_seconds_default: int = Field(gt=0)
    min_margin_level_pct: float = Field(gt=100)
    max_slippage_points: dict[str, int]
    max_spread_points: dict[str, int]
    canary_fills: int = Field(gt=0)

    @model_validator(mode="after")
    def _sanity(self) -> Self:
        if self.risk_per_trade > self.max_risk_per_trade:
            raise ValueError("risk_per_trade exceeds max_risk_per_trade")
        if self.daily_equity_loss_halt >= self.weekly_equity_loss_halt:
            raise ValueError("daily halt must be tighter than weekly halt")
        if self.weekly_equity_loss_halt >= self.hard_drawdown_halt:
            raise ValueError("weekly halt must be tighter than hard drawdown halt")
        for table_name in ("max_slippage_points", "max_spread_points"):
            if "default" not in getattr(self, table_name):
                raise ValueError(f"{table_name} requires a 'default' entry")
        return self

    def slippage_cap(self, symbol: str) -> int:
        return self.max_slippage_points.get(symbol, self.max_slippage_points["default"])

    def spread_cap(self, symbol: str) -> int:
        return self.max_spread_points.get(symbol, self.max_spread_points["default"])


class CostGateCfg(BaseModel):
    k_multiple: float = Field(default=4.0)
    k_floor: float = Field(default=3.0, ge=3.0)
    min_net_rr: float = Field(default=1.0)
    min_net_rr_floor: float = Field(default=0.8, ge=0.8)

    @model_validator(mode="after")
    def _floors(self) -> Self:
        if self.k_multiple < self.k_floor:
            raise ValueError(f"k_multiple {self.k_multiple} below hard floor {self.k_floor}")
        if self.min_net_rr < self.min_net_rr_floor:
            raise ValueError("min_net_rr below configured floor")
        return self


class LiquidityWindowCfg(BaseModel):
    spread_percentile_max: int = Field(default=40, gt=0, le=100)
    history_days: int = Field(default=20, gt=0)


class CostsConfig(BaseModel):
    commission_per_lot_per_side: dict[str, float]
    slippage_prior_points: dict[str, float]
    cost_gate: CostGateCfg
    liquidity_window: LiquidityWindowCfg

    @model_validator(mode="after")
    def _defaults_present(self) -> Self:
        for table_name in ("commission_per_lot_per_side", "slippage_prior_points"):
            if "default" not in getattr(self, table_name):
                raise ValueError(f"{table_name} requires a 'default' entry")
        return self

    def commission(self, symbol: str) -> float:
        table = self.commission_per_lot_per_side
        return table.get(symbol, table["default"])

    def slippage_prior(self, symbol: str) -> float:
        table = self.slippage_prior_points
        return table.get(symbol, table["default"])


class ScalpEligibilityCfg(BaseModel):
    max_stops_level_points: int = Field(gt=0)
    max_spread_p50_points: dict[str, int]
    min_ticks_per_minute: float = Field(gt=0)

    @model_validator(mode="after")
    def _default_present(self) -> Self:
        if "default" not in self.max_spread_p50_points:
            raise ValueError("max_spread_p50_points requires a 'default' entry")
        return self

    def spread_p50_cap(self, symbol: str) -> int:
        return self.max_spread_p50_points.get(symbol, self.max_spread_p50_points["default"])


class SymbolsConfig(BaseModel):
    universe: list[str] = Field(min_length=1)
    scalp_eligibility: ScalpEligibilityCfg
    per_symbol_caps: dict[str, dict[str, int]]

    @field_validator("universe")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate symbols in universe")
        return v


class SessionWindow(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ValueError(f"invalid HH:MM time: {v!r}")
        return v

    def start_minutes(self) -> int:
        h, m = self.start.split(":")
        return int(h) * 60 + int(m)

    def end_minutes(self) -> int:
        h, m = self.end.split(":")
        return int(h) * 60 + int(m)


class NewsBlackoutCfg(BaseModel):
    high_impact_minutes: int = Field(gt=0)
    medium_impact_minutes: int = Field(gt=0)
    stale_calendar_hours: int = Field(gt=0)


class SessionsConfig(BaseModel):
    sessions: dict[str, SessionWindow]
    entry_windows: dict[str, list[str]]
    rollover_blackout: SessionWindow
    friday_cutoff: str
    news_blackout: NewsBlackoutCfg

    @model_validator(mode="after")
    def _windows_exist(self) -> Self:
        for scope, names in self.entry_windows.items():
            for name in names:
                if name not in self.sessions:
                    raise ValueError(f"entry_windows[{scope}] references unknown session {name!r}")
        return self


class StrategyCfg(BaseModel):
    name: str
    enabled: bool
    version: int = Field(ge=0)
    trigger: Literal["tick_armed", "pending"]
    context_tf: Literal["M1", "M5", "M15"]
    max_signal_age_s: float = Field(gt=0)
    max_hold_s: int = Field(gt=0)
    min_cost_multiple: float = Field(ge=3.0)
    min_rr: float = Field(ge=0.8)
    cooldown_s: int = Field(ge=0)
    params: dict[str, float | int | str]

    def config_hash(self) -> str:
        canon = json.dumps(
            {"name": self.name, "version": self.version, "params": self.params},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


class StrategiesConfig(BaseModel):
    strategies: dict[str, StrategyCfg]

    @model_validator(mode="after")
    def _f6_never_originates(self) -> Self:
        f6 = self.strategies.get("F6")
        if f6 is not None and f6.enabled and f6.version == 0:
            raise ValueError("F6 scaffold (version 0) may not be enabled")
        return self


class CorrelationsConfig(BaseModel):
    max_correlation_weighted_risk: float = Field(gt=0)
    pairs: dict[str, float]

    @field_validator("pairs")
    @classmethod
    def _valid_pairs(cls, v: dict[str, float]) -> dict[str, float]:
        for key, w in v.items():
            if ":" not in key:
                raise ValueError(f"correlation key must be 'A:B', got {key!r}")
            if not 0 <= w <= 1:
                raise ValueError(f"correlation weight out of [0,1]: {key}={w}")
        return v

    def weight(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return self.pairs.get(f"{a}:{b}", self.pairs.get(f"{b}:{a}", 0.0))


class EnvSettings(BaseModel):
    """Process environment (.env). Password never leaves SecretStr."""

    terminal_path: str = ""
    login: int | None = None
    password: SecretStr | None = None
    server: str = ""
    trading_mode: TradingMode = TradingMode.SHADOW
    live_trading_enabled: bool = False
    live_account_allowlist: tuple[int, ...] = ()
    live_confirmation_phrase: SecretStr | None = None
    database_url: str = "sqlite:///data/aegis_velocity.db"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    bridge_port: int = 8790

    @classmethod
    def from_environ(cls, env: dict[str, str] | None = None) -> EnvSettings:
        e = os.environ if env is None else env

        def _get(key: str, default: str = "") -> str:
            return e.get(key, default).strip()

        login_raw = _get("MT5_LOGIN")
        allow_raw = _get("LIVE_ACCOUNT_ALLOWLIST")
        try:
            settings = cls(
                terminal_path=_get("MT5_TERMINAL_PATH"),
                login=int(login_raw) if login_raw else None,
                password=SecretStr(_get("MT5_PASSWORD")) if _get("MT5_PASSWORD") else None,
                server=_get("MT5_SERVER"),
                trading_mode=TradingMode(_get("TRADING_MODE", "SHADOW").upper()),
                live_trading_enabled=_get("LIVE_TRADING_ENABLED", "false").lower() == "true",
                live_account_allowlist=tuple(
                    int(x) for x in allow_raw.split(",") if x.strip()
                ),
                live_confirmation_phrase=(
                    SecretStr(_get("LIVE_CONFIRMATION_PHRASE"))
                    if _get("LIVE_CONFIRMATION_PHRASE")
                    else None
                ),
                database_url=_get("DATABASE_URL", "sqlite:///data/aegis_velocity.db"),
                dashboard_host=_get("DASHBOARD_HOST", "127.0.0.1"),
                dashboard_port=int(_get("DASHBOARD_PORT", "8000")),
                bridge_port=int(_get("BRIDGE_PORT", "8790")),
            )
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"invalid environment configuration: {exc}") from exc
        if settings.password is not None:
            register_secret(settings.password.get_secret_value())
        if settings.live_confirmation_phrase is not None:
            register_secret(settings.live_confirmation_phrase.get_secret_value())
        return settings


class DeskConfig(BaseModel):
    risk: RiskConfig
    costs: CostsConfig
    symbols: SymbolsConfig
    sessions: SessionsConfig
    strategies: StrategiesConfig
    correlations: CorrelationsConfig
    env: EnvSettings

    def config_hash(self) -> str:
        """Hash of trading-relevant config; arming tokens bind to this."""
        canon = json.dumps(
            {
                "risk": self.risk.model_dump(),
                "costs": self.costs.model_dump(),
                "symbols": self.symbols.model_dump(),
                "sessions": self.sessions.model_dump(),
                "strategies": self.strategies.model_dump(),
                "correlations": self.correlations.model_dump(),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _load_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    return raw


def load_desk_config(
    root: Path, env: dict[str, str] | None = None
) -> DeskConfig:
    cfg_dir = root / "configs"
    try:
        return DeskConfig(
            risk=RiskConfig(**_load_yaml(cfg_dir / "risk.yaml")),
            costs=CostsConfig(**_load_yaml(cfg_dir / "costs.yaml")),
            symbols=SymbolsConfig(**_load_yaml(cfg_dir / "symbols.yaml")),
            sessions=SessionsConfig(**_load_yaml(cfg_dir / "sessions.yaml")),
            strategies=StrategiesConfig(
                **_load_yaml(cfg_dir / "strategies.yaml")
            ),
            correlations=CorrelationsConfig(
                **_load_yaml(cfg_dir / "correlations.yaml")
            ),
            env=EnvSettings.from_environ(env),
        )
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"configuration invalid: {exc}") from exc


def apply_risk_tightening(current: RiskConfig, proposed: RiskConfig) -> RiskConfig:
    """Hot-reload while live: only risk-TIGHTENING changes are accepted (§13)."""
    loosened: list[str] = []
    tighten_down = (
        "risk_per_trade",
        "max_risk_per_trade",
        "max_total_open_risk",
        "daily_equity_loss_halt",
        "weekly_equity_loss_halt",
        "hard_drawdown_halt",
        "max_simultaneous_positions",
        "max_positions_per_symbol",
        "max_trades_per_hour_global",
        "max_trades_per_symbol_per_hour",
        "max_trades_per_day_global",
        "order_storm_fuse_per_minute",
        "max_hold_seconds_default",
    )
    for name in tighten_down:
        if getattr(proposed, name) > getattr(current, name):
            loosened.append(name)
    if proposed.anti_churn_seconds < current.anti_churn_seconds:
        loosened.append("anti_churn_seconds")
    if proposed.min_margin_level_pct < current.min_margin_level_pct:
        loosened.append("min_margin_level_pct")
    if loosened:
        raise ConfigError(f"hot-reload rejected, loosens risk: {', '.join(loosened)}")
    return proposed
