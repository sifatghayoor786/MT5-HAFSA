"""Config loading is fail-closed; hot-reload only tightens risk."""

from pathlib import Path

import pytest
import yaml

from aegis_velocity.core.config import (
    ConfigError,
    EnvSettings,
    RiskConfig,
    TradingMode,
    apply_risk_tightening,
    load_desk_config,
)

REPO = Path(__file__).resolve().parents[1]


def test_repo_configs_load_and_hash() -> None:
    cfg = load_desk_config(REPO, env={})
    assert "EURUSD" in cfg.symbols.universe
    assert cfg.risk.risk_per_trade == 0.0002
    assert cfg.env.trading_mode is TradingMode.SHADOW
    assert len(cfg.config_hash()) == 16
    f1 = cfg.strategies.strategies["F1"]
    assert f1.config_hash() == f1.config_hash()  # deterministic


def _risk_dict() -> dict[str, object]:
    raw = yaml.safe_load((REPO / "configs" / "risk.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_risk_exceeding_max_rejected() -> None:
    d = _risk_dict()
    d["risk_per_trade"] = 0.005
    d["max_risk_per_trade"] = 0.001
    with pytest.raises(Exception, match="max_risk_per_trade"):
        RiskConfig(**d)  # type: ignore[arg-type]


def test_halt_ladder_ordering_enforced() -> None:
    d = _risk_dict()
    d["daily_equity_loss_halt"] = 0.05
    d["weekly_equity_loss_halt"] = 0.02
    with pytest.raises(Exception, match="tighter"):
        RiskConfig(**d)  # type: ignore[arg-type]


def test_cost_gate_k_floor_is_hard(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(REPO / "configs", tmp_path / "configs")
    costs = yaml.safe_load((tmp_path / "configs" / "costs.yaml").read_text())
    costs["cost_gate"]["k_multiple"] = 2.0  # below hard floor 3
    (tmp_path / "configs" / "costs.yaml").write_text(yaml.safe_dump(costs))
    with pytest.raises(ConfigError, match="floor"):
        load_desk_config(tmp_path, env={})


def test_f6_scaffold_cannot_be_enabled(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(REPO / "configs", tmp_path / "configs")
    strat = yaml.safe_load((tmp_path / "configs" / "strategies.yaml").read_text())
    strat["strategies"]["F6"]["enabled"] = True
    (tmp_path / "configs" / "strategies.yaml").write_text(yaml.safe_dump(strat))
    with pytest.raises(ConfigError, match="F6"):
        load_desk_config(tmp_path, env={})


def test_missing_config_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_desk_config(tmp_path, env={})


def test_env_settings_parse_and_secret_registration() -> None:
    env = {
        "MT5_LOGIN": "12345678",
        "MT5_PASSWORD": "S3cretPw#1",
        "MT5_SERVER": "Deriv-Server",
        "TRADING_MODE": "demo",
        "LIVE_ACCOUNT_ALLOWLIST": "111, 222",
    }
    s = EnvSettings.from_environ(env)
    assert s.login == 12345678
    assert s.trading_mode is TradingMode.DEMO
    assert s.live_account_allowlist == (111, 222)
    assert s.password is not None
    assert "S3cretPw#1" not in repr(s)
    from aegis_velocity.core.security import redact

    assert "S3cretPw#1" not in redact("x S3cretPw#1 y")


def test_env_bad_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        EnvSettings.from_environ({"TRADING_MODE": "YOLO"})


def test_hot_reload_only_tightens() -> None:
    base = RiskConfig(**_risk_dict())  # type: ignore[arg-type]
    tighter = base.model_copy(update={"risk_per_trade": 0.0001})
    assert apply_risk_tightening(base, tighter).risk_per_trade == 0.0001

    looser = base.model_copy(update={"max_trades_per_hour_global": 60})
    with pytest.raises(ConfigError, match="max_trades_per_hour_global"):
        apply_risk_tightening(base, looser)

    shorter_churn = base.model_copy(update={"anti_churn_seconds": 10})
    with pytest.raises(ConfigError, match="anti_churn_seconds"):
        apply_risk_tightening(base, shorter_churn)
