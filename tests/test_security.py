"""Redaction filter: no secret may survive into any log record."""

import logging

import pytest

from aegis_velocity.core import security


@pytest.fixture(autouse=True)
def _clean_secrets() -> None:
    security.clear_secrets()


def test_registered_secret_is_masked_in_log_records(caplog: pytest.LogCaptureFixture) -> None:
    security.install_redaction()
    security.register_secret("Sup3rS3cretPw!")
    log = logging.getLogger("test.redaction")
    with caplog.at_level(logging.INFO):
        log.info("connecting with password Sup3rS3cretPw! now")
    assert "Sup3rS3cretPw!" not in caplog.text
    assert "***" in caplog.text


def test_password_assignment_patterns_masked() -> None:
    for text in (
        "password=hunter22",
        "PASSWORD: hunter22",
        'pwd="hunter22"',
        "MT5_PASSWORD=hunter22",
        "token: abcdef123",
        "phrase=I_ACCEPT_LIVE_TRADING_RISK",
    ):
        out = security.redact(text)
        assert "hunter22" not in out
        assert "abcdef123" not in out
        assert "I_ACCEPT_LIVE_TRADING_RISK" not in out


def test_login_masked_to_last_four() -> None:
    out = security.redact("account login=12345678 on server X")
    assert "12345678" not in out
    assert "5678" in out
    assert security.mask_login(12345678) == "****5678"
    assert security.mask_login("123") == "***"


def test_redaction_applies_to_formatted_args(caplog: pytest.LogCaptureFixture) -> None:
    security.install_redaction()
    security.register_secret("argSecret99")
    log = logging.getLogger("test.redaction.args")
    with caplog.at_level(logging.INFO):
        log.info("value is %s", "argSecret99")
    assert "argSecret99" not in caplog.text


def test_short_or_empty_secrets_not_registered() -> None:
    security.register_secret("")
    security.register_secret(None)
    security.register_secret("ab")
    assert security.redact("ab plain text") == "ab plain text"
